#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bitpin.py — Bitpin exchange client.

Auth model:
  api_key + secret_key  ->  POST /usr/authenticate/  ->  access + refresh tokens
  access token expires  ->  POST /usr/refresh_token/ ->  new access token

The order book endpoint is public (no auth). Placing orders / reading wallets
needs the access token, which is fetched lazily on first use and refreshed once
on a 401.

Price-unit note (same as Wallex):
  USDT_IRT prices are in Toman; engine passes price_scale=10 to normalize to
  Rial, and divides back by the scale before placing an order.
"""

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids


class BitpinClient(ExchangeClient):
    name = "bitpin"
    BASE = "https://api.bitpin.ir"

    def __init__(self, api_key="", secret_key=""):
        super().__init__(api_key, secret_key)
        self._access  = None
        self._refresh = None

    async def _authenticate(self):
        url     = self.BASE + "/api/v1/usr/authenticate/"
        payload = {"api_key": self.api_key, "secret_key": self.secret_key}
        data    = await self._post_json(url, payload)
        if "access" not in data:
            raise RuntimeError("bitpin authenticate failed: " + str(data))
        self._access  = data["access"]
        self._refresh = data["refresh"]

    async def _do_refresh(self):
        url     = self.BASE + "/api/v1/usr/refresh_token/"
        payload = {"refresh": self._refresh}
        data    = await self._post_json(url, payload)
        self._access = data["access"]

    def _auth_headers(self):
        return {"Authorization": "Bearer " + self._access} if self._access else {}

    async def get_orderbook(self, symbol, price_scale=1.0):
        url  = self.BASE + "/api/v1/mth/orderbook/" + symbol + "/"
        data = await self._get_json(url)

        asks = sort_asks(parse_levels(data.get("asks", []), price_scale))
        bids = sort_bids(parse_levels(data.get("bids", []), price_scale))
        return OrderBook(exchange=self.name, symbol=symbol, bids=bids, asks=asks)

    async def place_order(self, symbol, side, base_amount, price, order_type="limit"):
        if self._access is None:
            await self._authenticate()

        payload = {
            "symbol":      symbol,
            "type":        order_type,
            "side":        side,        # "buy" / "sell"
            "price":       str(int(price)),
            "base_amount": str(base_amount),
        }
        url = self.BASE + "/api/v1/odr/orders/"
        timeout = self._timeout(self.TRADE_TIMEOUT)

        # try once, refresh token on 401, then retry once
        for attempt in range(2):
            resp = await self._session.post(
                url, json=payload, headers=self._auth_headers(), timeout=timeout
            )
            if resp.status == 401 and attempt == 0:
                await self._do_refresh()
                continue
            return await resp.json()

    async def get_wallets(self):
        if self._access is None:
            await self._authenticate()
        url = self.BASE + "/api/v1/wlt/wallets/"
        return await self._get_json(url, headers=self._auth_headers())
