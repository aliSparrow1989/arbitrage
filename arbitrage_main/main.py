#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI wrapper — n8n هر چند ثانیه POST /run رو صدا می‌زنه.

برخلاف نسخه‌ی قبلی، فی‌ها و جفت‌ها و آستانه دیگه ثابت نیستن:
n8n اون‌ها رو توی بدنه‌ی JSON همین درخواست POST /run می‌فرسته.
هرچی نفرستی، از پیش‌فرض داخل engine.py پر می‌شه (merge بازگشتی).
"""

import logging

from fastapi import FastAPI, Request

from engine import DEFAULT_CONFIG, deep_merge, run

logging.basicConfig(
    level=logging.WARNING,   # n8n only needs the result JSON; keep warnings/errors, drop per-route INFO chatter
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="Arbitrage API")


def _parse_body(body):
    """Parse request body and determine config source."""
    if not isinstance(body, dict):
        body = {}
    is_custom = bool(body)  # آیا کاربر کانفیگی فرستاده؟
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


@app.post("/run")
async def run_once(request: Request):
    body = await _read_body(request)
    cfg, config_source = _parse_body(body)

    # تمام کار (ساخت کلاینت‌ها، session، اسکن، اجرا) داخل run انجام می‌شه
    result = await run(cfg)
    result["config_source"] = config_source
    return result


@app.post("/preview-config")
async def preview_config(request: Request):
    """کانفیگ مرج‌شده رو برمی‌گردونه بدون اجرای موتور آربیتراژ."""
    body = await _read_body(request)
    cfg, config_source = _parse_body(body)
    return {"config_source": config_source, "config": cfg}


@app.get("/health")
async def health():
    pairs = [p["name"] for p in DEFAULT_CONFIG.get("pairs", [])]
    return {"status": "ok", "default_pairs": pairs}
