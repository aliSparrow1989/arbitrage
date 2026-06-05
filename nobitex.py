#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nobitex.py — Nobitex exchange client.

Order book endpoint is public. Placing orders / reading wallets needs the
API key (Authorization: Token ...).
"""

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids


class NobitexClient(ExchangeClient):
    name = "nobitex"
    BASE = "https://apiv2.nobitex.ir"

    def _auth(self):
        return {"Authorization": "Token " + self.api_key}

    async def get_orderbook(self, symbol, price_scale=1.0):
        url  = self.BASE + "/v3/orderbook/" + symbol
        data = await self._get_json(url)

        asks = sort_asks(parse_levels(data.get("asks", []), price_scale))
        bids = sort_bids(parse_levels(data.get("bids", []), price_scale))
        return OrderBook(exchange=self.name, symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, order_type, src, dst, amount, price):
        payload = {
            "type":        order_type,      # "buy" / "sell"
            "srcCurrency": src,
            "dstCurrency": dst,
            "amount":      str(amount),
            "price":       str(int(price)),
            "execution":   "limit",
        }
        url = self.BASE + "/market/orders/add"
        return await self._post_json(url, payload, headers=self._auth())

    async def get_wallets(self):
        url = self.BASE + "/users/wallets/list"
        return await self._post_json(url, {}, headers=self._auth())
