# cex_option_reporting

Multi-tenant portal for CEX option strategy analytics — per-user **deals, PnL, balances, and graphs**.
Users are isolated by CEX sub-accounts. **OKX first**, designed to extend to other exchanges.

Architecture, ERD, and design decisions live in the companion knowledge folder
(`Documents/claude/Projects/cex_option_reporting`): `ARCHITECTURE.md`, `REPO_STRUCTURE.md`, `CONTEXT.md`, `diagrams/`.

## Stack (short version)

Python 3.12 · PostgreSQL 16 (medallion: bronze/silver/gold) · FastAPI + fastapi-users ·
SQL + pandas pipelines · pm2 process management. See `ARCHITECTURE.md` for the full rationale.

## Quickstart (dev)

```bash
# 1. environment
python -m venv .venv && source .venv/bin/activate
make install                       # pip install -e ".[dev]"
cp .env.example .env               # then fill in keys (see comments in the file)

# 2. database
make db-up                         # local Postgres via docker compose
make migrate                       # alembic upgrade head   (once migrations exist)

# 3. run pieces
make collect                       # one bronze collection pass
make pipeline                      # one bronze->silver->gold pass
make web                           # dev web server at http://localhost:8000
```

## Run under pm2 (prod-like)

```bash
pm2 start ecosystem.config.js      # collector + pipeline + web (+ worker)
pm2 logs
```

## Layout

```
app/
  connectors/   CEX abstraction (BaseCexConnector -> okx/, bybit/ ...)
  ingestion/    collector -> bronze
  pipelines/    bronze -> silver -> gold
  domain/       pure business logic (PnL, greeks, instrument parsing) — no I/O
  db/           SQLAlchemy engine, models, tenant-scoped repositories
  web/          FastAPI app (API + Jinja/HTMX UI) + auth
  worker/       optional alerts / reports
config/         settings, logging, secrets (Fernet)
migrations/     Alembic — single source of truth for schema
sql/            hand-written pipeline SQL (silver/, gold/)
tests/          unit / connectors / pipelines / web
```

Adding a new exchange = one new folder under `app/connectors/` implementing `BaseCexConnector`.
