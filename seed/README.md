# Seed data (core / settings tables)

CSV files here are loaded into the `core` schema by the seed loader. **Filename = table name.**

These CSVs are **version-controlled** (the folder is tracked in git). The `*.example.csv` files are
starting templates; edit them or create real `<table>.csv` files alongside them.

```bash
cd seed
for f in *.example.csv; do cp "$f" "${f%.example.csv}.csv"; done
# then edit user.csv, cex_account.csv, subaccount.csv, strategy.csv, strategy_rule.csv
```

> Note: `cex_account` credential columns are intentionally left blank — for dev the collector reads
> `OKX_K_*` from `.env`. Do **not** put plaintext API keys in these CSVs (they're in git).

## Load

From the repo root (venv active, DB migrated):

```bash
make seed                                # upsert all present CSVs (by id), in dependency order
python -m app.cli seed --table strategy  # load just one table
python -m app.cli seed --replace         # truncate the seed tables first, then load
```

## Rules

- **Include `id`** in every row. FKs reference these ids (e.g. `cex_account.user_id` → `user.id`),
  and the loader upserts on `id`, so re-running updates rather than duplicating. Sequences are reset
  to `max(id)` after each load.
- **Load order / dependencies:** `user` → `cex_account` → `subaccount` → `strategy` → `strategy_rule`.
- `match_json` is JSON inside a CSV cell — wrap the whole cell in double quotes and double any inner
  quotes, e.g. `"{""inst_pattern"": ""BTC-USD-*""}"`.
- Tables NOT seeded here: `instrument` (populated by the silver pipeline), `audit_log` and
  `pipeline_watermark` (written by the app/pipeline).

## Allowed values (data dictionary)

**`user`**

| column | allowed values |
|---|---|
| `role` | `client` (default) or `admin` — admin sees all clients / strategies |
| `is_active` | `true` / `false` |

**`cex_account`**

| column | allowed values |
|---|---|
| `cex_code` | `OKX` (more exchanges later, e.g. `BYBIT`) |
| `label` | free text, but must match `bronze.*.account_label` (e.g. `OKX_K`) |
| `flag` | `0` = live account, `1` = demo/simulated |

**`subaccount`**

| column | allowed values |
|---|---|
| `cex_code` | `OKX` |
| `subacct_name` | must match `bronze.*.subacct_name`; leave **blank** for single-account API keys |
| `is_active` | `true` / `false` |

**`strategy`**

| column | allowed values |
|---|---|
| `color` | hex color for the UI, e.g. `#4c9aff` |

**`strategy_rule`**

| column | allowed values |
|---|---|
| `strategy_id` | FK to a `strategy.id` in the **same** subaccount |
| `priority` | integer; **higher wins** when several rules match. Default `100` |
| `match_json` | matching condition (see below) |

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

Example — tag all BTC-USD short calls opened in June as strategy 1:

```json
{"inst_pattern": "BTC-USD-*", "opt_type": "C", "side": "short",
 "opened_after": "2026-06-01", "opened_before": "2026-07-01"}
```

## Mapping to bronze

- `cex_account.label` must match `bronze.*.account_label` (e.g. `OKX_K`).
- `subaccount.subacct_name` must match `bronze.*.subacct_name`. For single-account API keys OKX
  returns an empty sub-account name, so leave `subacct_name` blank to match.
