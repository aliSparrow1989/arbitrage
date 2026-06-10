#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Crypto Arbitrage Bot: Nobitex vs OmpFinex vs Wallex vs Bitpin vs Ramzinex
Pairs: USDT/TMN, BTC/USDT, ETH/USDT

Requirements:
    pip install aiohttp --user

OmpFinex orderbook swap note:
    data[sym]["asks"] = real bids  (buy orders, descending)
    data[sym]["bids"] = real asks  (sell orders, ascending)
    Each element: {"price": "...", "amount": "..."}

Wallex & Bitpin price unit note:
    USDTTMN / USDT_IRT prices are in Toman; Nobitex/OmpFinex/Ramzinex use Rial.
    wallex_price_scale=10 and bitpin_price_scale=10 normalize to Rial.
    When placing orders, price is divided back by the scale.

Bitpin transfer fees:
    USDT BEP20 withdrawal = ~0.5 USDT (verified at bitpin.ir/fee).
    BTC and ETH fees need verification at bitpin.ir/fee.

Ramzinex notes:
    Auth: POST api_key+secret → JWT token; headers Authorization2 + x-api-key.
    Orderbook: publicapi.ramzinex.com/.../orderbooks/{pair_id}/buys_sells
    Prices are already in Rial (IRR); ramzinex_price_scale=1.0 for all pairs.
    Transfer fees need verification at ramzinex.com/app/commissions.
