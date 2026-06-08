---
name: sync-arbitrage
description: Sync a new monolithic arbitrage.py main file into this project's separated structure (base.py + per-exchange clients + engine.py + main.py) and regenerate the n8n config JSON. Use whenever the user provides an updated arbitrage.py / "main file" and wants the split files and the API config brought back in line with it.
---

# Sync arbitrage.py → separated files + n8n config

This project started as one monolithic file (`arbitrage.py`) and was split into:

- `base.py` — `OrderBook`, `ExchangeClient` base, `parse_levels` / `sort_asks` / `sort_bids` helpers.
- `nobitex.py`, `ompfinex.py`, `wallex.py`, `bitpin.py` — one client each, subclassing `ExchangeClient`.
- `engine.py` — `DEFAULT_CONFIG` + `ArbitrageEngine` (evaluate / scan / execute / scan_once) + config merge + stdin→stdout entry point.
- `main.py` — FastAPI wrapper: `POST /run`, `POST /preview-config`, `GET /health`.

**Config is data-driven and passed in at runtime via the `POST /run` JSON body (from n8n).** `engine.py`'s `DEFAULT_CONFIG` only supplies fallbacks for fields the request omits (`deep_merge`, request wins).

The user periodically hands over a *new* monolithic `arbitrage.py` with edited values/logic. This skill brings the split files and the config back in sync.

## Inputs

- The new monolith: a path passed as an argument, a pasted code block in the conversation, or `./arbitrage.py` in the project root. If none is available, ask the user for it.

## Architecture rule — do NOT regress the split

The separated design is **intentionally simpler** than the monolith. The monolith runs an infinite loop with a circuit breaker, balance pre-checks, order rollback, and position flattening. The split version is a **single, stateless scan** driven by repeated `POST /run` calls from n8n.

When syncing:
- **Always sync**: config value tables, and the `evaluate` / `scan` / `_transfer_pct` / `_irt_transfer_fee_irr` math.
- **Do NOT auto-port** monolith-only machinery: `run()` main loop, `SCAN_INTERVAL`, `_suspend_pair`/`_is_suspended` circuit breaker, `_check_balances`, `_place_leg`/`_cancel_order`/`_flatten` rollback. If the monolith *changed* any of this in a way that matters, **flag it and ask** before porting — don't silently expand the split.

## Step 1 — Sync `engine.py` `DEFAULT_CONFIG`

Map each monolith constant to its config key. Watch the shape changes marked ⚠.

| Monolith constant | `DEFAULT_CONFIG` / JSON key | Notes |
|---|---|---|
| `MIN_PROFIT_PCT` | `min_profit_pct` | |
| `DRY_RUN` | `dry_run` | |
| `TRADE_AMOUNT` | `trade_amount` | same shape |
| `MIN_USDT_TRANSFER_AMOUNT` | `min_usdt_transfer_amount` | |
| `MIN_IRT_TRANSFER_IRR` | `min_irt_transfer_irr` | |
| `NOBITEX_FEE` | `fees.nobitex` | nested `IRT`/`USDT` → `{maker,taker}`, same shape |
| `OMPFINEX_FEE` | `fees.ompfinex` | ⚠ `flat_maker`/`flat_taker` → `maker`/`taker` (strip `flat_`) |
| `WALLEX_FEE` | `fees.wallex` | `{maker,taker}` |
| `BITPIN_FEE` | `fees.bitpin` | `{maker,taker}` |
| `TRANSFER_FEE` | `transfer_fees` | ⚠ tuple-keyed `(asset, exchange)` → nested `transfer_fees[asset][exchange]` |
| `IRT_WITHDRAWAL_FLAT_IRR` | `irt_withdrawal_flat_irr` | same shape |
| `IRT_DEPOSIT_FEE_PCT` | `irt_deposit_fee_pct` | same shape |
| `IRT_DEPOSIT_FLAT_IRR` | `irt_deposit_flat_irr` | same shape |
| `PAIR_CONFIG` | `pairs` | same per-pair shape; keep `enabled` flags |
| `NOBITEX_API_KEY`, `OMPFINEX_API_KEY`, `WALLEX_API_KEY`, `BITPIN_API_KEY`, `BITPIN_SECRET_KEY` | `keys.<exchange>.api_key` / `keys.bitpin.secret_key` | ⚠ **Keep `engine.py` defaults EMPTY (`""`)**. Real keys go only into the n8n JSON and `config.sample.json`. |

Edit only the values that actually differ — don't rewrite the whole block. Keep the existing comment style.

## Step 2 — Sync the math in `engine.py`

Compare these against the monolith and update if changed:

- `ArbitrageEngine.evaluate` — spread / fee / transfer / IRT-fee math and the returned dict keys (e.g. `irt_fee_pct`, `irt_fee_irr`).
- `_transfer_pct` — USDT amortizes over `min_usdt_transfer_amount`; other assets over `amount`.
- `_irt_transfer_fee_irr` — withdrawal (sell side) + deposit (buy side).
- `_exchange_fee` — stays **data-driven from `self.fees`**, not hardcoded constants. (Monolith reads `NOBITEX_FEE` etc.; the split reads `cfg["fees"]`. Keep it cfg-driven.)
- `scan` — keep returning `(best_candidate, opportunities)` and keep the per-route logging (IRT vs USDT format).

## Step 3 — Sync the client files (only if the monolith changed them)

Per-exchange API specifics live in `nobitex.py` / `ompfinex.py` / `wallex.py` / `bitpin.py`. If the monolith changed an endpoint URL, payload shape, auth header, orderbook parsing, or the OmpFinex bids/asks swap, port that change. Use the base-class helpers (`_get_json`, `_post_json`, `parse_levels`, `sort_asks`, `sort_bids`) — do **not** reintroduce inline `aiohttp`/`_parse` code.

## Step 4 — Regenerate the n8n config

Produce the n8n Code-node snippet (this is what gets pasted into the n8n workflow — it is **not** stored in the repo):

```js
return [{ json: { /* full config, mirroring DEFAULT_CONFIG */ } }];
```

Rules:
- Include **real API keys** here (from the monolith's `*_API_KEY` constants).
- Include every config section: `dry_run`, `min_profit_pct`, `keys`, `fees`, `transfer_fees`, `trade_amount`, `min_usdt_transfer_amount`, `min_irt_transfer_irr`, `irt_withdrawal_flat_irr`, `irt_deposit_fee_pct`, `irt_deposit_flat_irr`, `pairs`.
- Use `null` (not `None`) for absent symbols like TON's `omf_symbol` / `bitpin_symbol`.
- Print it in the chat for the user to paste into n8n.

## Step 5 — Update `config.sample.json`

Update the tracked sample to match the new values so the repo has a current reference. (It may be a subset — keep its existing scope unless the user wants it expanded.)

## Step 6 — Verify

- `python3 -m py_compile engine.py base.py nobitex.py ompfinex.py wallex.py bitpin.py` (the project's aiohttp lives in `.venv`; `py_compile` is the syntax check that doesn't need imports).
- Summarize exactly what changed (old → new per field) and reprint the n8n snippet.

## Deploy reminder

Python is baked into the Docker image at build time (`COPY . .`). After syncing, the user must rebuild to pick up `engine.py`/client changes:

```bash
docker compose up -d --build arbitrage
```

The n8n JSON change takes effect immediately (it's in the n8n workflow, not the image) — no rebuild needed for that part.
