# cex_option_reporting

Multi-tenant portal for CEX option strategy analytics — per-user **deals, PnL, balances, and graphs**.
Users are isolated by CEX sub-accounts. **OKX first**, designed to extend to other exchanges.

Architecture, ERD, and design decisions live in the companion knowledge folder
(`Documents/claude/Projects/cex_option_reporting`): `ARCHITECTURE.md`, `REPO_STRUCTURE.md`, `CONTEXT.md`, `diagrams/`.

## Stack (short version)

Python 3.12 · PostgreSQL 16 (medallion: bronze / silver / gold + core settings) · SQLAlchemy 2 + Alembic ·
FastAPI + Jinja templates + Plotly, session-cookie login (bcrypt) · Python / SQLAlchemy pipelines on
APScheduler · Telegram alerts · pm2 process management. See `ARCHITECTURE.md` for the full rationale.

## Quickstart (dev)

```bash
# 1. environment
python -m venv .venv && source .venv/bin/activate
make install                       # pip install -e ".[dev]"
cp .env.example .env               # then fill in keys (see comments in the file)

# 2. database
make db-up                         # local Postgres via docker compose
make migrate                       # alembic upgrade head (0001 … 0024)
make seed                          # seed/common + seed/<APP_ENV> into core (make seed ENV=test)
python -m app.cli set-password you@example.com   # login password for a seeded user

# 3. run pieces
python -m app.cli snapshot         # one snapshot collection pass (make collect-snapshot-loop = scheduled)
python -m app.cli history          # one history pass (fills / closed / bills; make collect-loop = scheduled)
make backfill                      # one-off: full history depth + index candles from the earliest position
make pipeline                      # one bronze->silver->gold pass
make web                           # dev web server at http://localhost:8000
make help                          # every make target
```

Full local setup and operations: `docs/RUNNING_LOCALLY.md`.

## Run under pm2 (prod-like)

```bash
pm2 start ecosystem.config.js      # collector-snapshot, collector-history, pipeline, web, worker
pm2 logs
```

## Layout

```
app/
  cli.py        Typer entrypoints (init-db, seed, snapshot, history, set-password, backfill,
                index-candles, pipeline, worker)
  connectors/   CEX abstraction (BaseCexConnector -> okx/; bybit/ is an empty placeholder)
  ingestion/    collectors + schedulers -> bronze (incl. BTC-USD 1m index candles)
  pipelines/    bronze -> silver -> gold (Python / SQLAlchemy)
  domain/       pure logic (instruments, contract sizes, legs, strategy rules, boxes, alerts,
                Black-76 pricing, metrics) — no I/O
  db/           SQLAlchemy engine + ORM models per layer (models.py = bronze, models_core/silver/gold),
                seed loader
  web/          FastAPI app: Dashboard ①–⑥, Analyze Ⓐ, admin Box builder ⑦ + Settings; JSON report API
  worker/       Telegram alerts for graph ① boxes
config/         settings (env-driven, per-account credentials from <LABEL>_* env vars), logging
migrations/     Alembic — single source of truth for schema
seed/           core settings as CSVs: common/, dev/, test/, prod/, examples/
scripts/        debug_expiries / debug_payoff (make targets), OKX smoke checks
tests/          config / connectors / db / domain / ingestion / web / worker
docs/           RUNNING_LOCALLY.md
```

Adding a new exchange = one new folder under `app/connectors/` implementing `BaseCexConnector`,
registered in `app/connectors/factory.py`.