"""

import asyncio
import logging
import time

import aiohttp

try:
    from arbit_logger import TradeLogger as _TradeLogger
    _trade_logger = _TradeLogger()
except Exception:
    _trade_logger = None

def _tlog(opp, dry_run, status, **kw):
    """Safe wrapper — swallows PermissionError when Excel has the file open."""
    if _trade_logger:
        try:
            _trade_logger.log(opp, dry_run, status, **kw)
        except Exception as _e:
            logging.getLogger("arbit").warning("trade_log skipped: %s", _e)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────

NOBITEX_API_KEY   = "db2dd6e8158697670998f6adc3beae28edd81be2"
OMPFINEX_API_KEY  = ""
WALLEX_API_KEY    = ""
BITPIN_API_KEY    = "DgATeGvIqGSYEhI8Z0XL3mK5oRnDPBQPYKPrjp8PSleUEjjNWSOBmW5AL6BTEHZlFQrNGxMYn6kVEefHZgFJeIyUQSp5cPivEhS7h2sgu5gtZIwXYvM0mKSsh7XLjI17"   # ← your Bitpin API key (only needed for DRY_RUN=False)
BITPIN_SECRET_KEY = "ZvIh7OQSKASybemE4FLK8qAVyQrVR099bWO6WJHf0D5YC4P4i66Lz0TLMOOy3lzrBnPa2Mq3qb17B27EBfVysC6i68vTa8wspP5aWR2sVB0HJmTsh9fxcDG0KEpTXFEn"   # ← your Bitpin secret key
RAMZINEX_API_KEY  = ""   # ← your Ramzinex API key  (ramzinex.com/app/api-management)
RAMZINEX_SECRET   = ""   # ← your Ramzinex secret key

# Minimum net profit (%) after all fees to trigger a trade
MIN_PROFIT_PCT = 0.2

# Minimum net profit in USDT — trades below this dollar amount are skipped
MIN_PROFIT_USDT = 0.04

# Trade size per opportunity
TRADE_AMOUNT = {
    "USDT_IRT": 15.0,   # USDT
    "BTC_USDT": 0.05,    # BTC
    "ETH_USDT": 1.0,     # ETH
    "BNB_USDT": 1.0,     # BNB
    "XRP_USDT": 500.0,   # XRP
    "SOL_USDT": 5.0,     # SOL
    "TON_USDT": 100.0,   # TON
}

# Dry run = no real orders placed
DRY_RUN = True

# Seconds between full scans
SCAN_INTERVAL = 5

# Fraction of wallet balance to use when balance < TRADE_AMOUNT (applies only when DRY_RUN=False)
WALLET_USE_RATIO = 0.98

# If wallet balance < TRADE_AMOUNT × MIN_BALANCE_RATIO, skip the trade entirely
MIN_BALANCE_RATIO = 1 - WALLET_USE_RATIO

# Maximum trades per program run (0 = unlimited)
MAX_TRADES_PER_RUN = 0

# Minimum transfer thresholds — fees are amortized across multiple trades
# (no transfer on every trade; batched until accumulated imbalance reaches these levels)
MIN_USDT_TRANSFER_AMOUNT = 500.0          # minimum USDT withdrawal per transfer event
MIN_IRT_TRANSFER_IRR     = 1_000_000_000  # minimum IRT bank transfer: 100M Toman in Rial


# ─────────────────────────────────────────────
#  FEE TABLES
# ─────────────────────────────────────────────

NOBITEX_FEE = {
    # IRT markets (USDT/TMN)
    "IRT":  {"maker": 0.0017, "taker": 0.002},
    # USDT markets (BTC/USDT, ETH/USDT)
    "USDT": {"maker": 0.0010, "taker": 0.0013},
}

OMPFINEX_FEE = {
    # Blue level (base) — flat for all markets
    "flat_maker": 0.0035,
    "flat_taker": 0.0035,
}

WALLEX_FEE = {
    # Base level — verify at wallex.ir/commission
    "maker": 0.0025,
    "taker": 0.0030,
}

BITPIN_FEE = {
    # Base level — verify at bitpin.ir/fee
    "maker": 0.0030,
    "taker": 0.0035,
}

RAMZINEX_FEE = {
    # Base level — verified from docs.ramzinex.com/openapi.json FeeRes example
    "IRT":  {"maker": 0.0020, "taker": 0.0025},
    "USDT": {"maker": 0.0010, "taker": 0.0010},
}

# IRT (Rial) bank-transfer fees for rebalancing the Rial leg after a USDT/IRT trade.
# When you buy USDT at exchange A and sell at B, IRT must be moved back from B→A.
# Cost = withdrawal fee at B (sell side) + deposit fee at A (buy side).
# All amounts in IRR (Rial).  Sources: exchange fee pages (2025).

# Effective flat withdrawal fee per exchange (cap is always reached at typical trade sizes)
IRT_WITHDRAWAL_FLAT_IRR = {
    "nobitex":  40000,    # 4,000 Toman flat for 400K–40M Toman range  [nobitex.ir/pricing]
    "wallex":   80000,    # 8,000 Toman cap per 100M Toman  [wallex.ir/commission]
    "ompfinex": 100000,   # 10,000 Toman cap (min 1,000 T)  [ompfinex.com/commission]
    "bitpin":   60000,    # 6,000 Toman cap (min 2,000 T)   [bitpin.ir/fee]
    "ramzinex": 60000,    # VERIFY at ramzinex.com/app/commissions
}

# Deposit fee as fraction of IRR amount
IRT_DEPOSIT_FEE_PCT = {
    "nobitex":  0.0001,    # 0.01% (شناسه‌دار / حساب‌به‌حساب)  [nobitex.ir/pricing]
    "wallex":   0.0001,    # 0.01% (شناسه‌دار)                  [wallex.ir/commission]
    "ompfinex": 0.0,       # free                               [ompfinex.com/commission]
    "bitpin":   0.0,       # flat only (see below)              [bitpin.ir/fee]
    "ramzinex": 0.0,       # VERIFY at ramzinex.com/app/commissions
}

# Additional flat deposit fee (used where a flat fee applies regardless of percentage)
IRT_DEPOSIT_FLAT_IRR = {
    "nobitex":  0,
    "wallex":   0,
    "ompfinex": 0,
    "bitpin":   40000,    # 4,000 Toman flat for deposits > 20M Toman  [bitpin.ir/fee]
    "ramzinex": 0,        # VERIFY at ramzinex.com/app/commissions
}

# Transfer fees: (asset, source_exchange) -> fee in asset units
# source_exchange = the exchange you withdraw FROM (the sell side)
TRANSFER_FEE = {
    ("USDT", "nobitex"):  0.7,       # TRC20: 1 USDT
    ("USDT", "ompfinex"): 0.7,       # TRC20: 1 USDT
    ("USDT", "wallex"):   0.8,       # BSC:   0.8 USDT
    ("USDT", "bitpin"):   0.5,       # BEP20: ~0.5 USDT (verified bitpin.ir/fee)
    ("BTC",  "nobitex"):  0.00005,
    ("BTC",  "ompfinex"): 0.00005,
    ("BTC",  "wallex"):   0.00005,
    ("BTC",  "bitpin"):   0.003,     # VERIFY at bitpin.ir/fee
    ("ETH",  "nobitex"):  0.0004,
    ("ETH",  "ompfinex"): 0.0004,
    ("ETH",  "wallex"):   0.003,     # ERC20: 0.003 ETH
    ("ETH",  "bitpin"):   0.015,     # VERIFY at bitpin.ir/fee
    # BNB (BEP20/BSC)
    ("BNB",  "nobitex"):  0.001,
    ("BNB",  "ompfinex"): 0.001,     # VERIFY at ompfinex.com
    ("BNB",  "wallex"):   0.0005,
    ("BNB",  "bitpin"):   0.001,     # VERIFY at bitpin.ir/fee
    # XRP (XRP Ledger)
    ("XRP",  "nobitex"):  0.2,
    ("XRP",  "ompfinex"): 0.2,       # VERIFY at ompfinex.com
    ("XRP",  "wallex"):   0.2,       # VERIFY at wallex.ir
    ("XRP",  "bitpin"):   0.2,       # VERIFY at bitpin.ir/fee
    # SOL (Solana)
    ("SOL",  "nobitex"):  0.01,
    ("SOL",  "ompfinex"): 0.01,      # VERIFY at ompfinex.com
    ("SOL",  "wallex"):   0.01,
    ("SOL",  "bitpin"):   0.01,      # VERIFY at bitpin.ir/fee
    # TON (TON network) — only available on Nobitex, Wallex, and Ramzinex
    ("TON",  "nobitex"):  0.1,
    ("TON",  "wallex"):   0.02,
    ("TON",  "ramzinex"): 0.1,      # VERIFY at ramzinex.com/app/commissions
    # Ramzinex crypto withdrawal fees (VERIFY at ramzinex.com/app/commissions)
    ("USDT", "ramzinex"): 0.8,   # bsc USDT
    ("BTC",  "ramzinex"): 0.00005,
    ("ETH",  "ramzinex"): 0.004,
    ("BNB",  "ramzinex"): 0.001,
    ("XRP",  "ramzinex"): 0.2,
    ("SOL",  "ramzinex"): 0.01,
}


# ─────────────────────────────────────────────
#  EXCHANGE ENABLE / DISABLE
# ─────────────────────────────────────────────

# Set False to exclude an exchange from all arbitrage scanning.
# Example — only Bitpin ↔ Nobitex: set ompfinex=False, wallex=False
EXCHANGE_ENABLED = {
    "nobitex":  True,
    "ompfinex": True,
    "wallex":   True,
    "bitpin":   True,
    "ramzinex": True,
}


# ─────────────────────────────────────────────
#  PAIR CONFIGURATION
# ─────────────────────────────────────────────

PAIR_CONFIG = [
    {
        "enabled":             True,           # ← True/False to activate this pair
        "name":                "USDT/TMN",
        "nobitex_symbol":      "USDTIRT",
        "omf_symbol":          "USDTIRR",
        "wallex_symbol":       "USDTTMN",
        "wallex_price_scale":  10.0,           # Toman × 10 = Rial
        "bitpin_symbol":       "USDT_IRT",
        "bitpin_price_scale":  10.0,           # Toman × 10 = Rial
        "ramzinex_pair_id":    11,             # USDT/IRR — prices already in Rial
        "market_type":         "IRT",
        "transfer_asset":      "USDT",
        "nb_src":              "usdt",
        "nb_dst":              "rls",
        "amount_key":          "USDT_IRT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        "name":                "BTC/USDT",
        "nobitex_symbol":      "BTCUSDT",
        "omf_symbol":          "BTCUSDT",
        "wallex_symbol":       "BTCUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       "BTC_USDT",
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    12,
        "market_type":         "USDT",
        "transfer_asset":      "BTC",
        "nb_src":              "btc",
        "nb_dst":              "usdt",
        "amount_key":          "BTC_USDT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        "name":                "ETH/USDT",
        "nobitex_symbol":      "ETHUSDT",
        "omf_symbol":          "ETHUSDT",
        "wallex_symbol":       "ETHUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       "ETH_USDT",
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    13,
        "market_type":         "USDT",
        "transfer_asset":      "ETH",
        "nb_src":              "eth",
        "nb_dst":              "usdt",
        "amount_key":          "ETH_USDT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        "name":                "BNB/USDT",
        "nobitex_symbol":      "BNBUSDT",
        "omf_symbol":          "BNBUSDT",
        "wallex_symbol":       "BNBUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       "BNB_USDT",
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    18,
        "market_type":         "USDT",
        "transfer_asset":      "BNB",
        "nb_src":              "bnb",
        "nb_dst":              "usdt",
        "amount_key":          "BNB_USDT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        "name":                "XRP/USDT",
        "nobitex_symbol":      "XRPUSDT",
        "omf_symbol":          "XRPUSDT",
        "wallex_symbol":       "XRPUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       "XRP_USDT",
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    643,
        "market_type":         "USDT",
        "transfer_asset":      "XRP",
        "nb_src":              "xrp",
        "nb_dst":              "usdt",
        "amount_key":          "XRP_USDT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        "name":                "SOL/USDT",
        "nobitex_symbol":      "SOLUSDT",
        "omf_symbol":          "SOLUSDT",
        "wallex_symbol":       "SOLUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       "SOL_USDT",
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    218,
        "market_type":         "USDT",
        "transfer_asset":      "SOL",
        "nb_src":              "sol",
        "nb_dst":              "usdt",
        "amount_key":          "SOL_USDT",
    },
    {
        "enabled":             False,          # ← True/False to activate this pair
        # TON on Nobitex, Wallex, Ramzinex; omf_symbol=None and bitpin_symbol=None skips those
        "name":                "TON/USDT",
        "nobitex_symbol":      "TONUSDT",
        "omf_symbol":          None,           # not listed on OmpFinex
        "wallex_symbol":       "TONUSDT",
        "wallex_price_scale":  1.0,
        "bitpin_symbol":       None,           # not listed on Bitpin
        "bitpin_price_scale":  1.0,
        "ramzinex_pair_id":    434,
        "market_type":         "USDT",
        "transfer_asset":      "TON",
        "nb_src":              "ton",
        "nb_dst":              "usdt",
        "amount_key":          "TON_USDT",
    },
]

ACTIVE_PAIRS = [p for p in PAIR_CONFIG if p.get("enabled", False)]


# ─────────────────────────────────────────────
#  ORDER BOOK
# ─────────────────────────────────────────────

class OrderBook(object):
    def __init__(self, exchange, symbol, bids, asks):
        self.exchange = exchange
        self.symbol   = symbol
        self.bids     = bids    # [[price, amount], ...]  best-bid first (descending)
        self.asks     = asks    # [[price, amount], ...]  best-ask first (ascending)
        self.ts       = time.time()

    def best_bid(self):
        return self.bids[0][0] if self.bids else 0.0

    def best_ask(self):
        return self.asks[0][0] if self.asks else float("inf")

    def best_bid_volume(self):
        return self.bids[0][1] if self.bids else 0.0

    def best_ask_volume(self):
        return self.asks[0][1] if self.asks else 0.0


# ─────────────────────────────────────────────
#  NOBITEX CLIENT
# ─────────────────────────────────────────────

class NobitexClient(object):
    BASE = "https://apiv2.nobitex.ir"

    def __init__(self, api_key):
        self.api_key  = api_key
        self._session = None

    def _auth(self):
        return {"Authorization": "Token " + self.api_key}

    async def get_orderbook(self, symbol, price_scale=1.0):
        url     = self.BASE + "/v3/orderbook/" + symbol
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, timeout=timeout)
        data = await resp.json()

        def _parse(rows):
            return [[float(r[0]) * price_scale, float(r[1])] for r in rows]

        asks = sorted(_parse(data.get("asks", [])), key=lambda x: x[0])
        bids = sorted(_parse(data.get("bids", [])), key=lambda x: x[0], reverse=True)
        return OrderBook(exchange="nobitex", symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, order_type, src, dst, amount, price):
        payload = {
            "type":        order_type,
            "srcCurrency": src,
            "dstCurrency": dst,
            "amount":      str(amount),
            "price":       str(int(price)),
            "execution":   "limit",
        }
        url     = self.BASE + "/market/orders/add"
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, headers=self._auth(), timeout=timeout)
        return await resp.json()

    async def get_wallets(self):
        url     = self.BASE + "/users/wallets/list"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, headers=self._auth(), timeout=timeout)
        return await resp.json()


# ─────────────────────────────────────────────
#  OMPFINEX CLIENT
# ─────────────────────────────────────────────

class OmpFinexClient(object):
    BASE = "https://api.ompfinex.com"

    def __init__(self, api_key):
        self.api_key    = api_key
        self._session   = None
        self._cache     = {}
        self._cache_ts  = 0.0
        self._cache_ttl = 2.0   # reuse same fetch for 2 seconds

    def _auth(self):
        return {"Authorization": "Bearer " + self.api_key}

    async def _fetch_all(self):
        """
        Single call returns all orderbooks.
        Response: { "data": { SYMBOL: {"asks": [...], "bids": [...]}, ... } }
        Swap correction:
          "asks" key -> real bids  (buy orders, descending)
          "bids" key -> real asks  (sell orders, ascending)
        Each entry: {"price": "...", "amount": "..."}
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        url     = self.BASE + "/v1/orderbook"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, timeout=timeout)
        raw  = await resp.json()
        self._cache    = raw.get("data", raw)
        self._cache_ts = now
        return self._cache

    async def get_orderbook(self, internal_symbol, price_scale=1.0):
        symbol_map = {
            "USDTIRT": "USDTIRR",
            "BTCUSDT": "BTCUSDT",
            "ETHUSDT": "ETHUSDT",
        }
        omf_sym   = symbol_map.get(internal_symbol, internal_symbol)
        all_books = await self._fetch_all()
        pair_data = all_books.get(omf_sym, {})

        def _parse(rows):
            out = []
            for x in rows:
                if isinstance(x, dict):
                    out.append([float(x["price"]) * price_scale,
                                float(x.get("amount", x.get("quantity", 0)))])
                elif isinstance(x, list):
                    out.append([float(x[0]) * price_scale, float(x[1])])
            return out

        real_bids = sorted(_parse(pair_data.get("asks", [])), key=lambda x: x[0], reverse=True)
        real_asks = sorted(_parse(pair_data.get("bids", [])), key=lambda x: x[0])
        return OrderBook(exchange="ompfinex", symbol=omf_sym, bids=real_bids, asks=real_asks)

    async def place_order(self, market_id, side, quantity, price):
        payload = {
            "market_id":  market_id,
            "order_type": side,
            "quantity":   quantity,
            "price":      price,
            "type":       "limit",
        }
        url     = self.BASE + "/v1/user/orders"
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, headers=self._auth(), timeout=timeout)
        return await resp.json()

    async def get_wallets(self):
        url     = self.BASE + "/v1/user/wallets"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, headers=self._auth(), timeout=timeout)
        return await resp.json()


