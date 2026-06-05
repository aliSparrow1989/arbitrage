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
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(title="Arbitrage API")


@app.post("/run")
async def run_once(request: Request):
    # بدنه‌ی JSON رو بگیر؛ اگه خالی/نامعتبر بود، {} در نظر بگیر
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}

    # کانفیگ کاربر روی پیش‌فرض merge می‌شه (مقدار کاربر برنده‌ست)
    cfg = deep_merge(DEFAULT_CONFIG, body)

    # تمام کار (ساخت کلاینت‌ها، session، اسکن، اجرا) داخل run انجام می‌شه
    return await run(cfg)


@app.get("/health")
async def health():
    pairs = [p["name"] for p in DEFAULT_CONFIG.get("pairs", [])]
    return {"status": "ok", "default_pairs": pairs}
