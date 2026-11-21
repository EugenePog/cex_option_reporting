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

> With the pm2 `pipeline` loop running, a manual run waits (silently) for the loop's current run —
> both take the same pipeline lock. Stop the loop first and don't suspend the command with Ctrl-Z
> (a suspended run keeps the lock): `pm2 stop pipeline && make pipeline-gold; pm2 start pipeline`.

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
To change or add one, edit `seed/common/contract_size.csv` (build 11: was `seed/contract_size.csv`) and
run `python -m app.cli seed --table contract_size`, then `make pipeline`.

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

Keep `seed/common/notification_setting.csv` (build 11: was `seed/notification_setting.csv`) in step if
you change values in the DB — `make seed` writes the CSV values back.

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

### 10h. Graph ① navigation like TradingView (build 9, no migration)

```bash
pm2 restart web                   # new static/chartnav.js + GET /api/price-candles
```

Then reload the Dashboard.

- **Move:** drag the chart. Dragging up / down also moves the prices (the **A** button under the price
  scale goes dark = prices no longer fit themselves).
- **Zoom:** click into the chart first (blue outline), then the wheel zooms **time** at the cursor and
  the prices fit. Before that click the wheel scrolls the page; Esc or a click outside releases it.
  On the price scale (right) the wheel or a drag up / down zooms **prices**; on the date scale (bottom)
  a drag left / right zooms **time**.
- **Mac trackpad:** two fingers left / right = move, up / down = zoom, pinch = zoom.
- **Back:** double-click the chart or **⟲** = the reporting period; double-click the price scale or
  **A** = prices fit again. **?** lists all gestures.
- **More data:** candles before / after the reporting period load as you move. Boxes stay those of the
  reporting period.
- **Candles:** 1H / 4H / 1D (calendar) / 1D (option) (default 1D (option); the Auto option was
  removed). The text next to the combobox shows the UTC window of each candle — fixed UTC boundaries:
  1H = HH:00–HH:59, 4H = 00:00–03:59 … 20:00–23:59, 1D (calendar) = 00:00–23:59, 1D (option) =
  08:00–07:59 next day (the option day: OKX options expire at 08:00 UTC). The 1D (option) bars are
  built by the pipeline (`gold.index_candle`, bar `1do`); the first run after the update builds them
  for the whole history.
- **Rev 2:** the date axis has twice as many ticks (e.g. every 3 days for a month), re-picked as you
  zoom. The dotted vertical line is **now** — hover it for the date and time (UTC and your local time);
  it moves on every minute.
- **Build 10 — graphs ② and ②b** work the same way on both of their charts (value scale on the
  left). With **Sync timelines** on (default) the two charts move together; one click gives the wheel
  to both. Days outside the reporting period load as you move. The **A ⟲ ?** buttons appear in the
  corner under the value scale while the mouse is over the chart. Both graphs now show times in UTC.

### 10i. Seeds per environment, matched on keys (build 11, migration 0024)

```bash
make migrate          # 0024: strategy.key + unique keys for the seeded core tables
make seed             # seed/common + seed/dev (APP_ENV, default dev) — matched on keys, no ids
pm2 restart web       # the Box builder shows each box's seed key
```

- **Folders:** `seed/common/` (contract sizes, Telegram notification settings — every environment),
  `seed/dev/`, `seed/test/`, `seed/prod/` (users, accounts, sub-accounts, boxes, rules). `APP_ENV` in
  `.env` picks the environment (`dev` | `test` | `prod`, default `dev`); `make seed ENV=test` for
  another one once. `seed/prod/` is empty for now. `seed/examples/` holds templates (not loaded).
- **No ids in the CSVs:** rows are matched on their keys — user e-mail, account `cex_code` + `label`,
  sub-account (account + `subacct_name`), box (sub-account + `key`), rule (sub-account + box +
  `match_json`), contract size, notification (asset + code). Parents are named by key
  (`user_email`, `cex_code` + `account_label` + `subacct_name`, `strategy_key`). The database assigns
  ids, so boxes made in the Box builder and rows added in Settings never clash with the seed.
- **First seed after the upgrade:** every existing row is matched — the output should say
  `0 new` for each table. Rows only in the database (e.g. boxes from the Box builder) stay as they are.
- If `make migrate` stops with *"these rows would break the new unique keys"*, merge or delete the
  listed duplicates (same account label twice, same sub-account twice, same rule twice) and run it
  again — nothing was changed.
- Full format and rules: `seed/README.md`.

### 10j. Faster bronze → silver (build 15, no migration)

```bash
pm2 restart pipeline   # nothing else — same tables, same results
```

- **Each bronze table is read once**, and only the JSON fields silver uses come from Postgres
  (`payload->>'…'`); greeks are loaded only for snapshot runs that have positions.
- **Bulk writes:** per table, all rows go in one `COPY` into a temporary staging table and one
  `INSERT … SELECT … ON CONFLICT` merges them — instead of one statement per row.
