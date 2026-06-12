#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ompfinex.py — OmpFinex exchange client.

Quirks preserved from the original bot:
  * A single /v1/orderbook call returns ALL markets at once, so we cache the
    full response for a couple of seconds and slice per-symbol.
  * The API swaps the meaning of "asks"/"bids":
        response "asks" key -> real BIDS  (buy orders)
        response "bids" key -> real ASKS  (sell orders)
    We correct that here so the OrderBook is normalized like every other one.
"""

import time

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids


class OmpFinexClient(ExchangeClient):
    name = "ompfinex"
    BASE = "https://api.ompfinex.com"

    def __init__(self, api_key="", secret_key=""):
        super().__init__(api_key, secret_key)
        self._cache     = {}
        self._cache_ts  = 0.0
        self._cache_ttl = 2.0   # reuse one fetch for 2 seconds

    def _auth(self):
        return {"Authorization": "Bearer " + self.api_key}

    async def _fetch_all(self):
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        url = self.BASE + "/v1/orderbook"
        raw = await self._get_json(url)
        self._cache    = raw.get("data", raw)
        self._cache_ts = now
        return self._cache

    async def get_orderbook(self, internal_symbol, price_scale=1.0):
        # map internal symbol -> OmpFinex symbol where they differ
        symbol_map = {
            "USDTIRT": "USDTIRR",
            "BTCUSDT": "BTCUSDT",
            "ETHUSDT": "ETHUSDT",
        }
        omf_sym   = symbol_map.get(internal_symbol, internal_symbol)
        all_books = await self._fetch_all()
        pair_data = all_books.get(omf_sym, {})

        # swap correction: their "asks" are real bids, their "bids" are real asks
        real_bids = sort_bids(parse_levels(pair_data.get("asks", []), price_scale))
        real_asks = sort_asks(parse_levels(pair_data.get("bids", []), price_scale))
        return OrderBook(exchange=self.name, symbol=omf_sym, bids=real_bids, asks=real_asks)

    async def place_order(self, market_id, side, quantity, price):
        payload = {
            "market_id":  market_id,
            "order_type": side,        # "buy" / "sell"
            "quantity":   quantity,
            "price":      price,
            "type":       "limit",
        }
        url = self.BASE + "/v1/user/orders"
        return await self._post_json(url, payload, headers=self._auth())

    async def get_wallets(self):
        url = self.BASE + "/v1/user/wallets"
        return await self._get_json(url, headers=self._auth())
