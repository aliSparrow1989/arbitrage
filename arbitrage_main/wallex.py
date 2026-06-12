#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wallex.py — Wallex exchange client.

Price-unit note:
  For USDTTMN Wallex quotes in Toman. The engine passes price_scale=10 so the
  stored book is in Rial and comparable with Nobitex (IRT) / OmpFinex (IRR).
  When PLACING an order the engine divides the price back by the scale, so
  place_order receives prices already in Wallex's native unit.
"""

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids


class WallexClient(ExchangeClient):
    name = "wallex"
    BASE = "https://api.wallex.ir"

    def _auth(self):
        return {"X-API-Key": self.api_key}

    async def get_orderbook(self, symbol, price_scale=1.0):
        url    = self.BASE + "/v1/depth"
        data   = await self._get_json(url, params={"symbol": symbol})
        result = data.get("result", {})

        asks = sort_asks(parse_levels(result.get("ask", []), price_scale))
        bids = sort_bids(parse_levels(result.get("bid", []), price_scale))
        return OrderBook(exchange=self.name, symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, symbol, side, quantity, price, order_type="limit"):
        # price must already be in Wallex native units (Toman for USDTTMN)
        payload = {
            "symbol":     symbol,
            "order_type": order_type,
            "side":       side,        # "buy" / "sell"
            "quantity":   quantity,
            "price":      price,
        }
        url = self.BASE + "/v1/account/orders"
        return await self._post_json(url, payload, headers=self._auth())

    async def get_wallets(self):
        url = self.BASE + "/v1/account/balances"
        return await self._get_json(url, headers=self._auth())
