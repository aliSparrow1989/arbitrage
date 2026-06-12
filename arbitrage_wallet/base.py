#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
base.py — Shared building blocks for all exchange clients.

Contains:
  * OrderBook       — a normalized order book (bids/asks) used by every exchange.
  * ExchangeClient  — the abstract base every exchange client inherits from.

Each concrete exchange (nobitex.py, ompfinex.py, wallex.py, bitpin.py) only has
to implement how IT fetches an order book and places an order. Everything shared
(session handling, timeouts, sorting/parsing helpers) lives here so it is written
once and reused.
"""

import time

import aiohttp


# ─────────────────────────────────────────────
#  ORDER BOOK (normalized, identical for all exchanges)
# ─────────────────────────────────────────────

class OrderBook(object):
    """
    A normalized order book.

    bids: [[price, amount], ...]  best-bid first (descending by price)
    asks: [[price, amount], ...]  best-ask first (ascending by price)

    Prices are always stored in a COMMON unit (Rial for IRT markets, USDT for
    USDT markets) after applying each exchange's price_scale, so books from
    different exchanges are directly comparable.
    """

    def __init__(self, exchange, symbol, bids, asks):
        self.exchange = exchange
        self.symbol   = symbol
        self.bids     = bids
        self.asks     = asks
        self.ts       = time.time()

    def best_bid(self):
        return self.bids[0][0] if self.bids else 0.0

    def best_ask(self):
        return self.asks[0][0] if self.asks else float("inf")

    def best_bid_volume(self):
        return self.bids[0][1] if self.bids else 0.0

    def best_ask_volume(self):
        return self.asks[0][1] if self.asks else 0.0

    @staticmethod
    def sort_levels(levels):
        """Return (bids_desc, asks_asc) is NOT done here; helpers below are per-side."""
        raise NotImplementedError  # kept intentionally unused; see sort helpers below


# ── parsing / sorting helpers shared by every client ────────────────

def parse_levels(rows, price_scale=1.0):
    """
    Normalize a list of order-book rows into [[price, amount], ...].

    Accepts either:
      * list/tuple rows:  ["170103", "6.81"]  or  [170103, 6.81]
      * dict rows:        {"price": "...", "amount"/"quantity": "..."}

    price is multiplied by price_scale (e.g. Toman -> Rial uses 10.0).
    """
    out = []
    for x in rows:
        if isinstance(x, dict):
            price = float(x["price"]) * price_scale
            amount = float(x.get("amount", x.get("quantity", 0)))
            out.append([price, amount])
        elif isinstance(x, (list, tuple)):
            out.append([float(x[0]) * price_scale, float(x[1])])
    return out


def sort_asks(levels):
    """Ascending by price — best (lowest) ask first."""
    return sorted(levels, key=lambda r: r[0])


def sort_bids(levels):
    """Descending by price — best (highest) bid first."""
    return sorted(levels, key=lambda r: r[0], reverse=True)


# ── wallet-balance normalization helpers (shared by every client) ───
#
# Each exchange returns balances in its own shape, so normalization is
# best-effort: we probe a set of commonly-used field names and always fall
# back gracefully. The raw payload is kept separately by the engine, so even
# if a field name is missed here nothing is lost.

# field-name candidates, in priority order
_FREE_KEYS   = ("free", "available", "balance", "value", "active_balance",
                "activeBalance", "amount", "cash")
_LOCKED_KEYS = ("locked", "blocked", "blockedBalance", "blocked_balance",
                "frozen", "in_orders", "freeze")
_ASSET_KEYS  = ("asset", "currency", "symbol", "coin", "currencySymbol", "code")
_WRAPPER_KEYS = ("data", "results", "result", "wallets", "balances", "list", "funds")


def to_float(v):
    """Lenient float() — strings, None and junk all degrade to 0.0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _pick(d, keys, default=None):
    """Return the first present, non-None value among `keys`."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _asset_symbol(row):
    """Extract an upper-cased asset symbol from a balance row.

    The asset may be a plain string ("usdt") or a nested object
    ({"symbol": "usdt", ...}) depending on the exchange.
    """
    val = _pick(row, _ASSET_KEYS)
    if isinstance(val, dict):
        val = _pick(val, ("symbol", "code", "name", "id"))
    return str(val).upper() if val not in (None, "") else None


def _unwrap_rows(raw):
    """Drill through common wrapper keys (data/result/balances/...) until we
    reach the list of rows or the {ASSET: {...}} map that holds balances."""
    node = raw
    for _ in range(5):                      # bounded descent, avoids cycles
        if not isinstance(node, dict):
            return node
        nxt = None
        for k in _WRAPPER_KEYS:
            if isinstance(node.get(k), (list, dict)):
                nxt = node[k]
                break
        if nxt is None:
            return node                     # already an {ASSET: {...}} map
        node = nxt
    return node


def _accumulate(out, asset, row):
    free   = to_float(_pick(row, _FREE_KEYS, 0))
    locked = to_float(_pick(row, _LOCKED_KEYS, 0))
    entry = out.setdefault(asset, {"free": 0.0, "locked": 0.0, "total": 0.0})
    entry["free"]   += free
    entry["locked"] += locked
    entry["total"]   = entry["free"] + entry["locked"]


def normalize_wallet_rows(raw):
    """Best-effort: turn any exchange wallet response into
    {ASSET: {"free": float, "locked": float, "total": float}}.

    Handles both list-shaped responses ([{...}, ...]) and dict-shaped asset
    maps ({"USDT": {...}, ...}), wrapped under any of `_WRAPPER_KEYS`.
    Returns {} for anything it can't make sense of.
    """
    rows = _unwrap_rows(raw)
    out = {}
    if isinstance(rows, dict):
        for key, row in rows.items():
            if not isinstance(row, dict):
                continue
            asset = _asset_symbol(row) or str(key).upper()
            _accumulate(out, asset, row)
    elif isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            asset = _asset_symbol(row)
            if asset:
                _accumulate(out, asset, row)
    return out


# ─────────────────────────────────────────────
#  ABSTRACT EXCHANGE CLIENT
# ─────────────────────────────────────────────

class ExchangeClient(object):
    """
    Base class for all exchange clients.

    Subclasses MUST set:
      name : str            — short identifier ("nobitex", "wallex", ...)
      BASE : str            — API base URL

    Subclasses MUST implement:
      async def get_orderbook(self, symbol, price_scale=1.0) -> OrderBook
      async def place_order(self, ...) -> dict

    The shared aiohttp session is injected by the engine via `attach_session`,
    so all clients reuse one connection pool.
    """

    name = "base"
    BASE = ""

    # default per-request timeouts (seconds)
    READ_TIMEOUT  = 5
    TRADE_TIMEOUT = 10

    def __init__(self, api_key="", secret_key=""):
        self.api_key    = api_key
        self.secret_key = secret_key
        self._session   = None

    def attach_session(self, session):
        """Engine calls this to share one aiohttp session across all clients."""
        self._session = session

    # ── small HTTP helpers so subclasses stay short ──────────────

    def _timeout(self, total):
        return aiohttp.ClientTimeout(total=total)

    async def _get_json(self, url, params=None, headers=None, total=None):
        timeout = self._timeout(total or self.READ_TIMEOUT)
        resp = await self._session.get(url, params=params, headers=headers, timeout=timeout)
        return await resp.json()

    async def _post_json(self, url, payload=None, headers=None, total=None):
        timeout = self._timeout(total or self.TRADE_TIMEOUT)
        resp = await self._session.post(url, json=(payload or {}), headers=headers, timeout=timeout)
        return await resp.json()

    # ── interface every exchange must provide ────────────────────

    async def get_orderbook(self, symbol, price_scale=1.0):
        raise NotImplementedError

    async def place_order(self, *args, **kwargs):
        raise NotImplementedError

    async def get_wallets(self):
        raise NotImplementedError

    def normalize_wallets(self, raw):
        """Map this exchange's raw wallet response to the common schema
        {ASSET: {"free", "locked", "total"}}.

        The shared `normalize_wallet_rows` handles every current exchange's
        shape; override this in a subclass only if one needs special-casing.
        """
        return normalize_wallet_rows(raw)
