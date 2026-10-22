"""Load core/settings data from CSV files into the DB (build 11: layered folders, natural keys).

Folders under `seed/` (filename = table name):

    common/   rows every environment gets (contract sizes, notification settings, …)
    dev/      rows of one environment — picked by APP_ENV (default `dev`), or `--env`
    test/
    prod/

A table's rows are those of `common/<table>.csv` followed by those of `<env>/<table>.csv`; an
environment row with the same key as a common row overrides it. Every environment behaves the same
(upsert, `--replace` allowed).

The CSVs carry **no ids**. Each row is matched on its natural key — updated when the row exists,
added otherwise (the database assigns the id) — and parents are named by their keys:

    table                 matched on                          parent columns in the CSV
    user                  email                               –
    cex_account           cex_code + label                    user_email
    subaccount            account + subacct_name              cex_code, account_label
    strategy              sub-account + key (blank = name)    cex_code, account_label, subacct_name
    strategy_rule         sub-account + box + match_json      … + strategy_key
    contract_size         cex_code + inst_type + underlying   –
    notification_setting  asset + code                        –

So rows added in the GUI (boxes in the Box builder, rows in Settings) never clash with the seed:
they get their own ids, and rows only in the database are left alone. A CSV row whose parent
can't be found stops the whole load (nothing is written) and names the file and line. Only the
columns a CSV has are written (e.g. `strategy.send_notifications` only when the column is there).

`instrument` is derived by the silver pipeline; `audit_log` / `pipeline_watermark` /
`notification_state` are app-written; `strategy_link` holds the Box builder's manual pins —
app-written, never seeded.

`--replace` truncates with CASCADE, which also empties every table referencing core.strategy —
including core.strategy_link. Manual pins are NOT rebuildable from bronze, so replace refuses to run
while pins exist unless explicitly allowed (`wipe_links=True` / `--wipe-links`).
"""
from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, select, text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import session_scope
from app.db.models_core import (
    CexAccount,
    ContractSize,
    CoreUser,
    NotificationSetting,
    Strategy,
    StrategyRule,
    Subaccount,
)

logger = logging.getLogger(__name__)

COMMON = "common"              # the layer every environment loads first

# (table name / csv filename stem, model) in FK dependency order.
SEED_TABLES: list[tuple[str, type]] = [
    ("user", CoreUser),
    ("cex_account", CexAccount),
    ("subaccount", Subaccount),
    ("strategy", Strategy),
    ("strategy_rule", StrategyRule),
    ("contract_size", ContractSize),
    ("notification_setting", NotificationSetting),
]
_MODEL_BY_NAME = {name: model for name, model in SEED_TABLES}

# Natural key (columns of the table, after the parents are resolved) and the parents each row
# names, in resolution order. build 11: replaces the id-based upsert (and build 6's NATURAL_KEYS).
NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "user": ("email",),
    "cex_account": ("cex_code", "label"),
    "subaccount": ("cex_account_id", "subacct_name"),
    "strategy": ("subaccount_id", "key"),
    "strategy_rule": ("subaccount_id", "strategy_id", "match_json"),
    "contract_size": ("cex_code", "inst_type", "underlying"),
    "notification_setting": ("asset", "code"),
}
PARENTS: dict[str, tuple[str, ...]] = {
    "cex_account": ("user",),
    "subaccount": ("cex_account",),
    "strategy": ("subaccount",),
    "strategy_rule": ("subaccount", "strategy"),
}
# Columns of the old (id-based) files: `id` is ignored; parent ids are refused (name the parent).
OLD_FK_COLUMNS = ("user_id", "cex_account_id", "subaccount_id", "strategy_id")


class SeedError(RuntimeError):
    """A seed file can't be loaded (bad row, unknown parent, old format) — nothing was written."""


class SeedReplaceWouldWipeLinks(RuntimeError):
    """`--replace` would TRUNCATE ... CASCADE away the Box builder's manual pins."""