# ─────────────────────────────────────────────
#  WALLEX CLIENT
# ─────────────────────────────────────────────

class WallexClient(object):
    BASE = "https://api.wallex.ir"

    def __init__(self, api_key):
        self.api_key  = api_key
        self._session = None

    def _auth(self):
        return {"X-API-Key": self.api_key}

    async def get_orderbook(self, symbol, price_scale=1.0):
        """
        price_scale: multiply all returned prices by this factor before storing.
        For USDTTMN, Wallex quotes in Toman; price_scale=10 converts to Rial
        so prices are comparable with Nobitex (IRT) and OmpFinex (IRR).
        Response: {"result": {"ask": [...], "bid": [...]}, "success": true}
        Each entry: {"price": 170103, "quantity": 6.81, "sum": "..."}
        """
        url     = self.BASE + "/v1/depth"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, params={"symbol": symbol}, timeout=timeout)
        data = await resp.json()
        result = data.get("result", {})

        def _parse(rows):
            out = []
            for x in rows:
                if isinstance(x, dict):
                    out.append([float(x["price"]) * price_scale,
                                float(x.get("quantity", x.get("amount", 0)))])
                elif isinstance(x, list):
                    out.append([float(x[0]) * price_scale, float(x[1])])
            return out

        asks = sorted(_parse(result.get("ask", [])), key=lambda x: x[0])
        bids = sorted(_parse(result.get("bid", [])), key=lambda x: x[0], reverse=True)
        return OrderBook(exchange="wallex", symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, symbol, side, quantity, price, order_type="limit"):
        """side: 'buy' or 'sell'. price must be in Wallex native units (Toman for USDTTMN)."""
        payload = {
            "symbol":     symbol,
            "order_type": order_type,
            "side":       side,
            "quantity":   quantity,
            "price":      price,
        }
        url     = self.BASE + "/v1/account/orders"
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, headers=self._auth(), timeout=timeout)
        return await resp.json()

    async def get_wallets(self):
        url     = self.BASE + "/v1/account/balances"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, headers=self._auth(), timeout=timeout)
        return await resp.json()


# ─────────────────────────────────────────────
#  BITPIN CLIENT
# ─────────────────────────────────────────────

class BitpinClient(object):
    BASE = "https://api.bitpin.ir"

    def __init__(self, api_key, secret_key):
        self.api_key    = api_key
        self.secret_key = secret_key
        self._session   = None
        self._access    = None
        self._refresh   = None

    async def _authenticate(self):
        """POST api_key+secret_key → access+refresh tokens."""
        url     = self.BASE + "/api/v1/usr/authenticate/"
        payload = {"api_key": self.api_key, "secret_key": self.secret_key}
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, timeout=timeout)
        data = await resp.json()
        self._access  = data["access"]
        self._refresh = data["refresh"]

    async def _do_refresh(self):
        """Use refresh token to get a new access token."""
        url     = self.BASE + "/api/v1/usr/refresh_token/"
        payload = {"refresh": self._refresh}
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, timeout=timeout)
        data = await resp.json()
        self._access = data["access"]

    def _auth_headers(self):
        if self._access:
            return {"Authorization": "Bearer " + self._access}
        return {}

    async def get_orderbook(self, symbol, price_scale=1.0):
        """
        Public endpoint — no auth required.
        price_scale=10 for USDT_IRT: converts Toman to Rial for comparison
        with Nobitex/OmpFinex.
        Response: {"asks": [["170985","790.29"],...], "bids": [...]}
        """
        url     = self.BASE + "/api/v1/mth/orderbook/" + symbol + "/"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, timeout=timeout)
        data = await resp.json()

        def _parse(rows):
            out = []
            for x in rows:
                out.append([float(x[0]) * price_scale, float(x[1])])
            return out

        asks = sorted(_parse(data.get("asks", [])), key=lambda x: x[0])
        bids = sorted(_parse(data.get("bids", [])), key=lambda x: x[0], reverse=True)
        return OrderBook(exchange="bitpin", symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, symbol, side, base_amount, price, order_type="limit"):
        """side: 'buy' or 'sell'. price in Bitpin native units (Toman for USDT_IRT)."""
        if self._access is None:
            await self._authenticate()

        payload = {
            "symbol":      symbol,
            "type":        order_type,
            "side":        side,
            "price":       str(int(price)),
            "base_amount": str(base_amount),
        }
        url     = self.BASE + "/api/v1/odr/orders/"
        timeout = aiohttp.ClientTimeout(total=10)

        for attempt in range(2):
            resp = await self._session.post(
                url, json=payload, headers=self._auth_headers(), timeout=timeout
            )
            if resp.status == 401 and attempt == 0:
                await self._do_refresh()
                continue
            return await resp.json()

    async def get_wallets(self):
        if self._access is None:
            await self._authenticate()
        url     = self.BASE + "/api/v1/wlt/wallets/"
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, headers=self._auth_headers(), timeout=timeout)
        return await resp.json()


# ─────────────────────────────────────────────
#  RAMZINEX CLIENT
# ─────────────────────────────────────────────

class RamzinexClient(object):
    BASE_PUBLIC  = "https://publicapi.ramzinex.com/exchange/api/v1.0/exchange"
    BASE_PRIVATE = "https://api.ramzinex.com/exchange/api/v1.0/exchange"

    # currency_id mapping for wallet balance queries
    CURRENCY_ID = {
        "IRR": 2, "IRT": 2,
        "USDT": 9, "BTC": 1, "ETH": 3,
        "XRP": 6, "BNB": 10, "SOL": 81, "TON": 166,
    }

    def __init__(self, api_key, secret_key):
        self.api_key    = api_key
        self.secret_key = secret_key
        self._session   = None
        self._token     = None

    async def _authenticate(self):
        """POST api_key+secret → JWT token stored in self._token."""
        url     = self.BASE_PRIVATE + "/auth/api_key/getToken"
        payload = {"api_key": self.api_key, "secret": self.secret_key}
        timeout = aiohttp.ClientTimeout(total=10)
        resp = await self._session.post(url, json=payload, timeout=timeout)
        data = await resp.json()
        self._token = data["data"]["token"]

    def _auth_headers(self):
        headers = {"x-api-key": self.api_key}
        if self._token:
            headers["Authorization2"] = "Bearer " + self._token
        return headers

    async def get_orderbook(self, pair_id, price_scale=1.0):
        """
        Public endpoint — no auth required.
        price_scale=1.0 for all Ramzinex pairs (prices already in IRR/Rial).
        Response: {"data": {"buys": [[price, amount, ...], ...],
                             "sells": [[price, amount, ...], ...]}, "status": 0}
        buys  = bid-side (buy orders, descending by price)
        sells = ask-side (sell orders, ascending by price)
        """
        url     = self.BASE_PUBLIC + "/orderbooks/%d/buys_sells" % pair_id
        timeout = aiohttp.ClientTimeout(total=5)
        resp = await self._session.get(url, timeout=timeout)
        data = await resp.json()
        book = data.get("data", {})

        def _parse(rows):
            out = []
            for x in rows:
                out.append([float(x[0]) * price_scale, float(x[1])])
            return out

        bids = sorted(_parse(book.get("buys",  [])), key=lambda x: x[0], reverse=True)
        asks = sorted(_parse(book.get("sells", [])), key=lambda x: x[0])
        return OrderBook(exchange="ramzinex", symbol=str(pair_id), bids=bids, asks=asks)

    async def place_order(self, pair_id, side, amount, price):
        """side: 'buy' or 'sell'. price must be in Rial (IRR)."""
        if self._token is None:
            await self._authenticate()
        payload = {
            "pair_id": pair_id,
            "amount":  amount,
            "price":   int(price),
            "type":    side,
        }
        url     = self.BASE_PRIVATE + "/users/me/orders/limit"
        timeout = aiohttp.ClientTimeout(total=10)
        for attempt in range(2):
            resp = await self._session.post(
                url, json=payload, headers=self._auth_headers(), timeout=timeout
            )
            if resp.status == 401 and attempt == 0:
                await self._authenticate()
                continue
            return await resp.json()

    async def get_wallets(self):
        """Returns summaryDesktop data: list of {currency_id, total_nr, in_order_nr}."""
        if self._token is None:
            await self._authenticate()
        url     = self.BASE_PRIVATE + "/users/me/funds/summaryDesktop"
        timeout = aiohttp.ClientTimeout(total=5)
        for attempt in range(2):
            resp = await self._session.get(url, headers=self._auth_headers(), timeout=timeout)
            if resp.status == 401 and attempt == 0:
                await self._authenticate()
                continue
            return await resp.json()
        return {}


