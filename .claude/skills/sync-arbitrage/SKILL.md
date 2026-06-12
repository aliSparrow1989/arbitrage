---
name: sync-arbitrage
description: Turn a monolithic arbitrage.py into (or back in sync with) this project's separated structure (base.py + per-exchange clients + engine.py + main.py + Dockerfile + config). If the split already exists, sync it; if only arbitrage.py exists, bootstrap the whole split from scratch. Also regenerates the n8n config JSON. Use whenever the user provides an arbitrage.py / "main file".
---

# arbitrage.py → separated files + n8n config

This project started as one monolithic file (`arbitrage.py`) and is meant to live as a separated structure:

- `base.py` — `OrderBook`, `ExchangeClient` base, `parse_levels` / `sort_asks` / `sort_bids` helpers.
- `nobitex.py`, `ompfinex.py`, `wallex.py`, `bitpin.py`, `ramzinex.py` — **one client file per exchange**, each subclassing `ExchangeClient`.
- `engine.py` — `DEFAULT_CONFIG` + `ArbitrageEngine` (the **comparison / arbitrage** logic: evaluate / scan / execute / scan_once) + config merge + stdin→stdout entry point.
- `main.py` — FastAPI wrapper: `POST /run`, `POST /preview-config`, `GET /health`.
- `requirements.txt`, `Dockerfile`, `config.sample.json`, `.gitignore` — packaging + a tracked sample of the runtime config.

**Config is data-driven and passed in at runtime via the `POST /run` JSON body (from n8n).** `engine.py`'s `DEFAULT_CONFIG` only supplies fallbacks for fields the request omits (`deep_merge`, request wins). The tracked `config.sample.json` is the reference copy of that runtime config (with real keys).

## Inputs

- The monolith: a path passed as an argument, a pasted code block in the conversation, or `./arbitrage.py` in the project root (check the skill's own dir and the project root). If none is available, ask the user for it.

## Step 0 — Detect the mode (bootstrap vs sync)

Look at the project dir (where `requirements.txt` / `main.py` would live — currently `arbitrage_main/`):

- **If the split already exists** (`base.py`, `engine.py`, `main.py` and the per-exchange files are present) → **Mode B: Sync** (jump to "Mode B" below).
- **If only `arbitrage.py` exists** (no `engine.py` / `base.py` / per-exchange files) → **Mode A: Bootstrap** — create the whole split from scratch (next section).

If it's ambiguous (some files exist, some don't), prefer Sync for the files that exist and create only the missing ones, and tell the user what you did.

---

# Mode A — Bootstrap the split from scratch

Goal: take a single `arbitrage.py` and produce the full separated layout above. **Keep each exchange in its own file, and keep all comparison/arbitrage logic in `engine.py` — never merge exchanges together or fold the math into a client file.**

## A1 — Read the monolith and inventory it

From `arbitrage.py`, identify:
- Which **exchanges** appear (look for per-exchange API base URLs, fee constants like `NOBITEX_FEE`, symbol fields like `*_symbol`, place-order branches). One client file per exchange found.
- The shared order-book shape and any parse/sort helpers → these become `base.py`.
- The **config constants** (fees, transfer fees, thresholds, trade sizes, pair table, API keys) → these become `DEFAULT_CONFIG` + `config.sample.json` + the n8n JSON.
- The comparison/arbitrage math (spread, fees, transfer cost, profit gates) → `engine.py`.

## A2 — Create `base.py`

Shared building blocks only:
- `OrderBook(exchange, symbol, bids, asks)` with `best_bid/best_ask/best_bid_volume/best_ask_volume`. Bids stored descending, asks ascending; prices in a common unit (Rial for IRT, USDT for USDT markets) after `price_scale`.
- `parse_levels(rows, price_scale=1.0)` — accepts list/tuple rows or `{"price","amount"/"quantity"}` dicts; multiplies price by `price_scale`.
- `sort_asks` (ascending), `sort_bids` (descending).
- `ExchangeClient` base: holds `api_key`/`secret_key`/`_session`; `attach_session(session)` so all clients share one `aiohttp` session; `_get_json` / `_post_json` helpers with `READ_TIMEOUT` / `TRADE_TIMEOUT`; abstract `get_orderbook` / `place_order` / `get_wallets`.

## A3 — Create one client file per exchange

Each `<exchange>.py` defines `class <Exchange>Client(ExchangeClient)` with `name`, `BASE`, and implements `get_orderbook(symbol, price_scale)` + `place_order(...)` (+ `get_wallets` if the monolith had it). **Use only the base-class helpers** (`_get_json`, `_post_json`, `parse_levels`, `sort_asks`, `sort_bids`) — do **not** copy inline `aiohttp` / ad-hoc parsing from the monolith into clients.

