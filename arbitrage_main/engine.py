#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py — Arbitrage coordinator.

Reads ONE JSON config object from stdin, runs a single scan across all active
pairs on all five exchanges, and writes ONE JSON result object to stdout.
All logs go to stderr so stdout stays clean for n8n.

Run:
    echo '{...config...}' | python3 engine.py
    # or, with no stdin, it falls back to DEFAULT_CONFIG below.

n8n integration:
  * Put your fees, transfer fees, threshold, trade sizes, pair list and API
    keys into the JSON you pipe to stdin.
  * Read the JSON printed to stdout.

Config schema (every field optional — missing fields fall back to defaults):
{
  "dry_run": true,
  "min_profit_pct": 0.2,
  "min_profit_usdt": 0.04,
  "keys": {
    "nobitex":  {"api_key": "...", "secret_key": "..."},
    "ompfinex": {"api_key": "..."},
    "wallex":   {"api_key": "..."},
    "bitpin":   {"api_key": "...", "secret_key": "..."},
    "ramzinex": {"api_key": "...", "secret_key": "..."}
  },
  "fees": {
    "nobitex":  {"IRT": {"taker": 0.0025, "maker": 0.0025},
                 "USDT": {"taker": 0.0013, "maker": 0.0010}},
    "ompfinex": {"taker": 0.0035, "maker": 0.0035},
    "wallex":   {"taker": 0.0030, "maker": 0.0025},
    "bitpin":   {"taker": 0.0035, "maker": 0.0030},
    "ramzinex": {"IRT": {"taker": 0.0025, "maker": 0.0020},
                 "USDT": {"taker": 0.0010, "maker": 0.0010}}
  },
  "transfer_fees": {
    "USDT": {"nobitex": 0.7, "ompfinex": 0.7, "wallex": 0.8, "bitpin": 0.5},
    "BTC":  {"nobitex": 0.00005, ...},
    ...
  },
  "trade_amount": {"USDT_IRT": 500.0, "BTC_USDT": 0.05, ...},
  "pairs": [ { ...pair config... }, ... ]
}
"""

import asyncio
import json
import logging
import random
import sys
import time

import aiohttp

from nobitex import NobitexClient
from ompfinex import OmpFinexClient
from wallex import WallexClient
from bitpin import BitpinClient
from ramzinex import RamzinexClient


log = logging.getLogger("engine")


# ─────────────────────────────────────────────
#  DEFAULT CONFIG (used when stdin is empty)
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "dry_run":         True,
    "min_profit_pct":  0.2,
    "min_profit_usdt": 0.04,   # second gate: net profit in USDT must also clear this

    # Perturb each live order's size DOWNWARD by up to this percent so two
    # otherwise-identical orders fired in consecutive scans don't collide on the
    # exchange's duplicate-order guard (e.g. Nobitex rejects duplicates for 10s).
    # 0 disables it. amount_jitter_decimals = how many decimals the venue accepts.
    # amount_dedup_window_sec = avoid reusing a size for this long (> the
    # exchange's duplicate window) so no two recent orders share a size.
    "amount_jitter_pct":       1.0,
    "amount_jitter_decimals":  2,
    "amount_dedup_window_sec": 15.0,

    # which exchanges to scan; empty/missing => all five
    "exchanges": ["nobitex", "ompfinex", "wallex", "bitpin", "ramzinex"],

    "keys": {
        "nobitex":  {"api_key": "", "secret_key": ""},
        "ompfinex": {"api_key": ""},
        "wallex":   {"api_key": ""},
        "bitpin":   {"api_key": "", "secret_key": ""},
        "ramzinex": {"api_key": "", "secret_key": ""},
    },

    # exchange trading fees (fraction, not percent)
    "fees": {
        "nobitex":  {"IRT":  {"maker": 0.0017, "taker": 0.002},
                     "USDT": {"maker": 0.0010, "taker": 0.0013}},
        "ompfinex": {"maker": 0.0035, "taker": 0.0035},
        "wallex":   {"maker": 0.0025, "taker": 0.0030},
        "bitpin":   {"maker": 0.0030, "taker": 0.0035},
        "ramzinex": {"IRT":  {"maker": 0.0020, "taker": 0.0025},
                     "USDT": {"maker": 0.0010, "taker": 0.0010}},
    },

    # withdrawal/transfer fee in ASSET units, keyed by [asset][source_exchange]
    "transfer_fees": {
        "USDT": {"nobitex": 0.7,     "ompfinex": 0.7,     "wallex": 0.8,     "bitpin": 0.5,   "ramzinex": 0.8},
        "BTC":  {"nobitex": 0.00005, "ompfinex": 0.00005, "wallex": 0.00005, "bitpin": 0.003, "ramzinex": 0.00005},
        "ETH":  {"nobitex": 0.0004,  "ompfinex": 0.0004,  "wallex": 0.003,   "bitpin": 0.015, "ramzinex": 0.004},
        "BNB":  {"nobitex": 0.001,   "ompfinex": 0.001,   "wallex": 0.0005,  "bitpin": 0.001, "ramzinex": 0.001},
        "XRP":  {"nobitex": 0.2,     "ompfinex": 0.2,     "wallex": 0.2,     "bitpin": 0.2,   "ramzinex": 0.2},
        "SOL":  {"nobitex": 0.01,    "ompfinex": 0.01,    "wallex": 0.01,    "bitpin": 0.01,  "ramzinex": 0.01},
        "TON":  {"nobitex": 0.1,                          "wallex": 0.02,                     "ramzinex": 0.1},
    },

    "trade_amount": {
        "USDT_IRT": 15.0,
        "BTC_USDT": 0.05,
        "ETH_USDT": 1.0,
        "BNB_USDT": 1.0,
        "XRP_USDT": 500.0,
        "SOL_USDT": 5.0,
        "TON_USDT": 100.0,
    },

    # minimum batch sizes used to amortize transfer fees across multiple trades
    "min_usdt_transfer_amount": 500.0,
    "min_irt_transfer_irr":     1_000_000_000,

    # IRT bank-transfer fees (Rial) — withdrawal from sell side + deposit to buy side
    "irt_withdrawal_flat_irr": {
        "nobitex":  40000,
        "wallex":   80000,
        "ompfinex": 100000,
        "bitpin":   60000,
        "ramzinex": 60000,
    },
    "irt_deposit_fee_pct": {
        "nobitex":  0.0001,
        "wallex":   0.0001,
        "ompfinex": 0.0,
        "bitpin":   0.0,
        "ramzinex": 0.0,
    },
    "irt_deposit_flat_irr": {
        "nobitex":  0,
        "wallex":   0,
        "ompfinex": 0,
        "bitpin":   40000,
        "ramzinex": 0,
    },

    "pairs": [
        {
            "enabled":            True,
            "name":               "USDT/TMN",
            "nobitex_symbol":     "USDTIRT",
            "omf_symbol":         "USDTIRR",
            "wallex_symbol":      "USDTTMN",
            "wallex_price_scale": 10.0,
            "bitpin_symbol":      "USDT_IRT",
            "bitpin_price_scale": 10.0,
            "ramzinex_pair_id":   11,
            "market_type":        "IRT",
            "transfer_asset":     "USDT",
            "nb_src":             "usdt",
            "nb_dst":             "rls",
            "amount_key":         "USDT_IRT",
            # balance-aware sizing: the base asset you move across exchanges and
            # the quote currency you spend to buy it. quote_price_scale converts
            # the engine's price unit to the wallet's balance unit:
            #   IRT markets  -> price is Rial, balance is Toman  -> 0.1
            #   USDT markets -> price is USDT, balance is USDT    -> 1.0
            "base_asset":         "USDT",
            "quote_asset":        "IRT",
            "quote_price_scale":  0.1,
        },
        # add BTC/USDT, ETH/USDT, ... here or override the whole list from n8n
    ],
}


# ─────────────────────────────────────────────
#  ARBITRAGE ENGINE
# ─────────────────────────────────────────────

# Recently used sizes per route: {sig: [(timestamp, amount), ...]}. Kept at MODULE
# level so it survives across the fresh ArbitrageEngine that run() builds for every
# /run cycle — that's what lets a new order avoid EVERY size still inside the
# exchange's duplicate window (not just the immediately previous one).
_RECENT_JITTERED = {}


class ArbitrageEngine(object):
    def __init__(self, clients, cfg):
        """
        clients : dict {"nobitex": NobitexClient, "ompfinex": ..., ...}
        cfg     : the full parsed config dict
        """
        self.clients      = clients
        self.cfg          = cfg
        self.dry_run      = bool(cfg.get("dry_run", True))
        self.min_pct      = float(cfg.get("min_profit_pct", 0.2))
        self.min_usdt     = float(cfg.get("min_profit_usdt", 0.0))
        # safety buffer applied ONLY to balance-derived caps, so an order is not
        # rejected for a hair's-worth of insufficient funds after price/fee moves
        self.safety_factor = float(cfg.get("balance_safety_factor", 0.95))
        # size jitter to dodge exchange duplicate-order rejection (see _jitter_amount)
        self.amount_jitter_pct      = float(cfg.get("amount_jitter_pct", 0.0))
        self.amount_jitter_decimals = int(cfg.get("amount_jitter_decimals", 2))
        # avoid reusing a size for at least this long (must exceed the exchange's
        # duplicate window — Nobitex ignores duplicate orders for 10s)
        self.amount_dedup_window    = float(cfg.get("amount_dedup_window_sec", 15.0))
        self._jitter_rng = random.Random()
        # also fetch + apply balance caps during dry runs (logs the cap, never
        # places orders) — handy for validating sizing with real keys
        self.check_balances_dry = bool(cfg.get("check_balances_in_dry_run", False))
        self.exchanges    = cfg.get("exchanges") or ["nobitex", "ompfinex", "wallex", "bitpin", "ramzinex"]
        self.fees         = cfg.get("fees", {})
        self.tfees        = cfg.get("transfer_fees", {})
        self.amounts      = cfg.get("trade_amount", {})
        self.min_usdt_xfr = float(cfg.get("min_usdt_transfer_amount", 500.0))
        self.min_irt_xfr  = float(cfg.get("min_irt_transfer_irr", 1_000_000_000))
        self.irt_wd_flat  = cfg.get("irt_withdrawal_flat_irr", {})
        self.irt_dep_pct  = cfg.get("irt_deposit_fee_pct", {})
        self.irt_dep_flat = cfg.get("irt_deposit_flat_irr", {})
        self.log          = log

    # ── fee helpers (now data-driven from cfg, not hardcoded) ────

    def _exchange_fee(self, exchange, market_type, role="taker"):
        node = self.fees.get(exchange, {})
        # nobitex and ramzinex are nested by market type; others are flat
        if exchange in ("nobitex", "ramzinex"):
            return node.get(market_type, {}).get(role, 0.0)
        return node.get(role, 0.0)

    def _transfer_pct(self, asset, source_exchange, amount):
        fee_units = self.tfees.get(asset, {}).get(source_exchange, 0.0)
        if amount <= 0:
            return 0.0
        if asset == "USDT":
            return (fee_units / self.min_usdt_xfr) * 100.0
        return (fee_units / amount) * 100.0

    def _irt_transfer_fee_irr(self, sell_ex, buy_ex, amount_irr):
        withdrawal = self.irt_wd_flat.get(sell_ex, 0)
        deposit = (
            self.irt_dep_flat.get(buy_ex, 0)
            + int(self.irt_dep_pct.get(buy_ex, 0.0) * amount_irr)
        )
        return withdrawal + deposit

    # ── profit calculation ───────────────────────────────────────

    def evaluate(self, buy_ob, sell_ob, cfg):
        max_amount = self.amounts.get(cfg["amount_key"], 0.0)
        mtype      = cfg["market_type"]

        if not buy_ob.asks or not sell_ob.bids:
            return None

        ask     = buy_ob.best_ask()
        bid     = sell_ob.best_bid()
        ask_vol = buy_ob.best_ask_volume()
        bid_vol = sell_ob.best_bid_volume()

        if ask <= 0 or bid <= 0:
            return None

        effective_amount = min(max_amount, ask_vol, bid_vol)
        if effective_amount <= 0:
            return None

        buy_fee  = self._exchange_fee(buy_ob.exchange,  mtype, "taker")
        sell_fee = self._exchange_fee(sell_ob.exchange, mtype, "taker")

        opp = {
            "pair":              cfg["name"],
            "buy_from":          buy_ob.exchange,
            "sell_to":           sell_ob.exchange,
            "buy_price":         ask,
            "sell_price":        bid,
            "amount":            effective_amount,
            "max_amount":        max_amount,
            "ask_vol":           ask_vol,
            "bid_vol":           bid_vol,
            "gross_pct":         (bid - ask) / ask * 100.0,
            "fee_total":         (buy_fee + sell_fee) * 100.0,
            "market_type":       mtype,
            "balance_limited":   False,
        }
        # fill all amount-dependent fields (net_pct/net_usdt/...) from opp["amount"]
        return self._recompute_profit(opp, cfg)

    def _recompute_profit(self, opp, cfg):
        """(Re)compute every amount-dependent field in `opp` from opp["amount"],
        using opp's prices/exchanges/market_type. Mutates and returns opp.

        Shared by evaluate() (initial sizing) and apply_balance_limit() (after a
        balance cap shrinks the amount) so the profit math lives in one place.
        """
        amount  = opp["amount"]
        ask     = opp["buy_price"]
        bid     = opp["sell_price"]
        mtype   = opp["market_type"]
        buy_ex  = opp["buy_from"]
        sell_ex = opp["sell_to"]

        buy_fee  = self._exchange_fee(buy_ex,  mtype, "taker")
        sell_fee = self._exchange_fee(sell_ex, mtype, "taker")
        transfer_pct = self._transfer_pct(cfg["transfer_asset"], sell_ex, amount)

        eff_buy  = ask * (1.0 + buy_fee)
        eff_sell = bid * (1.0 - sell_fee)

        irt_fee_irr = 0
        irt_fee_pct = 0.0
        if mtype == "IRT":
            amount_irr   = amount * ask
            full_irt_fee = self._irt_transfer_fee_irr(sell_ex, buy_ex, self.min_irt_xfr)
            irt_fee_irr  = full_irt_fee * amount_irr / self.min_irt_xfr
            if amount > 0 and eff_buy > 0:
                irt_fee_pct = irt_fee_irr / (amount * eff_buy) * 100.0

        net_pct = (eff_sell - eff_buy) / eff_buy * 100.0 - transfer_pct - irt_fee_pct

        spread_profit = amount * (eff_sell - eff_buy)
        transfer_cost = (transfer_pct / 100.0) * amount * ask

        if mtype == "IRT":
            net_irt  = spread_profit - transfer_cost - irt_fee_irr
            net_usdt = net_irt / ask if ask else 0.0
        else:
            net_usdt = spread_profit - transfer_cost
            net_irt  = 0.0

        opp.update({
            "transfer_pct":      transfer_pct,
            "irt_fee_pct":       irt_fee_pct,
            "irt_fee_irr":       irt_fee_irr,
            "net_pct":           net_pct,
            "net_irt":           net_irt,
            "net_usdt":          net_usdt,
            "liquidity_limited": amount < opp["max_amount"],
        })
        return opp

    # ── balance-aware sizing (caps amount to what wallets can fund) ──

    async def fetch_leg_balances(self, opp):
        """Fetch FRESH free balances for the two exchanges of this opportunity,
        concurrently. Returns {exchange: {ASSET: free}}; an exchange that errors
        comes back as {} so the caller treats it as zero-funds."""
        legs = [opp["buy_from"], opp["sell_to"]]
        raw  = await asyncio.gather(
            *[self.clients[ex].get_balances() for ex in legs],
            return_exceptions=True,
        )
        out = {}
        for ex, result in zip(legs, raw):
            if isinstance(result, Exception):
                self.log.warning("[%s] could not fetch %s balances: %s",
                                 opp["pair"], ex, result)
                out[ex] = {}
            else:
                out[ex] = result
        return out

    def apply_balance_limit(self, opp, cfg, balances):
        """Shrink opp["amount"] to what wallet balances allow on BOTH legs and
        re-validate the profit gates. Returns (opp, None) on success, or
        (None, reason) — a Persian skip reason — if the opportunity can no
        longer be funded or no longer clears min_pct / min_usdt.

        Both legs are sized in the base asset:
          * buy  leg: spend quote on buy_ex  -> max base = quote_bal / buy_price
          * sell leg: sell base on sell_ex   -> max base = base_bal
        The 0.95 safety buffer is applied ONLY to these balance caps, so when
        funds are plentiful the size is unchanged from evaluate()'s sizing.
        """
        buy_ex  = opp["buy_from"]
        sell_ex = opp["sell_to"]
        base_asset  = cfg.get("base_asset") or cfg["transfer_asset"]
        quote_asset = cfg.get("quote_asset", "IRT" if opp["market_type"] == "IRT" else "USDT")
        quote_scale = float(cfg.get("quote_price_scale",
                                    0.1 if opp["market_type"] == "IRT" else 1.0))

        quote_bal = balances.get(buy_ex, {}).get(quote_asset, 0.0)
        base_bal  = balances.get(sell_ex, {}).get(base_asset, 0.0)

        buy_price = opp["buy_price"]
        # price is in engine units; * quote_scale converts it to the wallet's
        # quote-balance unit, so quote_bal / (price*scale) is in base-asset units
        buy_cap  = quote_bal / (buy_price * quote_scale) if buy_price > 0 and quote_scale > 0 else 0.0
        sell_cap = base_bal

        prev   = opp["amount"]
        capped = min(prev, self.safety_factor * buy_cap, self.safety_factor * sell_cap)

        if capped <= 0:
            self.log.warning(
                "[%s] %s->%s SKIP: no executable size  (buy needs %s on %s=%.6g, "
                "sell needs %s on %s=%.6g)",
                opp["pair"], buy_ex, sell_ex,
                quote_asset, buy_ex, quote_bal, base_asset, sell_ex, base_bal)
            return None, (
                "کمبود موجودی: سایز قابل‌اجرا صفر شد "
                f"(برای خرید به {quote_asset} در {buy_ex} نیاز است، موجودی={quote_bal:.6g}؛ "
                f"برای فروش به {base_asset} در {sell_ex} نیاز است، موجودی={base_bal:.6g})"
            )

        opp["amount"]          = capped
        opp["balance_limited"] = capped < prev
        self._recompute_profit(opp, cfg)

        if opp["net_pct"] < self.min_pct or opp["net_usdt"] < self.min_usdt:
            self.log.warning(
                "[%s] %s->%s SKIP after balance cap: amount=%.6g  net=%.3f%%  "
                "net_usdt=%.4f  below gate (min %.3f%% / %.4f)",
                opp["pair"], buy_ex, sell_ex, capped,
                opp["net_pct"], opp["net_usdt"], self.min_pct, self.min_usdt)
            return None, (
                "پس از محدود شدن سایز با موجودی، سود زیر حد آستانه افتاد: "
                f"سایز={capped:.6g}، net={opp['net_pct']:.3f}٪، net_usdt={opp['net_usdt']:.4f} "
                f"(حداقل لازم {self.min_pct:.3f}٪ و {self.min_usdt:.4f})"
            )

        if opp["balance_limited"]:
            self.log.info(
                "[%s] %s->%s balance-capped %.6g -> %.6g  (buy %s=%.6g, sell %s=%.6g)",
                opp["pair"], buy_ex, sell_ex, prev, capped,
                quote_asset, quote_bal, base_asset, base_bal)
        return opp, None

    # ── scan one pair across all available exchanges ─────────────

    async def scan(self, cfg):
        # (exchange_label, client, symbol, price_scale)
        plan = [
            ("nobitex",  self.clients["nobitex"],  cfg.get("nobitex_symbol"),   1.0),
            ("ompfinex", self.clients["ompfinex"], cfg.get("omf_symbol"),       1.0),
            ("wallex",   self.clients["wallex"],   cfg.get("wallex_symbol"),    cfg.get("wallex_price_scale", 1.0)),
            ("bitpin",   self.clients["bitpin"],   cfg.get("bitpin_symbol"),    cfg.get("bitpin_price_scale", 1.0)),
            ("ramzinex", self.clients["ramzinex"], cfg.get("ramzinex_pair_id"), 1.0),
        ]

        tasks, labels = [], []
        for label, client, symbol, scale in plan:
            if label not in self.exchanges:
                continue   # exchange disabled in config
            if not symbol:
                continue   # exchange does not list this pair
            tasks.append(client.get_orderbook(symbol, price_scale=scale))
            labels.append(label)

        raw = await asyncio.gather(*tasks, return_exceptions=True)

        exchanges = []
        for label, result in zip(labels, raw):
            if isinstance(result, Exception):
                self.log.warning("[%s] %s orderbook unavailable: %s", cfg["name"], label, result)
            else:
                exchanges.append(result)

        if len(exchanges) < 2:
            self.log.error("[%s] only %d exchange(s) available, skipping",
                           cfg["name"], len(exchanges))
            return None, []

        opportunities = []
        best_candidate = None

        for buy_ob in exchanges:
            for sell_ob in exchanges:
                if buy_ob.exchange == sell_ob.exchange:
                    continue
                opp = self.evaluate(buy_ob, sell_ob, cfg)
                if opp is None:
                    continue

                qualifies = opp["net_pct"] >= self.min_pct and opp["net_usdt"] >= self.min_usdt
                flag    = "*** OPPORTUNITY ***" if qualifies else "."
                liq_tag = (
                    "  [liq=%.4g/%.4g]" % (opp["amount"], opp["max_amount"])
                    if opp["liquidity_limited"] else ""
                )
                if opp["market_type"] == "IRT":
                    self.log.info(
                        "%s [%s] %s->%s  ask=%.0f  bid=%.0f  "
                        "gross=%.2f%%  fees=%.2f%%  irt_f=%.3f%%  net=%.2f%%  "
                        "net_irt=%.0f  net_usdt=%.2f%s",
                        flag, opp["pair"], opp["buy_from"], opp["sell_to"],
                        opp["buy_price"], opp["sell_price"],
                        opp["gross_pct"], opp["fee_total"], opp["irt_fee_pct"],
                        opp["net_pct"], opp["net_irt"], opp["net_usdt"], liq_tag,
                    )
                else:
                    self.log.info(
                        "%s [%s] %s->%s  ask=%.4f  bid=%.4f  "
                        "gross=%.2f%%  fees=%.2f%%  net=%.2f%%  net_usdt=%.4f%s",
                        flag, opp["pair"], opp["buy_from"], opp["sell_to"],
                        opp["buy_price"], opp["sell_price"],
                        opp["gross_pct"], opp["fee_total"],
                        opp["net_pct"], opp["net_usdt"], liq_tag,
                    )

                if qualifies:
                    opportunities.append(opp)
                    if best_candidate is None or opp["net_pct"] > best_candidate["net_pct"]:
                        best_candidate = opp

        return best_candidate, opportunities

    # ── execute (places real orders only when dry_run is False) ──

    def _jitter_amount(self, opp, cfg):
        """Nudge opp["amount"] DOWNWARD by up to amount_jitter_pct so this order's
        size differs from the previous one on the same route. Exchanges such as
        Nobitex reject an identical order (same side+price+amount) for ~10s
        ("DuplicateOrder"); when that hit only the buy leg, the sell leg still
        filled and left a one-sided position (see execute.txt). A unique size
        sidesteps the guard. Jittering DOWN (never up) keeps the size inside the
        liquidity/balance caps already applied, and the SAME value is used for
        both legs so the hedge stays matched. Re-rolls until the size differs from
        EVERY size used on this route within amount_dedup_window seconds — so it
        can't collide with the previous order *or* the one before it while either
        is still inside the exchange's duplicate window.

        Mutates opp["amount"] and refreshes the amount-dependent profit fields so
        the recorded execution reflects what was actually sent."""
        base = opp["amount"]
        if self.amount_jitter_pct <= 0 or base <= 0:
            return
        sig  = (opp["pair"], opp["buy_from"], opp["sell_to"])
        span = base * self.amount_jitter_pct / 100.0
        now  = time.time()

        # drop sizes that have aged out of the duplicate window, block the rest
        recent  = [(ts, amt) for ts, amt in _RECENT_JITTERED.get(sig, [])
                   if now - ts < self.amount_dedup_window]
        blocked = {amt for _, amt in recent}

        jittered = base
        for _ in range(20):
            cand = round(base - self._jitter_rng.uniform(0.0, span),
                         self.amount_jitter_decimals)
            if cand > 0 and cand not in blocked:
                jittered = cand
                break

        recent.append((now, jittered))
        _RECENT_JITTERED[sig] = recent
        opp["amount"] = jittered
        self._recompute_profit(opp, cfg)

    async def execute(self, opp, cfg):
        if self.dry_run:
            self.log.warning("[DRY RUN] would execute %s -> %s  net=%.3f%%  net_usdt=%.4f",
                             opp["buy_from"], opp["sell_to"], opp["net_pct"], opp["net_usdt"])
            return {"executed": False, "dry_run": True}

        # unique size per cycle so the order isn't rejected as a duplicate
        self._jitter_amount(opp, cfg)

        wallex_scale = cfg.get("wallex_price_scale", 1.0)
        bitpin_scale = cfg.get("bitpin_price_scale", 1.0)
        out = {"executed": True, "buy": None, "sell": None}

        try:
            if opp["buy_from"] == "nobitex":
                out["buy"] = await self.clients["nobitex"].place_order(
                    "buy", cfg["nb_src"], cfg["nb_dst"], opp["amount"], opp["buy_price"])
            elif opp["buy_from"] == "ompfinex":
                out["buy"] = await self.clients["ompfinex"].place_order(
                    cfg["omf_symbol"], "buy", opp["amount"], opp["buy_price"])
            elif opp["buy_from"] == "wallex":
                out["buy"] = await self.clients["wallex"].place_order(
                    cfg["wallex_symbol"], "buy", opp["amount"], opp["buy_price"] / wallex_scale)
            elif opp["buy_from"] == "bitpin":
                out["buy"] = await self.clients["bitpin"].place_order(
                    cfg["bitpin_symbol"], "buy", opp["amount"], opp["buy_price"] / bitpin_scale)
            elif opp["buy_from"] == "ramzinex":
                out["buy"] = await self.clients["ramzinex"].place_order(
                    cfg["ramzinex_pair_id"], "buy", opp["amount"], opp["buy_price"])

            if opp["sell_to"] == "nobitex":
                out["sell"] = await self.clients["nobitex"].place_order(
                    "sell", cfg["nb_src"], cfg["nb_dst"], opp["amount"], opp["sell_price"])
            elif opp["sell_to"] == "ompfinex":
                out["sell"] = await self.clients["ompfinex"].place_order(
                    cfg["omf_symbol"], "sell", opp["amount"], opp["sell_price"])
            elif opp["sell_to"] == "wallex":
                out["sell"] = await self.clients["wallex"].place_order(
                    cfg["wallex_symbol"], "sell", opp["amount"], opp["sell_price"] / wallex_scale)
            elif opp["sell_to"] == "bitpin":
                out["sell"] = await self.clients["bitpin"].place_order(
                    cfg["bitpin_symbol"], "sell", opp["amount"], opp["sell_price"] / bitpin_scale)
            elif opp["sell_to"] == "ramzinex":
                out["sell"] = await self.clients["ramzinex"].place_order(
                    cfg["ramzinex_pair_id"], "sell", opp["amount"], opp["sell_price"])

        except Exception as exc:
            self.log.error("order placement failed: %s", exc)
            out["executed"] = False
            out["error"] = str(exc)

        return out

    # ── single scan across all active pairs ──────────────────────

    async def scan_once(self, active_pairs):
        self.log.info("scan start  dry_run=%s  threshold=%.3f%%  pairs=%s",
                      self.dry_run, self.min_pct, [p["name"] for p in active_pairs])

        results = await asyncio.gather(
            *[self.scan(cfg) for cfg in active_pairs],
            return_exceptions=True,
        )

        opportunities = []
        executions    = []
        skipped       = []   # opportunity found but no real buy — reason in Persian
        exceptions    = []   # a pair's scan crashed outright

        for result, cfg in zip(results, active_pairs):
            if isinstance(result, Exception):
                self.log.error("[%s] error: %s", cfg["name"], result)
                exceptions.append({"pair": cfg["name"], "exception": str(result)})
                continue
            best, all_opps = result
            opportunities.extend(all_opps)
            if not best:
                continue   # no qualifying opportunity at all -> nothing to skip

            # Cap the trade to what BOTH exchanges' wallets can actually fund,
            # using FRESH balances fetched right now (only for the two legs of
            # this opportunity). This prevents the one-sided fill that happens
            # when one leg is rejected for insufficient balance. Balances are
            # fetched per-execution, not per-scan, to keep request volume low.
            if not self.dry_run or self.check_balances_dry:
                leg_balances = await self.fetch_leg_balances(best)
                capped, skip_reason = self.apply_balance_limit(best, cfg, leg_balances)
                if capped is None:
                    skipped.append({
                        "pair":     cfg["name"],
                        "buy_from": best["buy_from"],
                        "sell_to":  best["sell_to"],
                        "reason":   skip_reason,
                    })
                    continue   # not fundable / no longer profitable -> skip
                best = capped

            ex = await self.execute(best, cfg)
            executions.append({"pair": cfg["name"], "opportunity": best, "result": ex})

            # If the execution did not place a real buy, record why (in Persian).
            if not ex.get("executed"):
                if ex.get("dry_run"):
                    reason = "حالت آزمایشی (dry_run) فعال است؛ سفارش واقعی ثبت نشد"
                elif ex.get("error"):
                    reason = f"خطا هنگام ثبت سفارش: {ex['error']}"
                else:
                    reason = f"execution did not place an order: {ex}"
                skipped.append({
                    "pair":     cfg["name"],
                    "buy_from": best["buy_from"],
                    "sell_to":  best["sell_to"],
                    "reason":   reason,
                })

        return {
            "timestamp":     time.time(),
            "dry_run":       self.dry_run,
            "threshold_pct": self.min_pct,
            "active_pairs":  [p["name"] for p in active_pairs],
            "opportunities": opportunities,
            "executions":    executions,
            "skipped":       skipped,
            "exceptions":    exceptions,
            "count":         len(opportunities),
        }


# ─────────────────────────────────────────────
#  CONFIG MERGE + CLIENT BUILDING
# ─────────────────────────────────────────────

def deep_merge(base, override):
    """Recursively merge override into base (override wins). Lists are replaced."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def build_clients(cfg):
    keys = cfg.get("keys", {})
    nb_k = keys.get("nobitex", {})
    om_k = keys.get("ompfinex", {})
    wl_k = keys.get("wallex", {})
    bp_k = keys.get("bitpin", {})
    rx_k = keys.get("ramzinex", {})
    return {
        "nobitex":  NobitexClient(api_key=nb_k.get("api_key", ""),
                                  secret_key=nb_k.get("secret_key", "")),
        "ompfinex": OmpFinexClient(api_key=om_k.get("api_key", "")),
        "wallex":   WallexClient(api_key=wl_k.get("api_key", "")),
        "bitpin":   BitpinClient(api_key=bp_k.get("api_key", ""),
                                 secret_key=bp_k.get("secret_key", "")),
        "ramzinex": RamzinexClient(api_key=rx_k.get("api_key", ""),
                                   secret_key=rx_k.get("secret_key", "")),
    }


