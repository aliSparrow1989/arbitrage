#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI wrapper — n8n calls POST /balances with a JSON config (containing the
API keys) and gets back the wallet balance on each of the five exchanges.

This mirrors arbitrage_main/main.py: any field omitted from the request body is
filled from DEFAULT_CONFIG via a recursive merge. API keys live only in the
request body — they are not stored or logged.
"""

import logging

from fastapi import FastAPI, Request

from wallet_engine import DEFAULT_CONFIG, deep_merge, run

logging.basicConfig(
    level=logging.WARNING,   # n8n only needs the result JSON; keep warnings/errors
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="Wallet Balance API")


def _parse_body(body):
    """Parse request body and determine config source."""
    if not isinstance(body, dict):
        body = {}
    is_custom = bool(body)
    cfg = deep_merge(DEFAULT_CONFIG, body)
    config_source = "merged" if is_custom else "default"
    return cfg, config_source


async def _read_body(request: Request) -> dict:
    """Safely read JSON body from request."""
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return {}
        return body
    except Exception:
        return {}


@app.post("/balances")
async def balances(request: Request):
    body = await _read_body(request)
    cfg, config_source = _parse_body(body)

    result = await run(cfg)
    result["config_source"] = config_source
    return result


@app.post("/preview-config")
async def preview_config(request: Request):
    """Return the merged config without contacting any exchange."""
    body = await _read_body(request)
    cfg, config_source = _parse_body(body)
    return {"config_source": config_source, "config": cfg}


@app.get("/health")
async def health():
    return {"status": "ok", "exchanges": DEFAULT_CONFIG.get("exchanges", [])}