Per-exchange specifics to carry over from the monolith:
- **OmpFinex**: preserve any bids/asks swap it does.
- **Wallex / Bitpin**: IRT markets quote in Toman → engine stores Rial via `price_scale=10.0`; `place_order` receives price already divided back to native units.
- **Ramzinex**: orderbook is the **public** endpoint keyed by integer `pair_id` (`buys`=bids, `sells`=asks), prices already in Rial (`price_scale=1.0`); auth is `api_key`+`secret` → JWT `token` (fetched lazily, re-fetched once on 401) sent as `x-api-key` + `Authorization2: Bearer <token>`; keep its `CURRENCY_ID` map for wallet queries.

## A4 — Create `engine.py` (the comparison/arbitrage file)

This is the **only** file with arbitrage logic. It must contain:
- `DEFAULT_CONFIG` — every config constant from the monolith mapped to a JSON key (see the mapping table in Mode B, Step 1). **Keep API-key defaults EMPTY (`""`)** — real keys live only in `config.sample.json` and the n8n JSON.
- `ArbitrageEngine` with `_exchange_fee` (data-driven from `cfg["fees"]`, nested by market type for nobitex/ramzinex, flat for others), `_transfer_pct`, `_irt_transfer_fee_irr`, `evaluate`, `scan` (returns `(best_candidate, opportunities)` with per-route IRT/USDT logging), `execute`, `scan_once`.
- `deep_merge`, `build_clients` (constructs the per-exchange clients from `cfg["keys"]`), `async run(cfg)` (builds clients, shares one session, scans active pairs, closes session).
- stdin→stdout entry point: read one JSON config from stdin (fall back to `DEFAULT_CONFIG`), print one JSON result to stdout, all logs to **stderr** so stdout stays clean for n8n.

**Do NOT port monolith-only machinery** into the split: the `run()` infinite loop, `SCAN_INTERVAL`, circuit breaker (`_suspend_pair`/`_is_suspended`), `_check_balances`, order rollback (`_place_leg`/`_cancel_order`/`_flatten`). The split is a **single, stateless scan** driven by repeated `POST /run` calls. If the monolith has logic that seems essential and doesn't fit the stateless model, **flag it and ask** before adding it.

## A5 — Create `main.py` (FastAPI wrapper)

`POST /run` (merge body over `DEFAULT_CONFIG` via `deep_merge`, run engine, return result + `config_source`), `POST /preview-config` (return merged config without running), `GET /health` (status + default pair names). Body parsing must be defensive (non-dict / invalid JSON → `{}`). Log level `WARNING`.

## A6 — Create packaging + config files

- `requirements.txt`: `fastapi`, `uvicorn[standard]`, `aiohttp` (plus anything the clients need).
- `Dockerfile`: `python:3.11-slim`, `WORKDIR /app`, copy `requirements.txt` first and `pip install` (cache layer), then `COPY . .`, `EXPOSE 8000`, `CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]`.
- `.gitignore`: `__pycache__/`, `.venv/`, `.env`, `*.log`, `.DS_Store`, etc.
- `config.sample.json`: the full runtime config (mirrors `DEFAULT_CONFIG`) **with the real API keys from the monolith**. This is the externalized config the user keeps in the repo as reference; the same shape is what n8n sends.

## A7 — Verify + hand off

- `python3 -m py_compile engine.py base.py main.py <each client>.py`.
- Produce the **n8n Code-node snippet** (Mode B, Step 4) with real keys.
- Tell the user the split was created from scratch, list the files, and give the build command (see "Deploy reminder").

---

# Mode B — Sync an existing split with a new monolith

The user periodically hands over a *new* monolithic `arbitrage.py` with edited values/logic. Bring the split files and the config back in sync.

## Architecture rule — do NOT regress the split

