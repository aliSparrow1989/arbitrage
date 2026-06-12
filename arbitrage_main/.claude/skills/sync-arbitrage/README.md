# sync-arbitrage

Turns a monolithic `arbitrage.py` into — or back in sync with — this project's
separated layout (`base.py` + per-exchange clients + `engine.py` + `main.py` +
`Dockerfile` + `config.sample.json`), and regenerates the n8n config JSON.

It auto-detects what to do:

- **Split already exists** → syncs the new monolith's values/logic into the split files.
- **Only `arbitrage.py` exists** → bootstraps the whole split from scratch
  (one client file per exchange, comparison/arbitrage in `engine.py`,
  externalized config in `config.sample.json`, and a `Dockerfile`).

## Run it

```
/sync-arbitrage
```

or point it straight at the file:

```
/sync-arbitrage path/to/arbitrage.py
```

## Deploy it

```bash
docker compose up -d --build arbitrage
```
