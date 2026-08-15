# Running Locally — Step by Step

> **Status of the repo:** this is a **scaffold**. The connector layer and configuration are
> implemented and testable *today*. The web server (`app/web/main.py`), database migrations
> (Alembic), and the collector/pipeline bodies are **not built yet** — steps that depend on them
> are marked **[needs implementation]** with what to build.

---

## 0. Prerequisites (install once on your Mac)

- **Python 3.12+** — the project requires it (`pyproject.toml`: `requires-python >=3.12`).
  Check: `python3 --version`. If older, install via [pyenv](https://github.com/pyenv/pyenv)
  (`brew install pyenv && pyenv install 3.12.5`) or python.org.
- **Docker Desktop** — to run Postgres locally. Check: `docker --version`.
  (Alternative: a native Postgres 16 via `brew install postgresql@16` — then skip the compose step
  and point `DATABASE_URL` at it.)
- **Node.js + pm2** — only needed to run everything as background services.
  `brew install node && npm install -g pm2`. Not required for dev.
- **git** — the repo is already cloned at `~/Documents/projects/cex_option_reporting`.

---

## 1. Open the project

```bash
cd ~/Documents/projects/cex_option_reporting
```

## 2. Create and activate a virtual environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate        # (.venv) should appear in your prompt
python --version                 # confirm 3.12.x
```

## 3. Install dependencies

```bash
pip install --upgrade pip
make install                     # == pip install -e ".[dev]"
```

This installs runtime deps (FastAPI, SQLAlchemy, pandas, python-okx, …) and dev tools
(ruff, black, mypy, pytest). Takes a minute or two the first time.

## 4. Create your `.env`

```bash
cp .env.example .env
```

Generate the two secrets and paste them into `.env`:

```bash
# Fernet key (encrypts CEX API credentials at rest)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# App secret (signs web sessions / JWTs)
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set `CREDENTIALS_FERNET_KEY=` and `APP_SECRET_KEY=` to those values. Leave the default
`DATABASE_URL` as-is if you use the Docker Postgres below.

## 5. Start Postgres

```bash
make db-up                       # docker compose up -d  (Postgres 16 + Adminer)
docker compose ps                # both containers 'running'/'healthy'
```

- Postgres → `localhost:5432` (user `cex`, password `cex`, db `cex_option_reporting`).
- Adminer (web DB browser) → http://localhost:8080 (System: PostgreSQL, Server: `db`).

Stop later with `make db-down`.

## 6. Verify the parts that work today ✅

**Connector smoke test** (no DB, no network, no exchange keys needed):

```bash
python scripts/smoke_connector.py
```

Expected: it prints a normalized balance row, a position row, the parsed underlying, and `SMOKE OK`.
This proves the CEX abstraction, factory registration, and OKX mappers work.

**Run the test suite / linters:**

```bash
make test        # pytest
make lint        # ruff
make typecheck   # mypy
```

---

## 7. Create the bronze tables ✅

Two ways; pick one:

```bash
make migrate      # alembic upgrade head — applies migrations/0001_bronze (recommended)
# or, quick dev bootstrap straight from the ORM models:
make init-db      # python -m app.cli init-db  (creates schemas + tables, no migration history)
```

Verify the tables exist:

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting -c "\dt bronze.*"
```

You should see `bronze.ingest_run` and `bronze.raw_balance/position/margin/opt_summary/trade_fill`.

## 8. Collect data → bronze ✅

Uses the OKX_K_* account. Two modes:

```bash
# Mode A — one daily pass now: current snapshot (positions/balances/margin/greeks) + recent fills
make collect                     # == python -m app.cli collect

# Mode A (scheduled) — run the daily scheduler in the foreground (fires at INGEST_HOUR_UTC, default 10:00 UTC)
make collect-loop                # this is exactly what the pm2 "collector" process runs

# Mode B — one-off full history backfill (pages fills back as far as OKX allows)
make backfill                    # == python -m app.cli backfill
```

Check what landed:

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select mode,status,row_count,started_at from bronze.ingest_run order by started_at desc limit 5;"
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select count(*) from bronze.raw_position;"
```

Run it as a background service with pm2 (Mode A on a schedule):

```bash
pm2 start ecosystem.config.js --only collector
pm2 logs collector
```

> pm2 tip: the ecosystem file runs `python -m app.cli ...`. Either activate the venv before
> `pm2 start`, or point it at the venv explicitly: `CEX_PYTHON=$(pwd)/.venv/bin/python pm2 start ecosystem.config.js`.

## 9. Run pipelines bronze→silver→gold — **[needs implementation]**

```bash
python -m app.cli pipeline       # once app/pipelines/runner.py is implemented
```

## 10. Start the web app — **[needs implementation]**

There is no `app/web/main.py` yet, so `make web` will fail today. Once the FastAPI app exists:

```bash
make web                         # uvicorn app.web.main:app --reload  →  http://localhost:8000
```

---

## Running everything as services (pm2) — after the above are implemented

```bash
pm2 start ecosystem.config.js    # collector + pipeline + web (+ worker)
pm2 status
pm2 logs
pm2 stop all
```

> Tip: pm2 uses whatever `python3`/`uvicorn` is first on PATH. Either activate the venv before
> `pm2 start`, or set absolute interpreter paths (e.g. `.venv/bin/python`) in `ecosystem.config.js`.

---

## What to build next to reach a runnable web app (shortest path)

1. **Alembic + `0001_core` migration** — at least `core.user`, `cex_account`, `subaccount`, `strategy`.
2. **`app/db/`** — SQLAlchemy engine/session + models mirroring the migration.
3. **Minimal `app/web/main.py`** — FastAPI app with a health route, then login + one dashboard page.
4. **`app/ingestion/collector.py`** — wire the OKX connector to a `bronze_writer`.

That gives a vertical slice: OKX → bronze → (silver/gold) → one authenticated dashboard.
See `ARCHITECTURE.md` §8 for the full build order.
```