The separated design is **intentionally simpler** than the monolith (single stateless scan vs. infinite loop + circuit breaker + balance pre-checks + order rollback + position flattening).

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
| `RAMZINEX_FEE` | `fees.ramzinex` | nested by market type (`IRT`/`USDT`) → `{maker,taker}`, like nobitex |
| `TRANSFER_FEE` | `transfer_fees` | ⚠ tuple-keyed `(asset, exchange)` → nested `transfer_fees[asset][exchange]` |
| `IRT_WITHDRAWAL_FLAT_IRR` | `irt_withdrawal_flat_irr` | same shape |
| `IRT_DEPOSIT_FEE_PCT` | `irt_deposit_fee_pct` | same shape |
| `IRT_DEPOSIT_FLAT_IRR` | `irt_deposit_flat_irr` | same shape |
| `PAIR_CONFIG` | `pairs` | same per-pair shape; keep `enabled` flags. Each pair also carries `ramzinex_pair_id` (integer; ramzinex orderbooks are keyed by pair_id, not symbol). |
| `NOBITEX_API_KEY`, `OMPFINEX_API_KEY`, `WALLEX_API_KEY`, `BITPIN_API_KEY`, `BITPIN_SECRET_KEY`, `RAMZINEX_API_KEY`, `RAMZINEX_SECRET_KEY` | `keys.<exchange>.api_key` / `keys.bitpin.secret_key` / `keys.ramzinex.secret_key` | ⚠ **Keep `engine.py` defaults EMPTY (`""`)**. Real keys go only into the n8n JSON and `config.sample.json`. Ramzinex needs **both** `api_key` + `secret_key` (it exchanges them for a JWT token). |

Edit only the values that actually differ — don't rewrite the whole block. Keep the existing comment style.

## Step 2 — Sync the math in `engine.py`

Compare these against the monolith and update if changed:

- `ArbitrageEngine.evaluate` — spread / fee / transfer / IRT-fee math and the returned dict keys (e.g. `irt_fee_pct`, `irt_fee_irr`).
- `_transfer_pct` — USDT amortizes over `min_usdt_transfer_amount`; other assets over `amount`.
- `_irt_transfer_fee_irr` — withdrawal (sell side) + deposit (buy side).
- `_exchange_fee` — stays **data-driven from `self.fees`**, not hardcoded constants. (Monolith reads `NOBITEX_FEE` etc.; the split reads `cfg["fees"]`. Keep it cfg-driven.) Note `nobitex` and `ramzinex` fees are nested by market type (`IRT`/`USDT`); the others are flat.
- `scan` — keep returning `(best_candidate, opportunities)` and keep the per-route logging (IRT vs USDT format).

## Step 3 — Sync the client files (only if the monolith changed them)

Per-exchange API specifics live in `nobitex.py` / `ompfinex.py` / `wallex.py` / `bitpin.py` / `ramzinex.py`. If the monolith changed an endpoint URL, payload shape, auth header, orderbook parsing, or the OmpFinex bids/asks swap, port that change. Use the base-class helpers (`_get_json`, `_post_json`, `parse_levels`, `sort_asks`, `sort_bids`) — do **not** reintroduce inline `aiohttp`/`_parse` code.

Ramzinex specifics to preserve: orderbook is the **public** endpoint keyed by integer `pair_id` (`buys` = bids, `sells` = asks), prices already in **Rial (IRR)** so `price_scale=1.0`; auth is `api_key` + `secret` → JWT `token` (fetched lazily, re-fetched once on a 401) sent as `x-api-key` + `Authorization2: Bearer <token>` headers; `CURRENCY_ID` maps asset → integer currency id for wallet queries.

## Step 4 — Regenerate the n8n config

Produce the n8n Code-node snippet (this is what gets pasted into the n8n workflow — it is **not** stored in the repo):

```js
return [{ json: { /* full config, mirroring DEFAULT_CONFIG */ } }];
```

Rules:
- Include **real API keys** here (from the monolith's `*_API_KEY` / `*_SECRET_KEY` constants), including `keys.ramzinex.api_key` + `keys.ramzinex.secret_key`.
- Include every config section: `dry_run`, `min_profit_pct`, `keys`, `fees`, `transfer_fees`, `trade_amount`, `min_usdt_transfer_amount`, `min_irt_transfer_irr`, `irt_withdrawal_flat_irr`, `irt_deposit_fee_pct`, `irt_deposit_flat_irr`, `pairs`.
- Use `null` (not `None`) for absent symbols like TON's `omf_symbol` / `bitpin_symbol`.
- Print it in the chat for the user to paste into n8n.

## Step 5 — Update `config.sample.json`

Update the tracked sample to match the new values so the repo has a current reference. (It may be a subset — keep its existing scope unless the user wants it expanded.)

## Step 6 — Verify

- `python3 -m py_compile engine.py base.py nobitex.py ompfinex.py wallex.py bitpin.py ramzinex.py` (the project's aiohttp lives in `.venv`; `py_compile` is the syntax check that doesn't need imports).
- Summarize exactly what changed (old → new per field) and reprint the n8n snippet.

## Deploy reminder

Python is baked into the Docker image at build time (`COPY . .`). After syncing or bootstrapping, the user must rebuild to pick up `engine.py`/client changes:

```bash
docker compose up -d --build arbitrage
```

The n8n JSON change takes effect immediately (it's in the n8n workflow, not the image) — no rebuild needed for that part.