# ─────────────────────────────────────────────
#  ARBITRAGE ENGINE
# ─────────────────────────────────────────────

class ArbitrageEngine(object):
    PAIR_SUSPEND_SECONDS = 300   # suspend pair for 5 min after leg risk
    FLATTEN_SUSPEND_SECONDS = 1800  # suspend pair for 30 min if flatten also fails

    def __init__(self, nb, omf, wlx, btp, rmx, dry_run=True):
        self.nb      = nb
        self.omf     = omf
        self.wlx     = wlx
        self.btp     = btp
        self.rmx     = rmx
        self.dry_run = dry_run
        self.log     = logging.getLogger("arbit")
        self._pair_suspended = {}   # pair_name -> resume timestamp

    # ── fee helpers ─────────────────────────

    def _exchange_fee(self, exchange, market_type, role="taker"):
        if exchange == "nobitex":
            return NOBITEX_FEE[market_type][role]
        elif exchange == "ompfinex":
            return OMPFINEX_FEE["flat_" + role]
        elif exchange == "wallex":
            return WALLEX_FEE[role]
        elif exchange == "bitpin":
            return BITPIN_FEE[role]
        elif exchange == "ramzinex":
            return RAMZINEX_FEE[market_type][role]
        return 0.0

    def _transfer_pct(self, asset, source_exchange, amount):
        """Transfer cost as % of trade amount, amortized over the minimum transfer threshold."""
        fee_units = TRANSFER_FEE.get((asset, source_exchange), 0.0)
        if amount <= 0:
            return 0.0
        if asset == "USDT":
            # Amortize over MIN_USDT_TRANSFER_AMOUNT: fee is spread across multiple trades
            return (fee_units / MIN_USDT_TRANSFER_AMOUNT) * 100.0
        return (fee_units / amount) * 100.0

    def _irt_transfer_fee_irr(self, sell_ex, buy_ex, amount_irr):
        """IRT bank-transfer cost in IRR: withdraw from sell_ex + deposit at buy_ex."""
        withdrawal = IRT_WITHDRAWAL_FLAT_IRR.get(sell_ex, 0)
        deposit = (
            IRT_DEPOSIT_FLAT_IRR.get(buy_ex, 0)
            + int(IRT_DEPOSIT_FEE_PCT.get(buy_ex, 0.0) * amount_irr)
        )
        return withdrawal + deposit

    # ── profit calculation ───────────────────

    def evaluate(self, buy_ob, sell_ob, cfg):
        """
        Returns opportunity dict, or None if spread does not cross.
        net_irt:  profit/loss in IRT (Rial) — only for IRT-market pairs
        net_usdt: profit/loss in USDT
        effective_amount: min(trade_amount, ask_vol_at_best, bid_vol_at_best)
        """
        max_amount = TRADE_AMOUNT[cfg["amount_key"]]
        mtype      = cfg["market_type"]

        if not buy_ob.asks or not sell_ob.bids:
            return None

        ask     = buy_ob.best_ask()
        bid     = sell_ob.best_bid()
        ask_vol = buy_ob.best_ask_volume()
        bid_vol = sell_ob.best_bid_volume()

        if ask <= 0 or bid <= 0:
            return None

        # actual tradeable amount is limited by available liquidity at best level
        effective_amount = min(max_amount, ask_vol, bid_vol)
        if effective_amount <= 0:
            return None

        buy_fee  = self._exchange_fee(buy_ob.exchange,  mtype, "taker")
        sell_fee = self._exchange_fee(sell_ob.exchange, mtype, "taker")

        # transfer cost: withdraw asset from sell exchange to rebalance
        # calculated against effective_amount so pct is correct when size is limited
        transfer_pct = self._transfer_pct(cfg["transfer_asset"], sell_ob.exchange, effective_amount)

        eff_buy  = ask * (1.0 + buy_fee)
        eff_sell = bid * (1.0 - sell_fee)

        gross_pct     = (bid - ask) / ask * 100.0
        fee_total_pct = (buy_fee + sell_fee) * 100.0

        # IRT bank-transfer fee: move Rial from sell exchange back to buy exchange
        irt_fee_irr = 0
        irt_fee_pct = 0.0
        if mtype == "IRT":
            amount_irr   = effective_amount * ask
            # Calculate fee for a full MIN_IRT_TRANSFER_IRR batch, then amortize per trade
            full_irt_fee = self._irt_transfer_fee_irr(sell_ob.exchange, buy_ob.exchange, MIN_IRT_TRANSFER_IRR)
            irt_fee_irr  = full_irt_fee * amount_irr / MIN_IRT_TRANSFER_IRR
            irt_fee_pct  = irt_fee_irr / (effective_amount * eff_buy) * 100.0

        net_pct = (eff_sell - eff_buy) / eff_buy * 100.0 - transfer_pct - irt_fee_pct

        # exact profit in quote currency:
        #   spread_profit = amount × (eff_sell − eff_buy)
        #   transfer_cost = fee_units × ask  (fee_units = transfer_pct/100 × amount)
        spread_profit = effective_amount * (eff_sell - eff_buy)
        transfer_cost = (transfer_pct / 100.0) * effective_amount * ask

        if mtype == "IRT":
            net_irt  = spread_profit - transfer_cost - irt_fee_irr   # Rial
            net_usdt = net_irt / ask                                  # Rial ÷ (Rial/USDT) = USDT
        else:
            net_usdt = spread_profit - transfer_cost   # USDT
            net_irt  = 0.0                             # N/A (would need USDT/IRT rate)

        return {
            "pair":              cfg["name"],
            "buy_from":          buy_ob.exchange,
            "sell_to":           sell_ob.exchange,
            "buy_price":         ask,
            "sell_price":        bid,
            "amount":            effective_amount,
            "max_amount":        max_amount,
            "ask_vol":           ask_vol,
            "bid_vol":           bid_vol,
            "gross_pct":         gross_pct,
            "fee_total":         fee_total_pct,
            "transfer_pct":      transfer_pct,
            "irt_fee_pct":       irt_fee_pct,
            "irt_fee_irr":       irt_fee_irr,
            "net_pct":           net_pct,
            "net_irt":           net_irt,
            "net_usdt":          net_usdt,
            "market_type":       mtype,
            "liquidity_limited": effective_amount < max_amount,
        }

    # ── circuit breaker ──────────────────────

    def _suspend_pair(self, pair_name, seconds):
        resume = time.time() + seconds
        self._pair_suspended[pair_name] = resume
        self.log.warning(
            "SUSPENDED [%s] for %d min — resumes at %s",
            pair_name,
            seconds // 60,
            time.strftime("%H:%M:%S", time.localtime(resume)),
        )

    def _is_suspended(self, pair_name):
        resume = self._pair_suspended.get(pair_name, 0)
        if time.time() < resume:
            return True
        if resume:
            del self._pair_suspended[pair_name]
        return False

    # ── scan one pair ────────────────────────

    async def scan(self, cfg):
        if self._is_suspended(cfg["name"]):
            return None

        # Build fetch tasks only for exchanges that support this pair
        # (omf_symbol=None or bitpin_symbol=None means that exchange is skipped)
        tasks  = []
        labels = []

        if EXCHANGE_ENABLED.get("nobitex", True):
            tasks.append(self.nb.get_orderbook(cfg["nobitex_symbol"]))
            labels.append("nobitex")

        if cfg.get("omf_symbol") and EXCHANGE_ENABLED.get("ompfinex", True):
            tasks.append(self.omf.get_orderbook(cfg["omf_symbol"]))
            labels.append("ompfinex")

        if cfg.get("wallex_symbol") and EXCHANGE_ENABLED.get("wallex", True):
            tasks.append(self.wlx.get_orderbook(
                cfg["wallex_symbol"], price_scale=cfg.get("wallex_price_scale", 1.0)
            ))
            labels.append("wallex")

        if cfg.get("bitpin_symbol") and EXCHANGE_ENABLED.get("bitpin", True):
            tasks.append(self.btp.get_orderbook(
                cfg["bitpin_symbol"], price_scale=cfg.get("bitpin_price_scale", 1.0)
            ))
            labels.append("bitpin")

        if cfg.get("ramzinex_pair_id") and EXCHANGE_ENABLED.get("ramzinex", True):
            tasks.append(self.rmx.get_orderbook(cfg["ramzinex_pair_id"]))
            labels.append("ramzinex")

        # return_exceptions=True: a single exchange failure does not abort the others
        raw = await asyncio.gather(*tasks, return_exceptions=True)

        exchanges = []
        for label, result in zip(labels, raw):
            if isinstance(result, Exception):
                self.log.warning("[%s] %s orderbook unavailable: %s",
                                 cfg["name"], label, result)
            else:
                exchanges.append(result)

        if len(exchanges) < 2:
            self.log.error("[%s] only %d exchange(s) available, skipping scan",
                           cfg["name"], len(exchanges))
            return None

        best_candidate = None

        # evaluate all directed routes among available exchanges: buy on A, sell on B (A != B)
        for buy_ob in exchanges:
            for sell_ob in exchanges:
                if buy_ob.exchange == sell_ob.exchange:
                    continue
                opp = self.evaluate(buy_ob, sell_ob, cfg)
                if opp is None:
                    continue
                flag = "*** OPPORTUNITY ***" if (opp["net_pct"] >= MIN_PROFIT_PCT and opp["net_usdt"] >= MIN_PROFIT_USDT) else "."
                liq_tag = (
                    "  [liq=%.4g/%.4g]" % (opp["amount"], opp["max_amount"])
                    if opp["liquidity_limited"] else ""
                )
                if opp["market_type"] == "IRT":
                    self.log.info(
                        "%s [%s] %s->%s  ask=%.0f  bid=%.0f  "
                        "gross=%.2f%%  fees=%.2f%%  irt_f=%.3f%%  net=%.2f%%  "
                        "net_irt=%.0f  net_usdt=%.2f%s",
                        flag,
                        opp["pair"],
                        opp["buy_from"],
                        opp["sell_to"],
                        opp["buy_price"],
                        opp["sell_price"],
                        opp["gross_pct"],
                        opp["fee_total"],
                        opp["irt_fee_pct"],
                        opp["net_pct"],
                        opp["net_irt"],
                        opp["net_usdt"],
                        liq_tag,
                    )
                else:
                    self.log.info(
                        "%s [%s] %s->%s  ask=%.4f  bid=%.4f  "
                        "gross=%.2f%%  fees=%.2f%%  net=%.2f%%  "
                        "net_usdt=%.4f%s",
                        flag,
                        opp["pair"],
                        opp["buy_from"],
                        opp["sell_to"],
                        opp["buy_price"],
                        opp["sell_price"],
                        opp["gross_pct"],
                        opp["fee_total"],
                        opp["net_pct"],
                        opp["net_usdt"],
                        liq_tag,
                    )
                if opp["net_pct"] >= MIN_PROFIT_PCT and opp["net_usdt"] >= MIN_PROFIT_USDT:
                    if best_candidate is None or opp["net_pct"] > best_candidate["net_pct"]:
                        best_candidate = opp

        return best_candidate

    # ── balance helpers ──────────────────────

    async def _fetch_balance(self, exchange, currency):
        """
        Return available balance in the exchange's native unit.
        Use currency="IRT" for the Rial/Toman quote; "USDT"/"BTC"/etc for crypto.
        Nobitex: Rial wallet is "rls". Wallex: Toman wallet is "TMN".
        Returns 0.0 on any error so the caller can decide to abort.
        """
        try:
            if exchange == "nobitex":
                data = await self.nb.get_wallets()
                tag = "rls" if currency == "IRT" else currency.lower()
                for w in data.get("wallets", []):
                    if w.get("currency", "").lower() == tag:
                        return float(w.get("balance", 0))
            elif exchange == "ompfinex":
                data = await self.omf.get_wallets()
                tag = "IRR" if currency == "IRT" else currency.upper()
                for w in data.get("data", []):
                    if w.get("coin", "").upper() == tag:
                        return float(w.get("available", 0))
            elif exchange == "wallex":
                data = await self.wlx.get_wallets()
                tag = "TMN" if currency == "IRT" else currency.upper()
                balances = data.get("result", {}).get("balances", {})
                if tag in balances:
                    return float(balances[tag].get("value", 0))
            elif exchange == "bitpin":
                data = await self.btp.get_wallets()
                items = data if isinstance(data, list) else data.get("results", [])
                for w in items:
                    if w.get("currency", "").upper() == currency.upper():
                        return float(w.get("balance", 0))
            elif exchange == "ramzinex":
                data = await self.rmx.get_wallets()
                cid  = RamzinexClient.CURRENCY_ID.get(currency.upper(), -1)
                for w in data.get("data", []):
                    if w.get("currency_id") == cid:
                        total    = float(w.get("total_nr",    0))
                        in_order = float(w.get("in_order_nr", 0))
                        return max(0.0, total - in_order)
        except Exception as exc:
            self.log.warning("balance fetch [%s %s]: %s", exchange, currency, exc)
        return 0.0

    async def _check_balances(self, opp, cfg):
        """
        Pre-flight guard: verify both sides have sufficient funds before placing orders.
        Returns (True, "ok") or (False, reason_string).

        opp["buy_price"] is always in IRR. Wallex/Bitpin hold Toman balances,
        so divide by price_scale (10) to convert IRR price to Toman for those exchanges.
        """
        asset   = cfg["transfer_asset"]
        mtype   = cfg["market_type"]
        amount  = opp["amount"]
        buy_ex  = opp["buy_from"]
        sell_ex = opp["sell_to"]

        if mtype == "IRT":
            scale = 10.0 if buy_ex in ("wallex", "bitpin") else 1.0
            quote_needed = amount * opp["buy_price"] / scale * 1.005
            quote_cur    = "IRT"
        else:
            quote_needed = amount * opp["buy_price"] * 1.005
            quote_cur    = "USDT"

        quote_bal, asset_bal = await asyncio.gather(
            self._fetch_balance(buy_ex,  quote_cur),
            self._fetch_balance(sell_ex, asset),
        )

        if quote_bal < quote_needed:
            return False, "%s at %s: need %.2f  have %.2f" % (
                quote_cur, buy_ex, quote_needed, quote_bal)
        if asset_bal < amount:
            return False, "%s at %s: need %.6f  have %.6f" % (
                asset, sell_ex, amount, asset_bal)
        return True, "ok"

    async def _adjust_amount_for_balance(self, opp, cfg):
        """
        Checks actual wallet balances and returns a (possibly reduced) trade amount.
        Returns (amount, None) on success, or (None, reason_str) if balance is too low.

        Rules (applied only when DRY_RUN=False):
          - If balance >= TRADE_AMOUNT  → keep original amount unchanged.
          - If balance < TRADE_AMOUNT   → use balance × WALLET_USE_RATIO as the cap.
          - If capped amount < TRADE_AMOUNT × MIN_BALANCE_RATIO → abort (too small to trade).
        """
        asset     = cfg["transfer_asset"]
        mtype     = cfg["market_type"]
        amount    = opp["amount"]
        buy_ex    = opp["buy_from"]
        sell_ex   = opp["sell_to"]
        max_trade = TRADE_AMOUNT[cfg["amount_key"]]
        min_amount = max_trade * MIN_BALANCE_RATIO

        if mtype == "IRT":
            scale        = 10.0 if buy_ex in ("wallex", "bitpin") else 1.0
            quote_cur    = "IRT"
        else:
            scale        = 1.0
            quote_cur    = "USDT"

        quote_bal, asset_bal = await asyncio.gather(
            self._fetch_balance(buy_ex, quote_cur),
            self._fetch_balance(sell_ex, asset),
        )

        adjusted = amount
        quote_needed = amount * opp["buy_price"] / scale

        # Cap by buy-side quote balance
        if quote_bal < quote_needed:
            usable_quote = quote_bal * WALLET_USE_RATIO
            if mtype == "IRT":
                adjusted = min(adjusted, usable_quote * scale / opp["buy_price"])
            else:
                adjusted = min(adjusted, usable_quote / opp["buy_price"])

        # Cap by sell-side asset balance
        if asset_bal < adjusted:
            adjusted = min(adjusted, asset_bal * WALLET_USE_RATIO)

        if adjusted < min_amount:
            return None, (
                "%s@%s=%.6f  %s@%s=%.6f → adjusted=%.6f < floor=%.6f"
                % (quote_cur, buy_ex, quote_bal, asset, sell_ex, asset_bal,
                   adjusted, min_amount)
            )

        if adjusted < amount:
            self.log.warning(
                "balance-capped amount %.6f → %.6f  (%s@%s=%.4f  %s@%s=%.6f)",
                amount, adjusted,
                quote_cur, buy_ex, quote_bal, asset, sell_ex, asset_bal,
            )

        return adjusted, None

    # ── order placement helpers ───────────────

    async def _place_leg(self, exchange, side, amount, price, cfg):
        """
        Place one order leg. price is always in IRR; auto-scaled for wallex/bitpin.
        Returns raw API response dict. Raises on any network or API error.
        """
        ws = cfg.get("wallex_price_scale", 1.0)
        bs = cfg.get("bitpin_price_scale", 1.0)
        if exchange == "nobitex":
            return await self.nb.place_order(
                side, cfg["nb_src"], cfg["nb_dst"], amount, price)
        elif exchange == "ompfinex":
            return await self.omf.place_order(
                cfg["omf_symbol"], side, amount, price)
        elif exchange == "wallex":
            return await self.wlx.place_order(
                cfg["wallex_symbol"], side, amount, price / ws)
        elif exchange == "bitpin":
            return await self.btp.place_order(
                cfg["bitpin_symbol"], side, amount, price / bs)
        elif exchange == "ramzinex":
            return await self.rmx.place_order(
                cfg["ramzinex_pair_id"], side, amount, price)
        raise ValueError("Unknown exchange: %s" % exchange)

    def _order_accepted(self, exchange, response):
        """True if the API confirmed the order was accepted (not just HTTP-200)."""
        if isinstance(response, Exception):
            return False
        try:
            if exchange == "nobitex":
                return response.get("status") == "ok"
            elif exchange == "ompfinex":
                s = response.get("status")
                return s is True or s == "ok"
            elif exchange == "wallex":
                return response.get("success") is True
            elif exchange == "bitpin":
                return "id" in response
            elif exchange == "ramzinex":
                return (response.get("status") == 0
                        and "order_id" in response.get("data", {}))
        except Exception:
            pass
        return False

    def _order_id(self, exchange, response):
        """Extract order ID string from a successful place_order response."""
        try:
            if exchange == "nobitex":
                return str(response["order"]["id"])
            elif exchange == "ompfinex":
                return str(response["data"]["id"])
            elif exchange == "wallex":
                return str(response["result"]["clientOrderId"])
            elif exchange == "bitpin":
                return str(response["id"])
            elif exchange == "ramzinex":
                return str(response["data"]["order_id"])
        except (KeyError, TypeError, AttributeError):
            pass
        return None

    # ── rollback helpers ──────────────────────

    async def _cancel_order(self, exchange, order_id):
        """
        Best-effort cancellation of an open order.
        Called as the first rollback step, before flatten.
        An already-filled order will silently fail to cancel — that is expected.
        """
        if order_id is None:
            self.log.warning("cancel skipped [%s]: no order_id", exchange)
            return
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            if exchange == "nobitex":
                resp = await self.nb._session.post(
                    self.nb.BASE + "/market/orders/update-status",
                    json={"order": int(order_id), "status": "cancelled"},
                    headers=self.nb._auth(), timeout=timeout)
            elif exchange == "ompfinex":
                resp = await self.omf._session.delete(
                    self.omf.BASE + "/v1/user/orders/" + order_id,
                    headers=self.omf._auth(), timeout=timeout)
            elif exchange == "wallex":
                resp = await self.wlx._session.delete(
                    self.wlx.BASE + "/v1/account/orders/" + order_id,
                    headers=self.wlx._auth(), timeout=timeout)
            elif exchange == "bitpin":
                resp = await self.btp._session.delete(
                    self.btp.BASE + "/api/v1/odr/orders/" + order_id + "/",
                    headers=self.btp._auth_headers(), timeout=timeout)
            elif exchange == "ramzinex":
                resp = await self.rmx._session.post(
                    self.rmx.BASE_PRIVATE + "/users/me/orders/" + order_id + "/cancel",
                    headers=self.rmx._auth_headers(), timeout=timeout)
            else:
                self.log.error("cancel: unknown exchange %s", exchange)
                return
            data = await resp.json()
            self.log.info("cancel [%s] id=%s: %s", exchange, order_id, data)
        except Exception as exc:
            self.log.error("cancel failed [%s id=%s]: %s", exchange, order_id, exc)

    async def _flatten(self, exchange, filled_side, amount, cfg):
        """
        Emergency position flattening: place the opposite side at an aggressive price
        that crosses the spread to guarantee a fast fill.
        filled_side: the side that already executed (we reverse it here).

        If this also fails, logs CRITICAL with "MANUAL INTERVENTION REQUIRED".
        """
        opp_side = "sell" if filled_side == "buy" else "buy"
        self.log.critical("FLATTEN: placing %s %.6f on %s", opp_side, amount, exchange)
        try:
            ws = cfg.get("wallex_price_scale", 1.0)
            bs = cfg.get("bitpin_price_scale", 1.0)
            if exchange == "nobitex":
                ob = await self.nb.get_orderbook(cfg["nobitex_symbol"])
            elif exchange == "ompfinex":
                ob = await self.omf.get_orderbook(cfg["omf_symbol"])
            elif exchange == "wallex":
                ob = await self.wlx.get_orderbook(cfg["wallex_symbol"], price_scale=ws)
            elif exchange == "ramzinex":
                ob = await self.rmx.get_orderbook(cfg["ramzinex_pair_id"])
            else:
                ob = await self.btp.get_orderbook(cfg["bitpin_symbol"], price_scale=bs)

            # Cross the spread: sell below best bid, buy above best ask
            if opp_side == "sell":
                price = ob.best_bid() * 0.995
            else:
                price = ob.best_ask() * 1.005

            res = await self._place_leg(exchange, opp_side, amount, price, cfg)
            self.log.critical("FLATTEN result [%s]: %s", exchange, res)
            return True
        except Exception as exc:
            self.log.critical(
                "!!! FLATTEN FAILED [%s]: %s  *** MANUAL INTERVENTION REQUIRED ***",
                exchange, exc)
            return False

    # ── execute ──────────────────────────────

    async def execute(self, opp, cfg):
        if self.dry_run:
            self.log.warning(
                "[DRY RUN] Would execute: %s -> %s  net=%.2f%%  net_irt=%.0f  net_usdt=%.2f",
                opp["buy_from"], opp["sell_to"],
                opp["net_pct"], opp["net_irt"], opp["net_usdt"])
            _tlog(opp, True, "dry_run")
            return True

        # Step 1 — adjust trade amount for available wallet balance
        adjusted_amount, reason = await self._adjust_amount_for_balance(opp, cfg)
        if adjusted_amount is None:
            self.log.error("ABORTED (balance): %s", reason)
            _tlog(opp, False, "balance_fail", error_msg=reason)
            return False

        opp = dict(opp)
        opp["amount"] = adjusted_amount

        self.log.critical(
            "EXECUTING  %s  buy %.6f @ %.0f on %s  |  sell @ %.0f on %s"
            "  |  net=%.2f%%  net_irt=%.0f  net_usdt=%.2f",
            opp["pair"], opp["amount"], opp["buy_price"], opp["buy_from"],
            opp["sell_price"], opp["sell_to"],
            opp["net_pct"], opp["net_irt"], opp["net_usdt"],
        )

        # Step 2 — fire both legs in parallel (minimises timing gap between legs)
        buy_res, sell_res = await asyncio.gather(
            self._place_leg(opp["buy_from"], "buy",  opp["amount"], opp["buy_price"],  cfg),
            self._place_leg(opp["sell_to"],  "sell", opp["amount"], opp["sell_price"], cfg),
            return_exceptions=True,
        )

        buy_ok  = self._order_accepted(opp["buy_from"], buy_res)
        sell_ok = self._order_accepted(opp["sell_to"],  sell_res)

        # Step 3 — both legs accepted: normal happy path
        if buy_ok and sell_ok:
            self.log.info(
                "Both legs placed  buy_id=%s@%s  sell_id=%s@%s",
                self._order_id(opp["buy_from"], buy_res),  opp["buy_from"],
                self._order_id(opp["sell_to"],  sell_res), opp["sell_to"],
            )
            _tlog(opp, False, "executed",
                buy_order_id=self._order_id(opp["buy_from"], buy_res),
                sell_order_id=self._order_id(opp["sell_to"], sell_res))
            return True

        # Step 4 — both legs rejected: no open position, nothing to do
        if not buy_ok and not sell_ok:
            self.log.error(
                "Both legs rejected — no open position.  buy=%s  sell=%s",
                buy_res, sell_res)
            _tlog(opp, False, "both_failed",
                error_msg="buy=%s|sell=%s" % (buy_res, sell_res))
            return False

        # Step 5 — one leg succeeded, one failed: LEG RISK — rollback
        if buy_ok and not sell_ok:
            oid = self._order_id(opp["buy_from"], buy_res)
            self.log.critical(
                "!!! LEG RISK !!!  buy OK id=%s@%s  |  sell FAILED: %s",
                oid, opp["buy_from"], sell_res)
            _tlog(opp, False, "leg_risk",
                buy_order_id=oid, error_msg="sell_failed: %s" % sell_res)
            await self._cancel_order(opp["buy_from"], oid)
            await asyncio.sleep(0.5)
            flatten_ok = await self._flatten(opp["buy_from"], "buy", opp["amount"], cfg)
        else:
            oid = self._order_id(opp["sell_to"], sell_res)
            self.log.critical(
                "!!! LEG RISK !!!  sell OK id=%s@%s  |  buy FAILED: %s",
                oid, opp["sell_to"], buy_res)
            _tlog(opp, False, "leg_risk",
                sell_order_id=oid, error_msg="buy_failed: %s" % buy_res)
            await self._cancel_order(opp["sell_to"], oid)
            await asyncio.sleep(0.5)
            flatten_ok = await self._flatten(opp["sell_to"], "sell", opp["amount"], cfg)

        if flatten_ok:
            # Flatten placed — suspend pair briefly while position settles
            self._suspend_pair(cfg["name"], self.PAIR_SUSPEND_SECONDS)
        else:
            # Flatten also failed: open position unknown — suspend longer, alert operator
            self._suspend_pair(cfg["name"], self.FLATTEN_SUSPEND_SECONDS)
            self.log.critical(
                "!!! OPEN POSITION ON %s — PAIR SUSPENDED %d MIN — CHECK MANUALLY !!!",
                cfg["name"], self.FLATTEN_SUSPEND_SECONDS // 60)

        return False

    # ── main loop ────────────────────────────

    async def run(self):
        self.log.info("=" * 60)
        self.log.info(
            "Arbitrage started  dry_run=%s  threshold=%.2f%%  min_usdt=%.4f  max_trades=%s",
            self.dry_run, MIN_PROFIT_PCT, MIN_PROFIT_USDT,
            MAX_TRADES_PER_RUN if MAX_TRADES_PER_RUN > 0 else "unlimited",
        )
        self.log.info("Active exchanges: %s",
                      [k for k, v in EXCHANGE_ENABLED.items() if v])
        self.log.info("Active pairs: %s", [p["name"] for p in ACTIVE_PAIRS])
        self.log.info("=" * 60)

        conn    = aiohttp.TCPConnector(limit=10)
        session = aiohttp.ClientSession(connector=conn)
        self.nb._session  = session
        self.omf._session = session
        self.wlx._session = session
        self.btp._session = session
        self.rmx._session = session

        try:
            trades_done = 0
            while True:
                try:
                    results = await asyncio.gather(
                        *[self.scan(cfg) for cfg in ACTIVE_PAIRS],
                        return_exceptions=True,
                    )
                    for result, cfg in zip(results, ACTIVE_PAIRS):
                        if isinstance(result, Exception):
                            self.log.error("[%s] error: %s", cfg["name"], result)
                        elif result:
                            executed = await self.execute(result, cfg)
                            if executed:
                                trades_done += 1
                                if MAX_TRADES_PER_RUN > 0 and trades_done >= MAX_TRADES_PER_RUN:
                                    self.log.info(
                                        "MAX_TRADES_PER_RUN=%d reached — stopping.",
                                        MAX_TRADES_PER_RUN,
                                    )
                                    return

                except Exception as exc:
                    self.log.error("Main loop error: %s", exc)

                await asyncio.sleep(SCAN_INTERVAL)

        finally:
            await session.close()


# ─────────────────────────────────────────────
#  MINIMUM SPREAD ANALYSIS
# ─────────────────────────────────────────────

def print_min_spread_analysis():
    """
    For each pair, print the minimum spread needed for every exchange combination.
    transfer_pct = (transfer_fee_in_asset / trade_amount_in_asset) * 100
    """
    def _irt_pct(sell_ex, buy_ex):
        """IRT round-trip fee as % — amortized over MIN_IRT_TRANSFER_IRR."""
        w = IRT_WITHDRAWAL_FLAT_IRR.get(sell_ex, 0)
        d = IRT_DEPOSIT_FLAT_IRR.get(buy_ex, 0) + int(IRT_DEPOSIT_FEE_PCT.get(buy_ex, 0.0) * MIN_IRT_TRANSFER_IRR)
        return (w + d) / MIN_IRT_TRANSFER_IRR * 100.0

    def _usdt_tr(fee_usdt):
        """USDT transfer fee as % — amortized over MIN_USDT_TRANSFER_AMOUNT."""
        return fee_usdt / MIN_USDT_TRANSFER_AMOUNT * 100

    pairs = [
        {
            "name":   "USDT/TMN",
            "routes": [
                # (buy_ex, buy_fee%, sell_ex, sell_fee%, usdt_transfer_pct, irt_fee_pct)
                # usdt_transfer_pct amortized over MIN_USDT_TRANSFER_AMOUNT
                ("nobitex",  0.25, "ompfinex", 0.35, _usdt_tr(1.0), _irt_pct("ompfinex", "nobitex")),
                ("nobitex",  0.25, "wallex",   0.20, _usdt_tr(0.8), _irt_pct("wallex",   "nobitex")),
                ("nobitex",  0.25, "bitpin",   0.35, _usdt_tr(0.5), _irt_pct("bitpin",   "nobitex")),
                ("nobitex",  0.25, "ramzinex", 0.25, _usdt_tr(1.0), _irt_pct("ramzinex", "nobitex")),
                ("ompfinex", 0.35, "nobitex",  0.25, _usdt_tr(1.0), _irt_pct("nobitex",  "ompfinex")),
                ("ompfinex", 0.35, "wallex",   0.20, _usdt_tr(0.8), _irt_pct("wallex",   "ompfinex")),
                ("ompfinex", 0.35, "bitpin",   0.35, _usdt_tr(0.5), _irt_pct("bitpin",   "ompfinex")),
                ("ompfinex", 0.35, "ramzinex", 0.25, _usdt_tr(1.0), _irt_pct("ramzinex", "ompfinex")),
                ("wallex",   0.20, "nobitex",  0.25, _usdt_tr(1.0), _irt_pct("nobitex",  "wallex")),
                ("wallex",   0.20, "ompfinex", 0.35, _usdt_tr(1.0), _irt_pct("ompfinex", "wallex")),
                ("wallex",   0.20, "bitpin",   0.35, _usdt_tr(0.5), _irt_pct("bitpin",   "wallex")),
                ("wallex",   0.20, "ramzinex", 0.25, _usdt_tr(1.0), _irt_pct("ramzinex", "wallex")),
                ("bitpin",   0.35, "nobitex",  0.25, _usdt_tr(1.0), _irt_pct("nobitex",  "bitpin")),
                ("bitpin",   0.35, "ompfinex", 0.35, _usdt_tr(1.0), _irt_pct("ompfinex", "bitpin")),
                ("bitpin",   0.35, "wallex",   0.20, _usdt_tr(0.8), _irt_pct("wallex",   "bitpin")),
                ("bitpin",   0.35, "ramzinex", 0.25, _usdt_tr(1.0), _irt_pct("ramzinex", "bitpin")),
                ("ramzinex", 0.25, "nobitex",  0.25, _usdt_tr(1.0), _irt_pct("nobitex",  "ramzinex")),
                ("ramzinex", 0.25, "ompfinex", 0.35, _usdt_tr(1.0), _irt_pct("ompfinex", "ramzinex")),
                ("ramzinex", 0.25, "wallex",   0.20, _usdt_tr(0.8), _irt_pct("wallex",   "ramzinex")),
                ("ramzinex", 0.25, "bitpin",   0.35, _usdt_tr(0.5), _irt_pct("bitpin",   "ramzinex")),
            ],
        },
        {
            "name":   "BTC/USDT",
            "routes": [
                # (buy_ex, buy_fee%, sell_ex, sell_fee%, usdt_transfer_pct, irt_fee_pct)
                # BTC transfer: fee_btc / 0.05 BTC * 100
                ("nobitex",  0.13, "ompfinex", 0.35, 0.00005/0.05*100, 0),
                ("nobitex",  0.13, "wallex",   0.20, 0.00005/0.05*100, 0),
                ("nobitex",  0.13, "bitpin",   0.35, 0.003/0.05*100,   0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.00005/0.05*100, 0),
                ("ompfinex", 0.35, "nobitex",  0.13, 0.00005/0.05*100, 0),
                ("ompfinex", 0.35, "wallex",   0.20, 0.00005/0.05*100, 0),
                ("ompfinex", 0.35, "bitpin",   0.35, 0.003/0.05*100,   0),
                ("ompfinex", 0.35, "ramzinex", 0.10, 0.00005/0.05*100, 0),
                ("wallex",   0.20, "nobitex",  0.13, 0.00005/0.05*100, 0),
                ("wallex",   0.20, "ompfinex", 0.35, 0.00005/0.05*100, 0),
                ("wallex",   0.20, "bitpin",   0.35, 0.003/0.05*100,   0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.00005/0.05*100, 0),
                ("bitpin",   0.35, "nobitex",  0.13, 0.00005/0.05*100, 0),
                ("bitpin",   0.35, "ompfinex", 0.35, 0.00005/0.05*100, 0),
                ("bitpin",   0.35, "wallex",   0.20, 0.00005/0.05*100, 0),
                ("bitpin",   0.35, "ramzinex", 0.10, 0.00005/0.05*100, 0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.00005/0.05*100, 0),
                ("ramzinex", 0.10, "ompfinex", 0.35, 0.00005/0.05*100, 0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.00005/0.05*100, 0),
                ("ramzinex", 0.10, "bitpin",   0.35, 0.003/0.05*100,   0),
            ],
        },
        {
            "name":   "ETH/USDT",
            "routes": [
                # ETH transfer: fee_eth / 1 ETH * 100
                ("nobitex",  0.13, "ompfinex", 0.35, 0.0004/1.0*100, 0),
                ("nobitex",  0.13, "wallex",   0.20, 0.003/1.0*100,  0),
                ("nobitex",  0.13, "bitpin",   0.35, 0.015/1.0*100,  0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.0004/1.0*100, 0),
                ("ompfinex", 0.35, "nobitex",  0.13, 0.0004/1.0*100, 0),
                ("ompfinex", 0.35, "wallex",   0.20, 0.003/1.0*100,  0),
                ("ompfinex", 0.35, "bitpin",   0.35, 0.015/1.0*100,  0),
                ("ompfinex", 0.35, "ramzinex", 0.10, 0.0004/1.0*100, 0),
                ("wallex",   0.20, "nobitex",  0.13, 0.0004/1.0*100, 0),
                ("wallex",   0.20, "ompfinex", 0.35, 0.0004/1.0*100, 0),
                ("wallex",   0.20, "bitpin",   0.35, 0.015/1.0*100,  0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.003/1.0*100,  0),
                ("bitpin",   0.35, "nobitex",  0.13, 0.0004/1.0*100, 0),
                ("bitpin",   0.35, "ompfinex", 0.35, 0.0004/1.0*100, 0),
                ("bitpin",   0.35, "wallex",   0.20, 0.003/1.0*100,  0),
                ("bitpin",   0.35, "ramzinex", 0.10, 0.004/1.0*100,  0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.004/1.0*100,  0),
                ("ramzinex", 0.10, "ompfinex", 0.35, 0.004/1.0*100,  0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.004/1.0*100,  0),
                ("ramzinex", 0.10, "bitpin",   0.35, 0.004/1.0*100,  0),
            ],
        },
        {
            "name":   "BNB/USDT",
            "routes": [
                # BNB transfer: fee_bnb / 1 BNB * 100
                ("nobitex",  0.13, "ompfinex", 0.35, 0.001/1.0*100,  0),
                ("nobitex",  0.13, "wallex",   0.20, 0.0005/1.0*100, 0),
                ("nobitex",  0.13, "bitpin",   0.35, 0.001/1.0*100,  0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.001/1.0*100,  0),
                ("ompfinex", 0.35, "nobitex",  0.13, 0.001/1.0*100,  0),
                ("ompfinex", 0.35, "wallex",   0.20, 0.001/1.0*100,  0),
                ("ompfinex", 0.35, "bitpin",   0.35, 0.001/1.0*100,  0),
                ("ompfinex", 0.35, "ramzinex", 0.10, 0.001/1.0*100,  0),
                ("wallex",   0.20, "nobitex",  0.13, 0.0005/1.0*100, 0),
                ("wallex",   0.20, "ompfinex", 0.35, 0.0005/1.0*100, 0),
                ("wallex",   0.20, "bitpin",   0.35, 0.0005/1.0*100, 0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.0005/1.0*100, 0),
                ("bitpin",   0.35, "nobitex",  0.13, 0.001/1.0*100,  0),
                ("bitpin",   0.35, "ompfinex", 0.35, 0.001/1.0*100,  0),
                ("bitpin",   0.35, "wallex",   0.20, 0.001/1.0*100,  0),
                ("bitpin",   0.35, "ramzinex", 0.10, 0.001/1.0*100,  0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.001/1.0*100,  0),
                ("ramzinex", 0.10, "ompfinex", 0.35, 0.001/1.0*100,  0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.001/1.0*100,  0),
                ("ramzinex", 0.10, "bitpin",   0.35, 0.001/1.0*100,  0),
            ],
        },
        {
            "name":   "XRP/USDT",
            "routes": [
                # XRP transfer: fee_xrp / 500 XRP * 100
                ("nobitex",  0.13, "ompfinex", 0.35, 0.2/500*100, 0),
                ("nobitex",  0.13, "wallex",   0.20, 0.2/500*100, 0),
                ("nobitex",  0.13, "bitpin",   0.35, 0.2/500*100, 0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.2/500*100, 0),
                ("ompfinex", 0.35, "nobitex",  0.13, 0.2/500*100, 0),
                ("ompfinex", 0.35, "wallex",   0.20, 0.2/500*100, 0),
                ("ompfinex", 0.35, "bitpin",   0.35, 0.2/500*100, 0),
                ("ompfinex", 0.35, "ramzinex", 0.10, 0.2/500*100, 0),
                ("wallex",   0.20, "nobitex",  0.13, 0.2/500*100, 0),
                ("wallex",   0.20, "ompfinex", 0.35, 0.2/500*100, 0),
                ("wallex",   0.20, "bitpin",   0.35, 0.2/500*100, 0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.2/500*100, 0),
                ("bitpin",   0.35, "nobitex",  0.13, 0.2/500*100, 0),
                ("bitpin",   0.35, "ompfinex", 0.35, 0.2/500*100, 0),
                ("bitpin",   0.35, "wallex",   0.20, 0.2/500*100, 0),
                ("bitpin",   0.35, "ramzinex", 0.10, 0.2/500*100, 0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.2/500*100, 0),
                ("ramzinex", 0.10, "ompfinex", 0.35, 0.2/500*100, 0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.2/500*100, 0),
                ("ramzinex", 0.10, "bitpin",   0.35, 0.2/500*100, 0),
            ],
        },
        {
            "name":   "SOL/USDT",
            "routes": [
                # SOL transfer: fee_sol / 5 SOL * 100
                ("nobitex",  0.13, "ompfinex", 0.35, 0.01/5.0*100, 0),
                ("nobitex",  0.13, "wallex",   0.20, 0.01/5.0*100, 0),
                ("nobitex",  0.13, "bitpin",   0.35, 0.01/5.0*100, 0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.01/5.0*100, 0),
                ("ompfinex", 0.35, "nobitex",  0.13, 0.01/5.0*100, 0),
                ("ompfinex", 0.35, "wallex",   0.20, 0.01/5.0*100, 0),
                ("ompfinex", 0.35, "bitpin",   0.35, 0.01/5.0*100, 0),
                ("ompfinex", 0.35, "ramzinex", 0.10, 0.01/5.0*100, 0),
                ("wallex",   0.20, "nobitex",  0.13, 0.01/5.0*100, 0),
                ("wallex",   0.20, "ompfinex", 0.35, 0.01/5.0*100, 0),
                ("wallex",   0.20, "bitpin",   0.35, 0.01/5.0*100, 0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.01/5.0*100, 0),
                ("bitpin",   0.35, "nobitex",  0.13, 0.01/5.0*100, 0),
                ("bitpin",   0.35, "ompfinex", 0.35, 0.01/5.0*100, 0),
                ("bitpin",   0.35, "wallex",   0.20, 0.01/5.0*100, 0),
                ("bitpin",   0.35, "ramzinex", 0.10, 0.01/5.0*100, 0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.01/5.0*100, 0),
                ("ramzinex", 0.10, "ompfinex", 0.35, 0.01/5.0*100, 0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.01/5.0*100, 0),
                ("ramzinex", 0.10, "bitpin",   0.35, 0.01/5.0*100, 0),
            ],
        },
        {
            "name":   "TON/USDT",
            "routes": [
                # TON on Nobitex, Wallex, Ramzinex (OmpFinex + Bitpin excluded)
                # TON transfer: fee_ton / 100 TON * 100
                ("nobitex",  0.13, "wallex",   0.20, 0.1/100*100,  0),
                ("nobitex",  0.13, "ramzinex", 0.10, 0.1/100*100,  0),
                ("wallex",   0.20, "nobitex",  0.13, 0.02/100*100, 0),
                ("wallex",   0.20, "ramzinex", 0.10, 0.02/100*100, 0),
                ("ramzinex", 0.10, "nobitex",  0.13, 0.1/100*100,  0),
                ("ramzinex", 0.10, "wallex",   0.20, 0.1/100*100,  0),
            ],
        },
    ]

    sep = "-" * 86
    print("\n" + sep)
    print("  Minimum required spread for profitable arbitrage (5 exchanges)")
    print(sep)
    hdr = "  {:<11}  {:<10}  {:<10}  {:>8}  {:>9}  {:>8}  {:>9}"
    row = "  {:<11}  {:<10}  {:<10}  {:>7.3f}%  {:>8.3f}%  {:>7.3f}%  {:>8.3f}%"
    print(hdr.format("Pair", "Buy on", "Sell on", "Fees", "USDT xfr", "IRT xfr", "Min spr"))
    print(sep)

    for p in pairs:
        for buy_ex, buy_fee, sell_ex, sell_fee, tr_pct, irt_pct in p["routes"]:
            fees_total = buy_fee + sell_fee
            min_spr    = fees_total + tr_pct + irt_pct
            print(row.format(p["name"], buy_ex, sell_ex, fees_total, tr_pct, irt_pct, min_spr))
        print(sep)

    print("  NOTE: Bitpin USDT withdrawal = ~0.5 USDT BEP20 (verified bitpin.ir/fee). BTC/ETH fees need verification.")
    print("  NOTE: Ramzinex fees VERIFY at ramzinex.com/app/commissions (transfer fees estimated).")
    print("  NOTE: USDT xfr amortized over %g USDT; IRT xfr amortized over %g Toman." % (
        MIN_USDT_TRANSFER_AMOUNT, MIN_IRT_TRANSFER_IRR / 10))
    print(sep + "\n")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    print_min_spread_analysis()

    nb     = NobitexClient(api_key=NOBITEX_API_KEY)
    omf    = OmpFinexClient(api_key=OMPFINEX_API_KEY)
    wlx    = WallexClient(api_key=WALLEX_API_KEY)
    btp    = BitpinClient(api_key=BITPIN_API_KEY, secret_key=BITPIN_SECRET_KEY)
    rmx    = RamzinexClient(api_key=RAMZINEX_API_KEY, secret_key=RAMZINEX_SECRET)
    engine = ArbitrageEngine(nb, omf, wlx, btp, rmx, dry_run=DRY_RUN)

    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        print("\nStopped by user.")
