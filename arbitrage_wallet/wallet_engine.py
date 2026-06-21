#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallet_engine.py — Wallet-balance coordinator.

Reads ONE JSON config object (from n8n via main.py, or from stdin when run
directly), fetches wallet balances from the five exchanges concurrently, and
returns ONE JSON result describing the balance on each exchange.

This is the wallet sibling of arbitrage_main/engine.py: it reuses the exact
same exchange clients, auth, shared aiohttp session and config-merge machinery,
but instead of scanning order books it calls get_wallets() on each exchange.

Config schema (every field optional — missing fields fall back to defaults):
{
  "exchanges":    ["nobitex", "ompfinex", "wallex", "bitpin", "ramzinex"],
  "include_zero": false,          # keep assets whose total balance is 0
  "include_raw":  false,          # keep each exchange's raw payload in output
  "assets":       ["USDT","BTC"], # optional whitelist; empty/missing => all
  "keys": {
    "nobitex":  {"api_key": "...", "secret_key": "..."},
    "ompfinex": {"api_key": "..."},
    "wallex":   {"api_key": "..."},
    "bitpin":   {"api_key": "...", "secret_key": "..."},
    "ramzinex": {"api_key": "...", "secret_key": "..."}
  }
}

API keys are read ONLY from this config (i.e. from the request body); they are
never stored, logged or read from the environment.
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
from ramzinex import RamzinexClient


log = logging.getLogger("wallet_engine")

ALL_EXCHANGES = ["nobitex", "ompfinex", "wallex", "bitpin", "ramzinex"]


# ─────────────────────────────────────────────
#  DEFAULT CONFIG (used when no body / stdin is sent)
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "exchanges":    list(ALL_EXCHANGES),
    "include_zero": False,
    "include_raw":  False,         # keep each exchange's raw payload in the output
    "assets":       [],            # empty => return every asset
    "keys": {
        "nobitex":  {"api_key": "", "secret_key": ""},
        "ompfinex": {"api_key": ""},
        "wallex":   {"api_key": ""},
        "bitpin":   {"api_key": "", "secret_key": ""},
        "ramzinex": {"api_key": "", "secret_key": ""},
    },
}


# ─────────────────────────────────────────────
#  CONFIG MERGE + CLIENT BUILDING (shared with engine.py)
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


# ─────────────────────────────────────────────
#  FIAT CANONICALIZATION
# ─────────────────────────────────────────────
#
# The same Iranian fiat currency comes back under different tickers AND
# different units across exchanges:
#     nobitex -> RLS (Rial)   ompfinex -> IRR (Rial)
#     wallex  -> TMN (Toman)  bitpin   -> IRT (Toman)
# We collapse all of them into a single canonical "IRT" expressed in Toman,
# so balances are directly comparable / summable. Rial tickers are divided by
# 10 (1 Toman = 10 Rial); Toman tickers pass through unchanged.

CANONICAL_FIAT = "IRT"
FIAT_TO_TOMAN = {
    "RLS": 0.1, "IRR": 0.1,    # Rial  -> Toman
    "TMN": 1.0, "IRT": 1.0,    # Toman -> Toman (already canonical)
}


def canonicalize_fiat(balances):
    """Merge every fiat ticker into `IRT` (Toman) and convert Rial -> Toman.

    Crypto assets pass through untouched. If an exchange somehow reports two
    fiat tickers, their (converted) amounts are summed into the single IRT entry.
    """
    out = {}
    for asset, vals in balances.items():
        factor = FIAT_TO_TOMAN.get(asset)
        if factor is None:                       # crypto — keep as-is
            entry = out.setdefault(asset, {"free": 0.0, "locked": 0.0, "total": 0.0})
            free, locked = vals["free"], vals["locked"]
        else:                                    # fiat — fold into IRT (Toman)
            entry = out.setdefault(CANONICAL_FIAT, {"free": 0.0, "locked": 0.0, "total": 0.0})
            free, locked = vals["free"] * factor, vals["locked"] * factor
        entry["free"]   += free
        entry["locked"] += locked
        entry["total"]   = entry["free"] + entry["locked"]
    return out


def round_balances(balances):
    """Round to a sane precision: whole Toman for IRT, 8 decimals for crypto.
    Returns plain JSON numbers (int for IRT, float for crypto)."""
    out = {}
    for asset, vals in balances.items():
        if asset == CANONICAL_FIAT:
            out[asset] = {k: round(v) for k, v in vals.items()}        # whole Toman (int)
        else:
            out[asset] = {k: round(v, 8) for k, v in vals.items()}     # crypto
    return out


# ─────────────────────────────────────────────
#  BALANCE FILTERING
# ─────────────────────────────────────────────

def _filter_balances(balances, include_zero, assets):
    """Drop zero-total assets (unless include_zero) and apply asset whitelist."""
    whitelist = {a.upper() for a in (assets or [])}
    out = {}
    for asset, vals in balances.items():
        if whitelist and asset not in whitelist:
            continue
        if not include_zero and vals.get("total", 0.0) == 0.0:
            continue
        out[asset] = vals
    return out


# ─────────────────────────────────────────────
#  RUN — fetch wallets across all enabled exchanges
# ─────────────────────────────────────────────

async def run(cfg):
    clients      = build_clients(cfg)
    enabled      = cfg.get("exchanges") or list(ALL_EXCHANGES)
    enabled      = [x for x in enabled if x in clients]
    include_zero = bool(cfg.get("include_zero", False))
    include_raw  = bool(cfg.get("include_raw", False))
    assets       = cfg.get("assets", [])

    conn    = aiohttp.TCPConnector(limit=10)
    session = aiohttp.ClientSession(connector=conn)
    for c in clients.values():
        c.attach_session(session)

    try:
        raw_results = await asyncio.gather(
            *[clients[x].get_wallets() for x in enabled],
            return_exceptions=True,
        )
    finally:
        await session.close()

    wallets = {}
    errors  = []
    for label, raw in zip(enabled, raw_results):
        if isinstance(raw, Exception):
            log.warning("[%s] wallet fetch failed: %s", label, raw)
            errors.append({"exchange": label, "error": str(raw)})
            entry = {"balances": {}, "error": str(raw)}
            if include_raw:
                entry["raw"] = None
            wallets[label] = entry
            continue
        try:
            normalized = clients[label].normalize_wallets(raw)
        except Exception as exc:                       # normalization is best-effort
            log.warning("[%s] normalize failed: %s", label, exc)
            normalized = {}
        canonical = canonicalize_fiat(normalized)
        balances  = _filter_balances(canonical, include_zero, assets)
        balances  = round_balances(balances)
        entry = {"balances": balances}
        if include_raw:
            entry["raw"] = raw
        wallets[label] = entry

    return {
        "timestamp": time.time(),
        "exchanges": enabled,
        "wallets":   wallets,
        "errors":    errors,
    }


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
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg, cfg_error = load_config_from_stdin()
    if cfg_error:
        log.warning(cfg_error + "  — falling back to defaults")

    result = asyncio.run(run(cfg))
    print(json.dumps(result, ensure_ascii=False))
