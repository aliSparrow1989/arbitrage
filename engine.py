#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py — Arbitrage coordinator.

Reads ONE JSON config object from stdin, runs a single scan across all active
pairs on all four exchanges, and writes ONE JSON result object to stdout.
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
  "min_profit_pct": 0.15,
  "keys": {
    "nobitex":  {"api_key": "..."},
    "ompfinex": {"api_key": "..."},
    "wallex":   {"api_key": "..."},
    "bitpin":   {"api_key": "...", "secret_key": "..."}
  },
  "fees": {
    "nobitex":  {"IRT": {"taker": 0.0025, "maker": 0.0025},
                 "USDT": {"taker": 0.0013, "maker": 0.0010}},
    "ompfinex": {"taker": 0.0035, "maker": 0.0035},
    "wallex":   {"taker": 0.0020, "maker": 0.0020},
    "bitpin":   {"taker": 0.0035, "maker": 0.0030}
  },
  "transfer_fees": {
    "USDT": {"nobitex": 1.0, "ompfinex": 1.0, "wallex": 0.8, "bitpin": 0.5},
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
import sys
import time

import aiohttp

from nobitex import NobitexClient
from ompfinex import OmpFinexClient
from wallex import WallexClient
from bitpin import BitpinClient


log = logging.getLogger("engine")


# ─────────────────────────────────────────────
#  DEFAULT CONFIG (used when stdin is empty)
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "dry_run":        True,
    "min_profit_pct": 0.15,

    "keys": {
        "nobitex":  {"api_key": ""},
        "ompfinex": {"api_key": ""},
        "wallex":   {"api_key": ""},
        "bitpin":   {"api_key": "", "secret_key": ""},
    },

    # exchange trading fees (fraction, not percent)
    "fees": {
        "nobitex":  {"IRT":  {"maker": 0.0025, "taker": 0.0025},
                     "USDT": {"maker": 0.0010, "taker": 0.0013}},
        "ompfinex": {"maker": 0.0035, "taker": 0.0035},
        "wallex":   {"maker": 0.0020, "taker": 0.0020},
        "bitpin":   {"maker": 0.0030, "taker": 0.0035},
    },

    # withdrawal/transfer fee in ASSET units, keyed by [asset][source_exchange]
    "transfer_fees": {
        "USDT": {"nobitex": 1.0,     "ompfinex": 1.0,     "wallex": 0.8,     "bitpin": 0.5},
        "BTC":  {"nobitex": 0.00005, "ompfinex": 0.00005, "wallex": 0.00005, "bitpin": 0.003},
        "ETH":  {"nobitex": 0.0004,  "ompfinex": 0.0004,  "wallex": 0.003,   "bitpin": 0.015},
        "BNB":  {"nobitex": 0.001,   "ompfinex": 0.001,   "wallex": 0.0005,  "bitpin": 0.001},
        "XRP":  {"nobitex": 0.2,     "ompfinex": 0.2,     "wallex": 0.2,     "bitpin": 0.2},
        "SOL":  {"nobitex": 0.01,    "ompfinex": 0.01,    "wallex": 0.01,    "bitpin": 0.01},
        "TON":  {"nobitex": 0.1,                          "wallex": 0.02},
    },

    "trade_amount": {
        "USDT_IRT": 500.0,
        "BTC_USDT": 0.05,
        "ETH_USDT": 1.0,
        "BNB_USDT": 1.0,
        "XRP_USDT": 500.0,
        "SOL_USDT": 5.0,
        "TON_USDT": 100.0,
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
            "market_type":        "IRT",
            "transfer_asset":     "USDT",
            "nb_src":             "usdt",
            "nb_dst":             "rls",
            "amount_key":         "USDT_IRT",
        },
        # add BTC/USDT, ETH/USDT, ... here or override the whole list from n8n
    ],
}


# ─────────────────────────────────────────────
#  ARBITRAGE ENGINE
# ─────────────────────────────────────────────