class _RowError(Exception):
    pass


def coerce_value(col_type: Any, raw: str) -> Any:
    """Coerce a CSV string to the column's Python type. Empty → NULL for non-text columns."""
    if isinstance(col_type, JSONB):
        return json.loads(raw) if raw != "" else None
    if isinstance(col_type, Boolean):
        return raw.strip().lower() in {"1", "true", "t", "yes", "y"} if raw != "" else None
    if isinstance(col_type, Integer):
        return int(raw) if raw != "" else None
    if isinstance(col_type, Numeric):
        return Decimal(raw) if raw != "" else None
    if isinstance(col_type, DateTime):
        return datetime.fromisoformat(raw) if raw != "" else None
    if isinstance(col_type, Date):
        return date.fromisoformat(raw) if raw != "" else None
    # String / Text: keep as-is (preserves intentional empty strings, e.g. subacct_name="").
    return raw


def _coerce_row(model: type, row: dict[str, str]) -> dict[str, Any]:
    columns = model.__table__.columns
    out: dict[str, Any] = {}
    for key, raw in row.items():
        key = (key or "").strip()
        if key not in columns:
            continue  # ignore unknown CSV columns (parent names, notes)
        out[key] = coerce_value(columns[key].type, raw if raw is not None else "")
    return out


def _reset_sequence(session, model: type, quoted: str) -> None:
    if "id" not in model.__table__.columns:
        return
    session.execute(text(
        f"SELECT setval(pg_get_serial_sequence('{quoted}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {quoted}), 1), "
        f"(SELECT MAX(id) FROM {quoted}) IS NOT NULL)"
    ))


# --------------------------------------------------------------------------- files / layers
@dataclass
class _Row:
    path: Path
    line: int
    raw: dict[str, str] = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


def seed_folders(base: str | Path, env: str) -> list[Path]:
    """The layers loaded for `env`: seed/common (if present), then seed/<env> (must exist)."""
    root = Path(base)
    if not root.is_dir():
        raise FileNotFoundError(f"Seed folder not found: {root.resolve()}")
    env_dir = root / env
    if not env_dir.is_dir():
        have = sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in (COMMON,
                                                                                     "examples"))
        raise FileNotFoundError(f"No seed folder for APP_ENV={env!r}: {env_dir} "
                                f"(available: {', '.join(have) or 'none'})")
    layers = [root / COMMON] if (root / COMMON).is_dir() else []
    return layers + [env_dir]


def _read_rows(path: Path, table: str) -> list[_Row]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = [(h or "").strip() for h in (reader.fieldnames or [])]
        old = [c for c in OLD_FK_COLUMNS if c in header]
        if old:
            raise SeedError(
                f"{path}: old seed format — column(s) {', '.join(old)} hold database ids. Name "
                "the parent by its key instead (user_email; cex_code + account_label + "
                "subacct_name; strategy_key) — see seed/README.md.")
        if "id" in header:
            logger.warning("seed: %s has an `id` column — ignored (the database assigns ids)", path)
        rows = []
        for raw in reader:
            if not any((v or "").strip() for v in raw.values()):
                continue                                  # blank line
            clean = {(k or "").strip(): (v if v is not None else "") for k, v in raw.items()}
            rows.append(_Row(path, reader.line_num, clean))
        return rows


# --------------------------------------------------------------------------- parents by key
def _val(row: _Row, col: str, required: bool = True) -> str:
    v = (row.raw.get(col) or "").strip()
    if required and not v:
        raise _RowError(f"`{col}` is empty")
    return v


def _user_id(s, row: _Row) -> int:
    email = _val(row, "user_email")
    uid = s.execute(select(CoreUser.id).where(CoreUser.email == email)).scalar()
    if uid is None:
        raise _RowError(f"no user {email!r} (user.csv)")
    return uid


