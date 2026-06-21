#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
nobitex.py — Nobitex exchange client.

Order book endpoint is public. Placing orders / reading wallets needs auth.

Auth model (Ed25519 request signing):
  Nobitex issues an api_key (public key) + secret_key (Ed25519 private seed),
  both base64url-encoded. Every authenticated request is signed:

      message   = timestamp + method + path + body   (concatenated, no separators)
      signature = base64( Ed25519_sign(message) )

  and sent with three headers:

      Nobitex-Key:       <api_key / public key>
      Nobitex-Signature: <signature>
      Nobitex-Timestamp: <unix seconds>

  Notes:
    * `path` is the request path including any query string (e.g.
      "/market/orders/list?fromId=123"), NOT the full URL.
    * `body` is the EXACT raw request body bytes; empty for GET. We serialize
      the JSON once and sign+send the very same string so they always match.
    * The key the API enforces IP-whitelisting against is the api_key, so the
      machine running this must be whitelisted in the Nobitex API-key settings.
"""

import base64
import json
import time

import nacl.signing

from base import ExchangeClient, OrderBook, parse_levels, sort_asks, sort_bids


class NobitexClient(ExchangeClient):
    name = "nobitex"
    BASE = "https://apiv2.nobitex.ir"

    # sent on every request so Nobitex can attribute traffic to the bot
    USER_AGENT = "TraderBot/arbitrage"

    def __init__(self, api_key="", secret_key=""):
        super().__init__(api_key, secret_key)
        self._signing_key = None
        if secret_key:
            # secret_key is the base64url-encoded 32-byte Ed25519 seed
            self._signing_key = nacl.signing.SigningKey(
                base64.urlsafe_b64decode(secret_key)
            )

    def _sign(self, method, path, body=""):
        """Return the signed-request headers for (method, path, body)."""
        if self._signing_key is None:
            raise RuntimeError("nobitex secret_key is not configured")
        ts  = str(int(time.time()))
        msg = (ts + method + path + body).encode("utf-8")
        sig = base64.b64encode(self._signing_key.sign(msg).signature).decode()
        return {
            "Nobitex-Key":       self.api_key,
            "Nobitex-Signature": sig,
            "Nobitex-Timestamp": ts,
            "User-Agent":        self.USER_AGENT,
            "Content-Type":      "application/json",
        }

    async def _signed_post(self, path, payload=None):
        """POST with an Ed25519 signature over the exact body we send."""
        body    = json.dumps(payload or {}, separators=(",", ":"))
        headers = self._sign("POST", path, body)
        timeout = self._timeout(self.TRADE_TIMEOUT)
        resp = await self._session.post(
            self.BASE + path, data=body, headers=headers, timeout=timeout
        )
        return await resp.json()

    async def _signed_get(self, path):
        headers = self._sign("GET", path, "")
        timeout = self._timeout(self.READ_TIMEOUT)
        resp = await self._session.get(
            self.BASE + path, headers=headers, timeout=timeout
        )
        return await resp.json()

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
        return await self._signed_post("/market/orders/add", payload)

    async def get_wallets(self):
        return await self._signed_post("/users/wallets/list", {})
