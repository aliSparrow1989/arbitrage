#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ramzinex.py — Ramzinex exchange client.

Auth model:
  api_key + secret  ->  POST /auth/api_key/getToken  ->  JWT token
  Private requests send both headers: x-api-key + Authorization2: Bearer <token>.
  Token is fetched lazily on first private call and re-fetched once on a 401.

Order book endpoint is public (no auth) and keyed by an integer pair_id (passed
in as the "symbol" by the engine's scan plan). Prices are already in Rial (IRR),
so ramzinex always uses price_scale=1.0.
"""

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids, to_float


class RamzinexClient(ExchangeClient):
    name         = "ramzinex"
    BASE_PUBLIC  = "https://publicapi.ramzinex.com/exchange/api/v1.0/exchange"
    BASE_PRIVATE = "https://api.ramzinex.com/exchange/api/v1.0/exchange"

    # currency_id mapping for wallet balance queries
    CURRENCY_ID = {
        "IRR": 2, "IRT": 2,
        "USDT": 9, "BTC": 1, "ETH": 3,
        "XRP": 6, "BNB": 10, "SOL": 81, "TON": 166,
    }

    # reverse map: currency_id -> symbol (for normalizing wallet rows).
    # IRR is preferred for id 2 since ramzinex prices are quoted in Rial.
    ID_TO_SYMBOL = {1: "BTC", 2: "IRR", 3: "ETH", 6: "XRP",
                    9: "USDT", 10: "BNB", 81: "SOL", 166: "TON"}

    def __init__(self, api_key="", secret_key=""):
        super().__init__(api_key, secret_key)
        self._token = None

    async def _authenticate(self):
        url     = self.BASE_PRIVATE + "/auth/api_key/getToken"
        payload = {"api_key": self.api_key, "secret": self.secret_key}
        data    = await self._post_json(url, payload)
        try:
            self._token = data["data"]["token"]
        except (KeyError, TypeError):
            raise RuntimeError("ramzinex authenticate failed: " + str(data))

    def _auth_headers(self):
        headers = {"x-api-key": self.api_key}
        if self._token:
            headers["Authorization2"] = "Bearer " + self._token
        return headers

    async def get_orderbook(self, pair_id, price_scale=1.0):
        # public endpoint; prices already in Rial so price_scale is normally 1.0
        url  = self.BASE_PUBLIC + "/orderbooks/%d/buys_sells" % int(pair_id)
        data = await self._get_json(url)
        book = data.get("data", {})

        # buys = bid side (descending), sells = ask side (ascending)
        bids = sort_bids(parse_levels(book.get("buys",  []), price_scale))
        asks = sort_asks(parse_levels(book.get("sells", []), price_scale))
        return OrderBook(exchange=self.name, symbol=str(pair_id), bids=bids, asks=asks)

    async def place_order(self, pair_id, side, amount, price):
        # price must be in Rial (IRR)
        if self._token is None:
            await self._authenticate()
        payload = {
            "pair_id": pair_id,
            "amount":  amount,
            "price":   int(price),
            "type":    side,        # "buy" / "sell"
        }
        url     = self.BASE_PRIVATE + "/users/me/orders/limit"
        timeout = self._timeout(self.TRADE_TIMEOUT)

        # try once, re-authenticate on 401, then retry once
        for attempt in range(2):
            resp = await self._session.post(
                url, json=payload, headers=self._auth_headers(), timeout=timeout
            )
            if resp.status == 401 and attempt == 0:
                await self._authenticate()
                continue
            return await resp.json()

    async def get_wallets(self):
        if self._token is None:
            await self._authenticate()
        url     = self.BASE_PRIVATE + "/users/me/funds/summaryDesktop"
        timeout = self._timeout(self.READ_TIMEOUT)
        for attempt in range(2):
            resp = await self._session.get(
                url, headers=self._auth_headers(), timeout=timeout
            )
            if resp.status == 401 and attempt == 0:
                await self._authenticate()
                continue
            return await resp.json()
        return {}

    def normalize_wallets(self, raw):
        """Ramzinex `summaryDesktop` returns rows shaped like:
            {"currency_id": 9, "total_nr": <total>, "in_order_nr": <locked>}

        The asset is a numeric currency_id (no symbol) and the field names are
        unknown to the generic normalizer, so we map them here. Unknown ids are
        kept under a "CID_<id>" label so nothing is silently dropped.
        """
        rows = raw.get("data") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return {}
        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = row.get("currency_id")
            asset = self.ID_TO_SYMBOL.get(cid, "CID_%s" % cid)
            total  = to_float(row.get("total_nr"))
            locked = to_float(row.get("in_order_nr"))
            free   = total - locked
            if free < 0:
                free = 0.0
            out[asset] = {"free": free, "locked": locked, "total": total}
        return out
