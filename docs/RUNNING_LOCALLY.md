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

Uses the OKX_K_* account. Collection is split into two schedules plus a one-off backfill:

```bash
# Snapshot — point-in-time balance/positions/margin/greeks. Scheduler fires at each SNAPSHOT_TIMES_UTC
# entry (default "*:00" = every hour on the hour, UTC). Runs in the foreground:
make collect-snapshot-loop       # == python -m app.cli snapshot --loop

# History — fills/closed-positions/bills over a limited window (today + INGEST_DAILY_LOOKBACK_DAYS).
# Scheduler fires at each INGEST_TIME_UTC entry (default "*:00" = every hour; "10:00" = once a day):
make collect-loop                # == python -m app.cli history --loop

# Backfill — one-off, full available history depth (also snapshots current state), then the
# BTC-USD 1-minute index candles from the day of the earliest open/closed position:
make backfill                    # == python -m app.cli backfill   (--no-candles to skip candles)
```

For a quick manual pass without the scheduler: `python -m app.cli snapshot` or `python -m app.cli history`.

**BTC-USD index candles** (`bronze.raw_index_candle`, OKX public `history-index-candles`, no API key):
both loops (and the one-shot `snapshot` / `history`) top them up after the accounts at their usual
times; `make backfill` fills them from the earliest position. Only missing minutes are fetched
(≤100 per request, ~9 requests / 2 s), so the first backfill of ~3 months takes a few minutes.
Re-run or extend just the candles with `python -m app.cli index-candles [--since 2026-01-01]`.
Instruments: `INDEX_CANDLE_INST_IDS` (default `BTC-USD`; empty = off).

Check what landed (note the `mode` column: snapshot | history | backfill | candles | candles_backfill):

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select mode,status,row_count,started_at from bronze.ingest_run order by started_at desc limit 5;"
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select count(*) from bronze.raw_position;"
```

Run as background services with pm2 (two collector processes):

```bash
pm2 start ecosystem.config.js --only collector-snapshot,collector-history
pm2 logs
```

> pm2 tip: the ecosystem file runs `python -m app.cli ...`. Either activate the venv before
> `pm2 start`, or point it at the venv explicitly: `CEX_PYTHON=$(pwd)/.venv/bin/python pm2 start ecosystem.config.js`.

## 9. Run pipelines bronze→silver→gold ✅

```bash
make pipeline            # both stages: bronze->silver then silver->gold
make pipeline-silver     # only bronze->silver
make pipeline-gold       # only silver->gold
```

## 10. Start the web dashboard ✅

```bash
# one-time: give your seeded user a login password (seed created the user without one)
python -m app.cli set-password you@example.com     # prompts for password

make web                 # uvicorn app.web.main:app --reload  →  http://localhost:8000
```

Open http://localhost:8000, sign in, and you'll see the **Dashboard** (graphs ①–⑥: ① price with
strategy boxes, ② equity & daily P&L (②b in-kind), ③ payoff, ④ strike×expiry map, ⑤ greeks term
structure, ⑥ maturity ladder) and the **Analyze** tab (Ⓐ: filter-driven KPIs, strategy table, symbol
bars, deal drill-down). The Underlying / Account selectors open on the values the graphs use (the
underlying with the most legs; your only account, or all accounts). A `client` user sees only their
own subaccounts; an `admin` sees all.

> The dashboard reads the **gold** tables, so run `make pipeline` first (and keep the collectors
> running) so there's data to show.

An **admin** also gets a third tab, **Box builder** (http://localhost:8000/box-builder): pick the
account in the page header ("Box builder for account: …"), then move position legs — one by one or
as a multi-selection — between **boxes** (the GUI's name for the account's strategies) by drag & drop,
review the P&L impact, and apply. The leg filters in the Legs panel narrow the legs list only; each
box always shows all its legs. The ✎ next to a box name renames / recolors it, or deletes it: its legs
move to `unassigned` and the box is soft-deleted (Undo in *History* brings it back). Each apply writes manual pins to `core.strategy_link` (they beat every `strategy_rule`) and
recomputes silver + gold in the background.

## 10b. Upgrading an existing database to the Box builder (migrations 0016 + 0017)

```bash
pm2 stop all                      # old pipeline code can't run against the new schema
git pull                          # (or apply the patch) — then, in the venv:
make migrate                      # 0016 fill dedupe fix · 0017 position legs + strategy_link
python -m app.cli backfill        # re-collect history: recovers fills that the old
                                  # (cex_code, trade_id) key dropped — OKX keeps ~3 months of fills
make pipeline                     # builds silver.position_leg / gold.position_leg, links rows
pm2 start ecosystem.config.js
```

- **0016** — fills were deduped on `(cex_code, trade_id)`, but OKX `tradeId` is per instrument, so
  a fill sharing a tradeId with an older fill on another instrument was silently skipped. Key is
  now `(cex_code, inst_id, trade_id)` in bronze and silver.
- **0017** — the **position leg** (OKX `posId` + `cTime`) becomes the unit of strategy tagging;
  `core.strategy_link` holds manual pins; `silver.trade_fill.strategy_id` is removed (fills link
  to their leg via `position_leg_id`); closed positions are keyed on `posId + cTime`.

### 10c. Edit / delete boxes (migration 0018)

```bash
make migrate                      # 0018: core.strategy.deleted_at / deleted_by / deleted_changeset_id
pm2 restart pipeline web          # new code ignores rules of deleted boxes; ✎ edit / delete in the UI
```

### 10d. BTC-USD index candles (migration 0019)

```bash
make migrate                      # 0019: bronze.raw_index_candle
python -m app.cli index-candles   # first fill: from the day of the earliest position (or: make backfill)
pm2 restart collector-snapshot collector-history   # loops now top up the candles at their times
```

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select inst_id, count(*), min(ts), max(ts) from bronze.raw_index_candle group by 1;"
```

