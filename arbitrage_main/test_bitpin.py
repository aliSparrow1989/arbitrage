#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_bitpin.py — standalone sanity test for the Bitpin client.

What it does, step by step (each step prints its result so you can see exactly
where it breaks):
  1. authenticate           -> proves api_key + secret_key are valid
  2. get_wallets            -> proves the access token works on a private route
  3. get_orderbook          -> reads the current USDT/IRT book (raw Toman prices)
  4. place_order (sell 1)    -> places a SELL limit order for 1 USDT

Fill in API_KEY and SECRET_KEY below, then run:
    python test_bitpin.py

NOTE: this places a REAL order for 1 USDT. The price is set 1% ABOVE the best
bid on purpose, so it sits as a resting limit order instead of filling instantly
— giving you a chance to cancel it from the Bitpin UI. Set DRY_RUN = True to skip
the actual order placement and only test auth + orderbook.
"""

import asyncio

import aiohttp

from bitpin import BitpinClient


# ── fill these in ──────────────────────────────────────────────
API_KEY    = "DgATeGvIqGSYEhI8Z0XL3mK5oRnDPBQPYKPrjp8PSleUEjjNWSOBmW5AL6BTEHZlFQrNGxMYn6kVEefHZgFJeIyUQSp5cPivEhS7h2sgu5gtZIwXYvM0mKSsh7XLjI17"
SECRET_KEY = "ZvIh7OQSKASybemE4FLK8qAVyQrVR099bWO6WJHf0D5YC4P4i66Lz0TLMOOy3lzrBnPa2Mq3qb17B27EBfVysC6i68vTa8wspP5aWR2sVB0HJmTsh9fxcDG0KEpTXFEn"

SYMBOL     = "USDT_IRT"   # bitpin market symbol
SELL_AMT   = "1"         # how many USDT to sell
DRY_RUN    = False        # True = test auth + orderbook only, do NOT place order
# ───────────────────────────────────────────────────────────────


async def main():
    if not API_KEY or not SECRET_KEY:
        print("!! Fill in API_KEY and SECRET_KEY at the top of the file first.")
        return

    client = BitpinClient(api_key=API_KEY, secret_key=SECRET_KEY)

    async with aiohttp.ClientSession() as session:
        client.attach_session(session)

        # 1) authenticate
        print("1) authenticate ...")
        try:
            await client._authenticate()
            print("   OK  access token acquired:", client._access[:16], "...")
        except Exception as e:
            print("   FAIL:", repr(e))
            return

        # 2) wallets (private route, proves token works)
        print("2) get_wallets ...")
        try:
            wallets = await client.get_wallets()
            print("   OK :", wallets)
        except Exception as e:
            print("   FAIL:", repr(e))

        # 3) orderbook (raw Toman prices — price_scale=1.0)
        print("3) get_orderbook ...")
        book = await client.get_orderbook(SYMBOL, price_scale=1.0)
        best_bid = book.best_bid()
        best_ask = book.best_ask()
        print("   best_bid:", best_bid, " best_ask:", best_ask)

        if not best_bid:
            print("   FAIL: empty order book, cannot pick a sell price.")
            return

        # price set 1% above best bid so it rests instead of filling instantly
        sell_price = int(best_bid * 1.01)
        print("   sell price chosen:", sell_price, "(1% above best bid)")

        # 4) place the sell order
        if DRY_RUN:
            print("4) place_order ... SKIPPED (DRY_RUN = True)")
            return

        print("4) place_order  sell", SELL_AMT, SYMBOL, "@", sell_price, "...")
        try:
            resp = await client.place_order(
                symbol=SYMBOL,
                side="sell",
                base_amount=SELL_AMT,
                price=sell_price,
                order_type="limit",
            )
            print("   response:", resp)
        except Exception as e:
            print("   FAIL:", repr(e))


if __name__ == "__main__":
    asyncio.run(main())