def _account_id(s, row: _Row) -> int:
    cex, label = _val(row, "cex_code"), _val(row, "account_label")
    aid = s.execute(select(CexAccount.id).where(CexAccount.cex_code == cex,
                                                CexAccount.label == label)).scalar()
    if aid is None:
        raise _RowError(f"no account {cex} / {label} (cex_account.csv)")
    return aid


def _subaccount_id(s, row: _Row) -> int:
    aid = _account_id(s, row)
    name = (row.raw.get("subacct_name") or "").strip()
    sid = s.execute(select(Subaccount.id).where(Subaccount.cex_account_id == aid,
                                                Subaccount.subacct_name == name)).scalar()
    if sid is None:
        raise _RowError(f"no sub-account {row.raw.get('account_label')!r} / {name!r} "
                        "(subaccount.csv)")
    return sid


def _strategy_id(s, row: _Row, sub_id: int) -> int:
    key = _val(row, "strategy_key")
    st = s.execute(select(Strategy.id).where(Strategy.subaccount_id == sub_id, Strategy.key == key)
                   .order_by(Strategy.deleted_at.is_not(None), Strategy.id)).scalars().first()
    if st is None:
        raise _RowError(f"no box with key {key!r} in that sub-account (strategy.csv)")
    return st


def _values(s, table: str, model: type, row: _Row) -> dict[str, Any]:
    """The row's column values, parents resolved to ids, key columns filled."""
    try:
        values = _coerce_row(model, row.raw)
    except (ValueError, json.JSONDecodeError) as e:
        raise _RowError(f"bad value: {e}") from e
    values.pop("id", None)
    parents = PARENTS.get(table, ())
    if "user" in parents:
        values["user_id"] = _user_id(s, row)
    if "cex_account" in parents:
        values["cex_account_id"] = _account_id(s, row)
    if "subaccount" in parents:
        values["subaccount_id"] = _subaccount_id(s, row)
    if "strategy" in parents:
        values["strategy_id"] = _strategy_id(s, row, values["subaccount_id"])
    if table == "subaccount":
        values.setdefault("subacct_name", "")
    if table == "strategy":
        if not (values.get("name") or "").strip():
            raise _RowError("`name` is empty")
        if not (values.get("key") or "").strip():
            values["key"] = values["name"]              # blank key = the name
    for k in NATURAL_KEYS[table]:
        if values.get(k) in (None, "") and not (table == "subaccount" and k == "subacct_name"):
            raise _RowError(f"key column `{k}` is empty")
    return values


def _find(s, table: str, model: type, values: dict[str, Any]):
    q = select(model).where(*[getattr(model, k) == values[k] for k in NATURAL_KEYS[table]])
    if model is Strategy:                               # an active box before a deleted one
        q = q.order_by(Strategy.deleted_at.is_not(None), Strategy.id)
    return s.execute(q.limit(1)).scalars().first()