async def run(cfg):
    clients = build_clients(cfg)
    engine  = ArbitrageEngine(clients, cfg)

    active_pairs = [p for p in cfg.get("pairs", []) if p.get("enabled", False)]
    if not active_pairs:
        return {
            "timestamp": time.time(),
            "exception": "no active pairs (set enabled=true on at least one pair)",
            "opportunities": [], "executions": [], "skipped": [], "exceptions": [], "count": 0,
        }

    conn    = aiohttp.TCPConnector(limit=10)
    session = aiohttp.ClientSession(connector=conn)
    for c in clients.values():
        c.attach_session(session)

    try:
        return await engine.scan_once(active_pairs)
    finally:
        await session.close()


# ─────────────────────────────────────────────
#  ENTRY POINT — stdin (JSON) -> stdout (JSON)
# ─────────────────────────────────────────────

def load_config_from_stdin():
    """Read JSON config from stdin; fall back to DEFAULT_CONFIG if empty/invalid."""
    raw = ""
    if not sys.stdin.isatty():
        raw = sys.stdin.read()
    raw = raw.strip()
    if not raw:
        return DEFAULT_CONFIG, None
    try:
        user_cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        return DEFAULT_CONFIG, "invalid stdin JSON: %s" % exc
    return deep_merge(DEFAULT_CONFIG, user_cfg), None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,   # quiet by default; result goes to stdout as JSON
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg, cfg_error = load_config_from_stdin()
    if cfg_error:
        log.warning(cfg_error + "  — falling back to defaults")

    result = asyncio.run(run(cfg))
    print(json.dumps(result, ensure_ascii=False))
