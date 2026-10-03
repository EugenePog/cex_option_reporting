# Seed data (core / settings tables)

CSV files here are loaded into the `core` schema by the seed loader. **Filename = table name.**

These CSVs are **version-controlled** (the folder is tracked in git). The `*.example.csv` files are
starting templates; edit them or create real `<table>.csv` files alongside them.

```bash
cd seed
for f in *.example.csv; do cp "$f" "${f%.example.csv}.csv"; done
# then edit user.csv, cex_account.csv, subaccount.csv, strategy.csv, strategy_rule.csv,
# contract_size.csv
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
- **Load order / dependencies:** `user` → `cex_account` → `subaccount` → `strategy` → `strategy_rule`;
  `contract_size` has no dependencies.
- `match_json` is JSON inside a CSV cell — wrap the whole cell in double quotes and double any inner
  quotes, e.g. `"{""inst_pattern"": ""BTC-USD-*""}"`.
- Tables NOT seeded here: `instrument` (populated by the silver pipeline), `audit_log` and
  `pipeline_watermark` (written by the app/pipeline), and `strategy_link` — the **manual strategy
  links (pins)** written by the admin **Box builder** tab.
- **`--replace` and manual pins:** `--replace` truncates with `CASCADE`, which would also empty
  `core.strategy_link` (and silver/gold, which are rebuildable — pins are not). The loader therefore
  **refuses `--replace` while pins exist**; seed without `--replace` (upsert by `id`), or pass
  `--wipe-links` if you really want to drop them.
- **Strategies created in the Box builder** (`+ New box` — a box is a strategy) live only in the database. Add them
  to `strategy.csv` (with their DB `id`) to keep them in git — a CSV row reusing the same `id` for
  something else would overwrite them on the next seed.
- **Boxes edited or deleted in the Box builder** (✎): a rename / recolor in the GUI is overwritten by
  the next `seed` if the box is in `strategy.csv` — change it there too. A **deleted** box is a soft
  delete (`deleted_at`, migration 0018): `seed` (upsert) does not bring it back because `deleted_at`
  is not a CSV column, but `seed --replace --wipe-links` does. Remove it (and its `strategy_rule.csv`
  rows) from the CSVs to drop it for good. Never delete the `unassigned` row of a subaccount.

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

**`contract_size`** — contract size per exchange (migration 0020; replaces the old hardcoded values)

| column | allowed values |
|---|---|
| `cex_code` | `OKX` (one row per exchange — sizes differ between exchanges) |
| `inst_type` | `OPTION` (also `FUTURES`, `SWAP`, `SPOT` — OKX instType names) |
| `underlying` | OKX instFamily, e.g. `BTC-USD`, `ETH-USD` (= the option's underlying) |
| `ct_val` | coin units per contract, > 0 — OKX options: BTC-USD `0.01`, ETH-USD `0.1`, SOL-USD `1` |
| `ct_val_ccy` | the coin `ct_val` is counted in, e.g. `BTC` |

The migration already inserts the three OKX option rows (ids 1–3), so seeding this file is only
needed to change or add rows. A leg whose (exchange, type, underlying) has no row gets no coin
size in gold (`size_coin` NULL — the chart then shows contracts) and the payoff report counts
1 contract = 1 coin; the pipeline logs a warning naming the missing row.

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

> **Precedence:** a manual pin set in the Box builder (`core.strategy_link`) always wins over
> every rule; rules decide only for legs without a pin. Rules are evaluated once per **position
> leg** (posId + open time): `side` is the leg direction and `opened_after/before` compare the
> leg's open time (OKX `cTime`), so all rows of a leg get the same strategy.

Example — tag all BTC-USD short calls opened in June as strategy 1:

```json
{"inst_pattern": "BTC-USD-*", "opt_type": "C", "side": "short",
 "opened_after": "2026-06-01", "opened_before": "2026-07-01"}
```

## Mapping to bronze

- `cex_account.label` must match `bronze.*.account_label` (e.g. `OKX_K`).
- `subaccount.subacct_name` must match `bronze.*.subacct_name`. For single-account API keys OKX
  returns an empty sub-account name, so leave `subacct_name` blank to match.
