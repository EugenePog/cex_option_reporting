# Seed data (core / settings tables)

CSV files here are loaded into the `core` schema by the seed loader. **Filename = table name.**
These CSVs are **version-controlled** (the folder is tracked in git).

> **Build 11 — new layout and format.** Seeds are split by environment, and the CSVs carry **no
> ids**: every row is matched on its natural key and parents are named by their keys, so rows
> added in the GUI (Box builder, Settings) never clash with the seed. Migration **0024** adds the
> keys (`make migrate` before the first `make seed`).

## Layout

```
seed/
  common/     rows every environment gets — contract_size.csv, notification_setting.csv
  dev/        rows of the dev environment    — user, cex_account, subaccount, strategy, strategy_rule
  test/       rows of the test environment   — (same tables)
  prod/       rows of production             — empty for now (see prod/README.md)
  examples/   templates for all seven tables (not loaded)
```

`make seed` loads `common/` and then the environment's folder. The environment is `APP_ENV` in
`.env` (`dev` | `test` | `prod`, default `dev`). A table can have a file in both layers: the
environment's rows come second, and a row with the same key as a common row **overrides** it
(e.g. `prod/notification_setting.csv` with stricter thresholds). Every environment behaves the
same — match on keys, `--replace` allowed.

## Load

From the repo root (venv active, DB migrated):

```bash
make seed                                   # seed/common + seed/$APP_ENV (default dev)
make seed ENV=test                          # another environment, once
python -m app.cli seed --env prod           # same, from the CLI
python -m app.cli seed --table strategy     # just one table (both layers)
python -m app.cli seed --replace            # truncate the seed tables first, then load
```

The command prints, per table, how many rows were **new** and how many **updated**, and from which
files. If a row can't be loaded (unknown parent, empty key, old format) **nothing is written** and
the error names the file and line.

## Format: keys, not ids

- **No `id` column.** The database assigns ids. (An `id` column left in a file is ignored with a
  warning; the old parent-id columns `user_id`, `cex_account_id`, `subaccount_id`, `strategy_id` are
  refused — name the parent instead.)
- **A row is "the same row" when its key matches** — it is updated; otherwise it is added. Rows that
  are only in the database (e.g. boxes created in the Box builder) are left alone.
- **Parents are named by their keys**:

| file | matched on | columns that name the parent |
|---|---|---|
| `user.csv` | `email` | – |
| `cex_account.csv` | `cex_code` + `label` | `user_email` |
| `subaccount.csv` | account + `subacct_name` | `cex_code`, `account_label` |
| `strategy.csv` | sub-account + `key` (blank key = the name) | `cex_code`, `account_label`, `subacct_name` |
| `strategy_rule.csv` | sub-account + box + `match_json` | `cex_code`, `account_label`, `subacct_name`, `strategy_key` |
| `contract_size.csv` | `cex_code` + `inst_type` + `underlying` | – |
| `notification_setting.csv` | `asset` + `code` | – |

- **Load order / dependencies:** `user` → `cex_account` → `subaccount` → `strategy` → `strategy_rule`;
  `contract_size` and `notification_setting` have no dependencies. A parent must be in the database
  already or earlier in the same load (its own CSV).
- **Only the columns a file has are written** — e.g. leave `send_notifications` out of
  `strategy.csv` to keep what the Box builder's ✎ dialog set.
- `match_json` is JSON inside a CSV cell — wrap the whole cell in double quotes and double any inner
  quotes, e.g. `"{""inst_pattern"": ""BTC-USD-*""}"`.

## Boxes (`strategy.csv`) and the Box builder

- **`key`** is a box's fixed name for the seed files (column `core.strategy.key`, migration 0024).
  It is set when the box is created — from the CSV (`key`, or the name when blank), or in the Box
  builder from the name you give it — and **renames don't change it**. The Box builder's ✎ dialog
  shows it ("Seed key"). Existing boxes got their name as key in 0024.
- **Boxes created in the Box builder** live only in the database. To keep one in git, add a row to
  `seed/<env>/strategy.csv` with its account and its key — it is then matched, not duplicated.