### 10e. Price with strategy boxes + contract size in core (migrations 0020 + 0021)

```bash
make migrate                      # 0020: core.contract_size (+ OKX BTC/ETH/SOL option rows)
                                  # 0021: silver/gold.index_candle, gold.box_shape, gold.position_leg cols
make pipeline                     # first run types all candles + builds the 1h/4h/1d bars (seconds)
pm2 restart pipeline web          # new pipeline steps + Dashboard graph ① "Price with strategy boxes"
```

Collectors: both schedules now default to **every hour on the hour** (`*:00`). If
your `.env` sets `SNAPSHOT_TIMES_UTC` / `INGEST_TIME_UTC`, it overrides the default — set them to `*:00`
(or delete the lines) and `pm2 restart collector-snapshot collector-history`.

Contract sizes now live in `core.contract_size` (one row per exchange / instrument type / underlying).
To change or add one, edit `seed/contract_size.csv` and run `python -m app.cli seed --table contract_size`,
then `make pipeline`.

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select bar, count(*), max(ts) from gold.index_candle group by 1;" \
  -c "select subaccount_id, strategy_id, n_legs, size_coin, coin from gold.box_shape;"
```

### 10f. Telegram alerts for graph ① boxes (migration 0022, pm2 `worker`)

The `worker` process sends a Telegram message when a box on graph ① "Price with strategy boxes"
(a) reaches its top strike, (b) its bottom strike, (c) gets within X % of the top, (d) within Y % of
the bottom, (e) reaches its last expiry, or (f) is N days before it. (Build 7: a, b, e take a
threshold too — e.g. a at 0.5 % fires 0.5 % below the top strike; e at 0.25 fires 6 h before.) Only boxes with at least one
open leg are watched; each message goes out once per crossing. Critical ones (a, b, e by default)
start with 🔴 **CRITICAL** (Telegram can't color text), the others with 🟡.

```bash
pip install -e ".[dev]"           # new dependency: python-telegram-bot
make migrate                      # 0022: core.notification_setting (+ 6 BTC-USD rows), core.notification_state
```

Add the bot to `.env` — the same token and chat id your existing monitor uses work:

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...  # from @BotFather
TELEGRAM_CHAT_ID=123456789        # your chat (a group id starts with -)
# optional, defaults shown:
# ALERTS_INTERVAL_SECONDS=60  ALERTS_REARM_MINUTES=60  ALERTS_EXPIRY_GRACE_HOURS=12
# ALERTS_MAX_PRICE_AGE_MINUTES=90
```

(Chat id unknown? Send your bot any message, then open
`https://api.telegram.org/bot<TOKEN>/getUpdates` — it is `result[].message.chat.id`.)

```bash
make alerts-test                  # one "alerts are set up" message in the chat
make alerts-dry-run               # what would be sent right now (sends and stores nothing)
pm2 start ecosystem.config.js --only worker    # or: pm2 restart worker
pm2 logs worker
```

Settings (per asset — every account and box of BTC-USD shares them) are in `core.notification_setting`
and are read on every check, e.g.:

```bash
docker exec -it cex_pg psql -U cex -d cex_option_reporting \
  -c "select id, asset, code, threshold, is_enabled, is_critical from core.notification_setting order by id;" \
  -c "update core.notification_setting set threshold = 3 where asset = 'BTC-USD' and code = 'near_top_pct';" \
  -c "update core.notification_setting set is_enabled = false where asset = 'BTC-USD' and code = 'last_expiry_days';" \
  -c "select * from core.notification_state order by last_sent_at desc nulls last limit 20;"
```

Keep `seed/notification_setting.csv` in step if you change values in the DB — `make seed` writes the
CSV values back.

### 10g. Per-box switch + Settings tab (migration 0023)

```bash
make migrate                      # 0023: core.strategy.send_notifications (default true — nothing changes)
pm2 restart web worker            # new Settings tab; worker skips boxes switched off
```

- **Per box:** Box builder → ✎ on a box (or **+ New box**) → **Send notifications** next to Color.
  Off = the worker ignores the box (no message, no state written; earlier state is kept). A box that is
  off shows 🔕 next to its name.
- **What to send:** the admin **Settings** tab → **Telegram notifications** — add, edit and delete
  rows (asset, notification a–f, threshold 0–100 for every code: % from the strike for a–d, days
  before the last expiry for e/f — 🆕 build 7: a, b, e use it too, so the critical tier can fire
  before the strike / expiry; on/off; critical; note). One row per asset + notification; the asset list is the same as the Dashboard's
  Underlying list (underlyings with positions in the accounts).

---

## Running everything as services (pm2)

```bash
pm2 start ecosystem.config.js    # collector-snapshot + collector-history + pipeline + web + worker
                                 # (worker = Telegram alerts, needs TELEGRAM_* in .env — see 10f)
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