class ArbitrageEngine(object):
    def __init__(self, clients, cfg):
        """
        clients : dict {"nobitex": NobitexClient, "ompfinex": ..., ...}
        cfg     : the full parsed config dict
        """
        self.clients = clients
        self.cfg     = cfg
        self.dry_run = bool(cfg.get("dry_run", True))
        self.min_pct = float(cfg.get("min_profit_pct", 0.15))
        self.fees    = cfg.get("fees", {})
        self.tfees   = cfg.get("transfer_fees", {})
        self.amounts = cfg.get("trade_amount", {})
        self.log     = log

    # ── fee helpers (now data-driven from cfg, not hardcoded) ────

    def _exchange_fee(self, exchange, market_type, role="taker"):
        node = self.fees.get(exchange, {})
        # nobitex is nested by market type; others are flat
        if exchange == "nobitex":
            return node.get(market_type, {}).get(role, 0.0)
        return node.get(role, 0.0)

    def _transfer_pct(self, asset, source_exchange, amount):
        fee_units = self.tfees.get(asset, {}).get(source_exchange, 0.0)
        if amount <= 0:
            return 0.0
        return (fee_units / amount) * 100.0

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
        transfer_pct = self._transfer_pct(cfg["transfer_asset"], sell_ob.exchange, effective_amount)

        eff_buy  = ask * (1.0 + buy_fee)
        eff_sell = bid * (1.0 - sell_fee)

        gross_pct     = (bid - ask) / ask * 100.0
        fee_total_pct = (buy_fee + sell_fee) * 100.0
        net_pct       = (eff_sell - eff_buy) / eff_buy * 100.0 - transfer_pct

        spread_profit = effective_amount * (eff_sell - eff_buy)
        transfer_cost = (transfer_pct / 100.0) * effective_amount * ask

        if mtype == "IRT":
            net_irt  = spread_profit - transfer_cost
            net_usdt = net_irt / ask if ask else 0.0
        else:
            net_usdt = spread_profit - transfer_cost
            net_irt  = 0.0

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
            "net_pct":           net_pct,
            "net_irt":           net_irt,
            "net_usdt":          net_usdt,
            "market_type":       mtype,
            "liquidity_limited": effective_amount < max_amount,
        }

    # ── scan one pair across all available exchanges ─────────────

    async def scan(self, cfg):
        # (exchange_label, client, symbol, price_scale)
        plan = [
            ("nobitex",  self.clients["nobitex"],  cfg.get("nobitex_symbol"), 1.0),
            ("ompfinex", self.clients["ompfinex"], cfg.get("omf_symbol"),     1.0),
            ("wallex",   self.clients["wallex"],   cfg.get("wallex_symbol"),  cfg.get("wallex_price_scale", 1.0)),
            ("bitpin",   self.clients["bitpin"],   cfg.get("bitpin_symbol"),  cfg.get("bitpin_price_scale", 1.0)),
        ]

        tasks, labels = [], []
        for label, client, symbol, scale in plan:
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
                if opp["net_pct"] >= self.min_pct:
                    opportunities.append(opp)
                    if best_candidate is None or opp["net_pct"] > best_candidate["net_pct"]:
                        best_candidate = opp

        return best_candidate, opportunities

    # ── execute (places real orders only when dry_run is False) ──

    async def execute(self, opp, cfg):
        if self.dry_run:
            self.log.warning("[DRY RUN] would execute %s -> %s  net=%.3f%%  net_usdt=%.4f",
                             opp["buy_from"], opp["sell_to"], opp["net_pct"], opp["net_usdt"])
            return {"executed": False, "dry_run": True}

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
        errors        = []

        for result, cfg in zip(results, active_pairs):
            if isinstance(result, Exception):
                self.log.error("[%s] error: %s", cfg["name"], result)
                errors.append({"pair": cfg["name"], "error": str(result)})
                continue
            best, all_opps = result
            opportunities.extend(all_opps)
            if best:
                ex = await self.execute(best, cfg)
                executions.append({"pair": cfg["name"], "opportunity": best, "result": ex})

        return {
            "timestamp":     time.time(),
            "dry_run":       self.dry_run,
            "threshold_pct": self.min_pct,
            "active_pairs":  [p["name"] for p in active_pairs],
            "opportunities": opportunities,
            "executions":    executions,
            "errors":        errors,
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
    return {
        "nobitex":  NobitexClient(api_key=nb_k.get("api_key", "")),
        "ompfinex": OmpFinexClient(api_key=om_k.get("api_key", "")),
        "wallex":   WallexClient(api_key=wl_k.get("api_key", "")),
        "bitpin":   BitpinClient(api_key=bp_k.get("api_key", ""),
                                 secret_key=bp_k.get("secret_key", "")),
    }


async def run(cfg):
    clients = build_clients(cfg)
    engine  = ArbitrageEngine(clients, cfg)

    active_pairs = [p for p in cfg.get("pairs", []) if p.get("enabled", False)]
    if not active_pairs:
        return {
            "timestamp": time.time(),
            "error": "no active pairs (set enabled=true on at least one pair)",
            "opportunities": [], "executions": [], "errors": [], "count": 0,
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
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg, cfg_error = load_config_from_stdin()
    if cfg_error:
        log.warning(cfg_error + "  — falling back to defaults")

    result = asyncio.run(run(cfg))
    print(json.dumps(result, ensure_ascii=False))