- **Unchanged rows are not rewritten** (`… DO UPDATE … WHERE (…) IS DISTINCT FROM (…)`), so a run
  leaves no dead rows behind. `silver.position_leg` (a few hundred rows) is still refreshed every
  run — its `updated_at` is the Box builder's "as of" time.
- **The pm2 loop skips silver** when no bronze row was added and none of the core rows silver
  reads (accounts, sub-accounts, boxes, rules, Box builder pins) changed since its last silver run.
  Gold still runs. A manual `make pipeline*` and the Box builder's recompute always run in full.
- Duplicate keys inside one run (two bronze rows for the same silver row): the newest bronze row
  wins. Before, the row that came last in the table's physical order won — the same in practice.
- **Log:** `silver <table>: N written (C new or changed), …` per table and `silver done in X s`
  (or `silver: skipped — no new bronze rows …`). Measured on 67k positions / 160k bronze rows:
  **158 s → 3–4 s**, silver identical row for row, and a second run changes 0 rows.

### 10k. Settings → Position hedged (build 16, migration 0025)

```bash
make migrate          # 0025: core.hedged_position (empty)
pm2 restart web       # Settings shows "Position hedged" above Telegram notifications
```

- **What it is:** the hedged amount **Q** per account and asset — how much of the asset (e.g. BTC)
  the account's options hedge. It is for the future-PnL profile planned next to graph ① (Q hedged by
  the options vs. the same Q without them); nothing draws it yet.
- **Where:** admin **Settings → Position hedged**. One row per account × asset — the assets the
  account has (or had) a balance in, the same list as graph ②b's asset selector. Type Q, then
  **Save** (or Enter); **Clear** (two clicks) removes it. Empty = not set; 0 is a value.
- **Balance now** is the account's latest balance snapshot of the asset, shown for reference only:
  OKX's balance (`eq`) on an options account includes the options' own PnL, so it is not the amount
  held — that is why Q is typed in.
- **Seeding (optional):** `seed/<env>/hedged_position.csv` (columns `cex_code, account_label,
  subacct_name, ccy, qty, note`; template in `seed/examples/`). `make seed` writes the CSV values
  over values typed in Settings for the same account + asset, and `seed --replace` empties the table
  (it references `core.subaccount`) — keep values you want to survive a replace in the CSV.
- Check: `psql "$DATABASE_URL" -c "select * from core.hedged_position order by subaccount_id, ccy;"`;
  changes are in `core.audit_log` (`settings.hedged.*`).

### 10l. Alerts can't be missed silently (build 17, migration 0026)

```bash
make migrate                          # 0026: codes g / h + core.notification_feed_state
pm2 restart pipeline worker web
python -m app.cli worker --dry-run    # optional: what would be sent now
```

- **Silver ignores a snapshot run that is still being written** (`bronze.ingest_run` status
  `RUNNING`, started < 10 min ago). Before, a pipeline run that fell between the collector's
  positions write and its balances write saw every leg as "stale": the box looked closed to the
  worker for one pipeline cycle — its price alerts were not checked, then re-armed and sent again
  (e.g. "BTC-USD reached the bottom strike" every ~1–2 h while the price stayed below). The run is
  picked up by the next pipeline run once it has finished. A run still `RUNNING` after 10 min
  (crashed collector) counts as finished, and a run that ended in `ERROR` counts as before — so
  this can never hide legs. Silver reads its sources in one consistent snapshot (REPEATABLE READ).
  The alert rules themselves are unchanged.
- **New notifications** in **Settings → Telegram notifications**, per asset, threshold in
  **minutes** (1–1440):
  - `g · no_data_warning` (default 10 min) — 🟡 `⚠ alerts can't check BTC-USD: no price for 12 min`
  - `h · no_data_error` (default 30 min, critical) — 🔴 `CRITICAL · ⚠ alerts can't check …`

  "No price" = the newest price the worker has — the live OKX index, else the newest stored
  1-minute candle — is older than the threshold (no price at all = at once). The message says what
  the worker has and whether the box price alerts run on that old price or are not checked at all.
  Each is sent once per outage (h alone if both are due at once); when a fresh price is back,
  one `✅ alerts can check BTC-USD again` follows. Checked for every exchange × asset that has
  alive boxes with alerts on. The migration adds g and h, switched on, for every asset that already
  has notification settings; `seed/common/notification_setting.csv` has them for BTC-USD.
