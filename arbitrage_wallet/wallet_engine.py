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
  "assets":       ["USDT","BTC"], # optional whitelist; empty/missing => all
  "keys": {
    "nobitex":  {"api_key": "..."},
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
    "assets":       [],            # empty => return every asset
    "keys": {
        "nobitex":  {"api_key": ""},
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
        "nobitex":  NobitexClient(api_key=nb_k.get("api_key", "")),
        "ompfinex": OmpFinexClient(api_key=om_k.get("api_key", "")),
        "wallex":   WallexClient(api_key=wl_k.get("api_key", "")),
        "bitpin":   BitpinClient(api_key=bp_k.get("api_key", ""),
                                 secret_key=bp_k.get("secret_key", "")),
        "ramzinex": RamzinexClient(api_key=rx_k.get("api_key", ""),
                                   secret_key=rx_k.get("secret_key", "")),
    }


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
            wallets[label] = {"balances": {}, "raw": None, "error": str(raw)}
            continue
        try:
            normalized = clients[label].normalize_wallets(raw)
        except Exception as exc:                       # normalization is best-effort
            log.warning("[%s] normalize failed: %s", label, exc)
            normalized = {}
        balances = _filter_balances(normalized, include_zero, assets)
        wallets[label] = {"balances": balances, "raw": raw}

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
