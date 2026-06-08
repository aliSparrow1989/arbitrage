#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py — هر ۵ ثانیه یک بار run() رو اجرا می‌کنه و نتیجه رو چاپ می‌کنه.
به کد اصلی دست نمی‌زنه.

اجرا:
    python3 monitor.py
"""
import sys

import asyncio
import json
import logging
from datetime import datetime

from engine import DEFAULT_CONFIG, run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=__import__("sys").stderr,
)

INTERVAL = 5  # ثانیه


def pprint(text):
    """چاپ روی stderr تا کنار لاگ‌ها دیده بشه."""
    print(text, file=sys.stderr)


async def main():
    pprint(f"[monitor] شروع لوپ — هر {INTERVAL} ثانیه یک بار اسکن\n")
    iteration = 0

    while True:
        iteration += 1
        now = datetime.now().strftime("%H:%M:%S")
        pprint(f"{'─' * 60}")
        pprint(f"[{now}]  اسکن #{iteration}")

        try:
            result = await run(DEFAULT_CONFIG)
            pprint(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as exc:
            pprint(f"[خطا] {exc}")

        pprint("")
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[monitor] متوقف شد.")