- **Boxes edited in the Box builder** (✎): a rename / recolor is overwritten by the next `seed` if
  the box is in `strategy.csv` (matched by key) — change it there too.
- **Boxes deleted in the Box builder** (soft delete, `deleted_at`, migration 0018): `seed` updates
  the row but keeps it deleted (`deleted_at` is not a CSV column); `seed --replace --wipe-links`
  brings it back. Remove it (and its `strategy_rule.csv` rows) from the CSVs to drop it for good.
  Never delete the `unassigned` row of a sub-account.

## Other rules

- Tables NOT seeded here: `instrument` (populated by the silver pipeline), `audit_log`,
  `pipeline_watermark` and `notification_state` (written by the app / pipeline / alerts worker), and
  `strategy_link` — the **manual strategy links (pins)** written by the admin **Box builder** tab.
- **`--replace` and manual pins:** `--replace` truncates with `CASCADE`, which would also empty
  `core.strategy_link` (and silver/gold, which are rebuildable — pins are not). The loader therefore
  **refuses `--replace` while pins exist**; seed without `--replace` (match on keys), or pass
  `--wipe-links` if you really want to drop them.
- **Settings tab rows** (`notification_setting`): a change made in the admin **Settings → Telegram
  notifications** editor is overwritten by the next `make seed` if the CSV still has the old values
  for that asset + code — change it there too.