# --------------------------------------------------------------------------- load
def load_seed(folder: str = "seed", only: str | None = None, replace: bool = False,
              wipe_links: bool = False, env: str | None = None) -> dict[str, dict[str, Any]]:
    """Load seed/common + seed/<env> into the core tables (env: APP_ENV, default dev).

    Returns {table: {"rows", "new", "updated", "files"}}. `only` restricts to one table;
    `replace` truncates the target tables first (CASCADE) — refused while manual pins exist unless
    `wipe_links`. Raises SeedError (nothing written) when a row can't be loaded.
    """
    if env is None:
        from config.settings import get_settings
        env = get_settings().app_env or "dev"
    layers = seed_folders(folder, env)
    stray = sorted(p.name for p in Path(folder).glob("*.csv"))
    if stray:
        logger.warning("seed: %s in %s/ are not loaded any more — rows now live in %s/%s/ or "
                       "%s/<env>/ (build 11)", ", ".join(stray), folder, folder, COMMON, folder)

    targets = [(n, m) for n, m in SEED_TABLES if only is None or n == only]
    if only and not targets:
        raise ValueError(f"Unknown seed table '{only}'. Known: {list(_MODEL_BY_NAME)}")
    files = {n: [d / f"{n}.csv" for d in layers if (d / f"{n}.csv").exists()] for n, _ in targets}

    counts: dict[str, dict[str, Any]] = {}
    with session_scope() as s:
        prep = s.bind.dialect.identifier_preparer

        if replace and not wipe_links:
            n_links = s.execute(text("SELECT count(*) FROM core.strategy_link")).scalar_one()
            if n_links:
                raise SeedReplaceWouldWipeLinks(
                    f"seed --replace would delete {n_links} manual strategy link(s) "
                    "(core.strategy_link, Box builder pins) via TRUNCATE ... CASCADE. "
                    "Seed without --replace (match on keys), or pass --wipe-links to drop them.")

        if replace:
            # Truncate in reverse dependency order; CASCADE handles the rest.
            for name, model in reversed(targets):
                if files[name]:
                    s.execute(text(
                        f"TRUNCATE {prep.format_table(model.__table__)} RESTART IDENTITY CASCADE"
                    ))

        for name, model in targets:
            if not files[name]:
                logger.info("seed: no %s.csv in %s, skipping", name,
                            " or ".join(str(d) for d in layers))
                continue
            quoted = prep.format_table(model.__table__)
            _reset_sequence(s, model, quoted)           # new ids never collide with old ones
            new = updated = 0
            errors: list[str] = []
            seen: dict[tuple, str] = {}
            for path in files[name]:
                for row in _read_rows(path, name):
                    try:
                        values = _values(s, name, model, row)
                    except _RowError as e:
                        errors.append(f"{row.where}: {e}")
                        continue
                    key = tuple(json.dumps(values[k], sort_keys=True, default=str)
                                for k in NATURAL_KEYS[name])
                    if key in seen and seen[key].split(":")[0] == str(path):
                        logger.warning("seed: %s repeats the row of %s — the later one wins",
                                       row.where, seen[key])
                    seen[key] = row.where
                    obj = _find(s, name, model, values)
                    if model is Strategy:
                        _warn_same_name(s, values, row, obj.id if obj is not None else None)
                    if obj is None:
                        s.add(model(**values))
                        new += 1
                    else:
                        for col, v in values.items():
                            setattr(obj, col, v)
                        if model is Strategy and obj.deleted_at is not None:
                            logger.info("seed: %s — box %r is deleted in the Box builder; "
                                        "updated, kept deleted", row.where, obj.key)
                        updated += 1
                    s.flush()                            # later rows can name this one as parent
            if errors:
                raise SeedError(f"seed: core.{name} not loaded — nothing was written:\n  "
                                + "\n  ".join(errors))
            _reset_sequence(s, model, quoted)
            counts[name] = {"rows": new + updated, "new": new, "updated": updated,
                            "files": [str(p) for p in files[name]]}
            logger.info("seed: core.%s — %d new, %d updated (%s)", name, new, updated,
                        ", ".join(str(p) for p in files[name]))
    return counts


def _warn_same_name(s, values: dict[str, Any], row: _Row, self_id: int | None) -> None:
    """A seeded box whose name another active box of the sub-account already has (e.g. a box
    made in the Box builder under that name after the seeded one was renamed) — allowed, but
    confusing."""
    q = select(Strategy.key).where(
        Strategy.subaccount_id == values["subaccount_id"], Strategy.deleted_at.is_(None),
        Strategy.name == values.get("name"), Strategy.key != values["key"])
    if self_id is not None:
        q = q.where(Strategy.id != self_id)
    clash = s.execute(q).scalars().first()
    if clash is not None:
        logger.warning("seed: %s — box %r gets the name %r, which box %r of that sub-account "
                       "already has (renamed / created in the Box builder?) — two boxes show the "
                       "same name", row.where, values["key"], values.get("name"), clash)