- Not covered by g / h: the worker process itself being down (it can't report that) — keep an eye on
  `pm2 status`, or add an external uptime check.

### 10m. One account at a time + graph ① future-PnL panel (build 18, no migration)

```bash
pm2 restart web
```

- **Account selector** (Dashboard, Analyze): no *All* any more — accounts sorted by name, the first
  is the default. The Underlying / ②b asset (Dashboard) and Symbol / Strategy (Analyze) lists show
  what that account has.
- **Graph ①, right of the chart:** *PnL at expiry* if BTC ends at that price — the account's Q
  (Settings → Position hedged, §10k) with the open options of the boxes **in the legend and
  switched on** (a grey box drops out; the panel updates on every legend click), from the current
  price, no premiums. Green / red = profit / loss with options; the band out to "BTC only" is dark green where
  the options save, brown where they cost. White lines = open strikes. Hover = values in USD and
  BTC; click = pin (click again or Esc to unpin). Drag / wheel inside the panel move / zoom the
  prices only. Q not set → the panel shows the options alone ("Q not set").
- Check: `select * from core.notification_feed_state;` (armed / sent per exchange × asset × code)
  and `scripts/check_alert.sql` for a box alert.

### 10n. Auto-refresh + exact last-price time (build 19, no migration)

```bash
pm2 restart web
```

- **The Dashboard and the Analyze tab redraw themselves** every `PIPELINE_INTERVAL_SECONDS` (new data
  can't arrive faster than the pipeline writes it); the subtitle shows "Updates every 5 min · last
  HH:MM". Not while the browser tab is in the background (it catches up when you come back) and not
  in the middle of a drag. Your zoom, the values scale, boxes switched off in graph ①'s legend, a
  pinned PnL read-out, the payoff expiry and an open deals list all stay. Change the cadence with
  `PIPELINE_INTERVAL_SECONDS` and restart `pipeline` and `web`.
- **Last price** (label on graph ①, start of the PnL panel): the close of the newest stored
  1-minute index candle, timed at the END of that minute (the time the price is from). With
  snapshots every minute it is at most ~1 minute + one pipeline interval old.

### 10o. Gold rebuild without off-time (build 20, no migration)

```bash
pm2 restart pipeline   # web / worker need nothing
```

- Gold is still rebuilt in full on every run, in one transaction — but the old rows are now removed
  with `DELETE` instead of `TRUNCATE`. Pages, API calls and the alerts worker no longer wait for the
  rebuild: until it commits they read the previous gold in full, then the new gold appears all at
  once (never empty, never half-built). A slow report query no longer holds the rebuild up either.
- Gold `id`s keep counting up instead of restarting at 1 each run (nothing uses them).
- The old rows become dead rows that autovacuum removes; gold tables settle at about twice their
  live size. Check: `select relname, n_live_tup, n_dead_tup, last_autovacuum from
  pg_stat_user_tables where schemaname = 'gold' order by n_dead_tup desc;`
- To confirm nothing waits on gold any more, run this during a gold run — it should return no rows:
  `select pid, now() - query_start, wait_event, left(query, 60) from pg_stat_activity
  where wait_event_type = 'Lock' and query ilike '%gold.%';`

### 10p. Premiums, fees and realized PnL in the PnL panel (build 21, migration 0027)

**Nothing to re-collect.** The collectors store OKX's whole message in bronze, so the fields this
build needs are already there for all history: positions `pnl` / `realizedPnl` (every snapshot) and
fills `fillIdxPx`. Bronze is not touched; the pipeline reads them into new silver columns.

1. Check what bronze holds (optional):

   ```bash
   docker exec -it cex_pg psql -U cex -d cex_option_reporting -c "
   select count(*) rows, count(payload->>'realizedPnl') realized, count(payload->>'pnl') pnl
     from bronze.raw_position;" -c "
   select count(*) fills, count(payload->>'fillIdxPx') idx_px from bronze.raw_trade_fill;"
   ```

   Fills without `fillIdxPx` (if any) are priced from the 1-minute index candles instead.

2. Upgrade:

   ```bash
   pm2 stop pipeline
   make migrate          # 0027: 2 columns on silver.position_snapshot, 1 on silver.trade_fill,
                         #       4 on gold.position_leg — adds columns, deletes nothing
   make pipeline         # one-off: silver rewrites its existing snapshot / fill rows once to fill
                         # the new columns (longer than a normal run), then gold computes the cash
   pm2 restart pipeline web
   ```

3. Check the result:

   ```bash
   docker exec -it cex_pg psql -U cex -d cex_option_reporting -c "
   select status, cash_source, count(*), round(sum(open_cash_usd)) open_usd,
          round(sum(realized_cash_usd)) realized_usd, sum(cash_missing_px) unpriced
     from gold.position_leg group by 1, 2 order by 1, 2;"
   ```

   `cash_source`: `fills` = from the trades (exact), `snapshots` = open legs replayed from position
   snapshots (trades not all collected, e.g. opened before collection started, or the newest trade
   not in yet — switches to `fills` after the next history run), `summary` = closed legs from
   positions-history (whole fee at the closing price). `unpriced` should be 0.

**On graph ①:** *Premiums & fees (open)* (on by default) and *Realized (closed)* (off by default) above the chart. Premiums
on = open options counted from their entry with the premium received / paid and fees (so the V
need not meet at today's price); Realized on = + realized PnL of options closed in the period. All
in USD at the index price of each trade. The hover splits *Options* into *at expiry*, *premiums &
fees (open)* and *realized in period*. Q (Settings → Position hedged) must not include option cash.

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