- `cex_account` credential columns are not in the CSVs — the collector reads `<LABEL>_*` from
  `.env`. Do **not** put API keys in these files (they're in git).
- `seed/*.csv` directly in `seed/` (the old place) are no longer loaded; the loader warns about them.

## Allowed values (data dictionary)

**`user`**

| column | allowed values |
|---|---|
| `email` | the login; **key** |
| `role` | `client` (default) or `admin` — admin sees all clients / strategies |
| `display_name` | free text |
| `is_active` | `true` / `false` |

**`cex_account`**

| column | allowed values |
|---|---|
| `user_email` | the owner — an `email` from `user.csv` / the database |
| `cex_code` | `OKX` (more exchanges later, e.g. `BYBIT`); **key** with `label` |
| `label` | free text, but must match `bronze.*.account_label` (e.g. `OKX_K`) |
| `flag` | `0` = live account, `1` = demo/simulated |

**`subaccount`**

| column | allowed values |
|---|---|
| `cex_code`, `account_label` | the account (`cex_account.csv`) |
| `subacct_name` | must match `bronze.*.subacct_name`; leave **blank** for single-account API keys; **key** with the account |
| `display_name` | free text — shown as the account name in the GUI |
| `is_active` | `true` / `false` |

**`strategy`**

| column | allowed values |
|---|---|
| `cex_code`, `account_label`, `subacct_name` | the sub-account (`subaccount.csv`) |
| `key` | the box's fixed seed key; blank = `name`; **key** with the sub-account |
| `name` | shown in the GUI; `unassigned` = the sub-account's fallback box (required, one per sub-account) |
| `description` | free text |
| `color` | hex color for the UI, e.g. `#4c9aff` |
| `send_notifications` | optional (migration 0023): `true` / `false` — Telegram alerts for this box. Leave the column out to keep what the Box builder's ✎ dialog set; if the column is there, `make seed` writes it back. New boxes default to `true`. |

**`strategy_rule`**

| column | allowed values |
|---|---|
| `cex_code`, `account_label`, `subacct_name` | the sub-account the rule tags legs of |
| `strategy_key` | the `key` of a box in the **same** sub-account |
| `match_json` | matching condition (see below); **key** with the sub-account and the box |
| `priority` | integer; **higher wins** when several rules match. Default `100` |

**`contract_size`** — contract size per exchange (migration 0020; replaces the old hardcoded values)

| column | allowed values |
|---|---|
| `cex_code` | `OKX` (one row per exchange — sizes differ between exchanges) |
| `inst_type` | `OPTION` (also `FUTURES`, `SWAP`, `SPOT` — OKX instType names) |
| `underlying` | OKX instFamily, e.g. `BTC-USD`, `ETH-USD` (= the option's underlying) |
| `ct_val` | coin units per contract, > 0 — OKX options: BTC-USD `0.01`, ETH-USD `0.1`, SOL-USD `1` |
| `ct_val_ccy` | the coin `ct_val` is counted in, e.g. `BTC` |
| `note` | free text |

The migration already inserts the three OKX option rows, so seeding this file is only needed to
change or add rows. A leg whose (exchange, type, underlying) has no row gets no coin size in gold
(`size_coin` NULL — the chart then shows contracts) and the payoff report counts 1 contract = 1 coin;
the pipeline logs a warning naming the missing row.

**`notification_setting`** — Telegram alerts for the boxes of Dashboard graph ① (migration 0022)

One row per **asset × code**; every account and box of that asset uses it. The alerts worker
(`python -m app.cli worker --loop`, pm2 `worker`) reads the table on every check, so a change in the
DB takes effect within a minute — no restart. An asset without rows gets no alerts.

| column | allowed values |
|---|---|
| `asset` | the option underlying / index, e.g. `BTC-USD` (same as `underlying` elsewhere) |
| `code` | `top_strike` (a, critical tier: price ≥ top strike − X %), `bottom_strike` (b, critical: price ≤ bottom strike + Y %), `near_top_pct` (c, early tier: price ≥ top − X %), `near_bottom_pct` (d, early: price ≤ bottom + Y %), `last_expiry` (e, critical: time ≥ last expiry − N days), `last_expiry_days` (f, early: time ≥ last expiry − N days) |
| `threshold` | 0–100, used by **every** code (build 7): X / Y in **percent** for a–d (e.g. `2` = 2 % from the strike towards the inside of the box; `0` = at the strike), N in **days** for e / f (may be fractional, `0.5` = 12 h; `0` = at the expiry). Defaults: a, b, e `0`; c, d `2`; f `1` |
| `is_enabled` | `true` / `false` — on / off |
| `is_critical` | `true` / `false` — critical messages start with 🔴 **CRITICAL** (Telegram has no red text), others with 🟡; default true for a, b, e |
| `note` | free text |

The migration already inserts the six BTC-USD rows (all on; a, b, e critical; X = Y = 2; N = 1), so
seeding this file is only needed to change them or add assets (e.g. six more rows for `ETH-USD`).

### `strategy_rule.match_json` vocabulary

A JSON object of conditions. Multiple keys are **AND-ed** (all must hold). Evaluated by the silver
tagger; the first matching rule (by highest `priority`) assigns the strategy, otherwise the position
falls into the `unassigned` strategy.

```jsonc
{"inst_pattern": "BTC-USD-*"}                        // glob on inst_id
{"opt_type": "C"}                                    // calls only ("C" | "P")
{"side": "short"}                                    // "long" | "short"
{"underlying": "BTC-USD"}                            // exact underlying
{"opened_after": "2026-06-01", "opened_before": "2026-07-01"}  // opened-time window (UTC dates)
```

> **Precedence:** a manual pin set in the Box builder (`core.strategy_link`) always wins over
> every rule; rules decide only for legs without a pin. Rules are evaluated once per **position
> leg** (posId + open time): `side` is the leg direction and `opened_after/before` compare the
> leg's open time (OKX `cTime`), so all rows of a leg get the same strategy.

Example — tag all BTC-USD short calls opened in June as the box `daily-short-straddle`:

```csv
cex_code,account_label,subacct_name,strategy_key,match_json,priority
OKX,OKX_K,,daily-short-straddle,"{""inst_pattern"": ""BTC-USD-*"", ""opt_type"": ""C"", ""side"": ""short"", ""opened_after"": ""2026-06-01"", ""opened_before"": ""2026-07-01""}",100
```

## Mapping to bronze

- `cex_account.label` must match `bronze.*.account_label` (e.g. `OKX_K`).
- `subaccount.subacct_name` must match `bronze.*.subacct_name`. For single-account API keys OKX
  returns an empty sub-account name, so leave `subacct_name` blank to match.
