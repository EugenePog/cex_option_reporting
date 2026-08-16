"""Load core/settings data from CSV files into the DB (filename = table name).

Each CSV under the seed folder is upserted by primary key `id` into its `core` table, in
dependency order, then the id sequence is reset. Re-running updates rather than duplicating.

Only the manually-managed settings tables are handled here:
    user -> cex_account -> subaccount -> strategy -> strategy_rule
(`instrument` is derived by the silver pipeline; `audit_log` / `pipeline_watermark` are app-written.)
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Integer, Numeric, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import session_scope
from app.db.models_core import (
    CexAccount,
    CoreUser,
    Strategy,
    StrategyRule,
    Subaccount,
)

logger = logging.getLogger(__name__)

# (table name / csv filename stem, model) in FK dependency order.
SEED_TABLES: list[tuple[str, type]] = [
    ("user", CoreUser),
    ("cex_account", CexAccount),
    ("subaccount", Subaccount),
    ("strategy", Strategy),
    ("strategy_rule", StrategyRule),
]
_MODEL_BY_NAME = {name: model for name, model in SEED_TABLES}


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
        key = key.strip()
        if key not in columns:
            continue  # ignore unknown CSV columns
        out[key] = coerce_value(columns[key].type, raw)
    return out


def _reset_sequence(session, model: type, quoted: str) -> None:
    if "id" not in model.__table__.columns:
        return
    session.execute(text(
        f"SELECT setval(pg_get_serial_sequence('{quoted}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {quoted}), 1), "
        f"(SELECT MAX(id) FROM {quoted}) IS NOT NULL)"
    ))


def load_seed(folder: str = "seed", only: str | None = None, replace: bool = False) -> dict[str, int]:
    """Load CSVs from `folder` into core tables. Returns {table: rows_loaded}.

    `only` restricts to one table; `replace` truncates the target tables first (CASCADE).
    """
    base = Path(folder)
    if not base.is_dir():
        raise FileNotFoundError(f"Seed folder not found: {base.resolve()}")

    targets = [(n, m) for n, m in SEED_TABLES if only is None or n == only]
    if only and not targets:
        raise ValueError(f"Unknown seed table '{only}'. Known: {list(_MODEL_BY_NAME)}")

    counts: dict[str, int] = {}
    with session_scope() as s:
        prep = s.bind.dialect.identifier_preparer

        if replace:
            # Truncate in reverse dependency order; CASCADE handles the rest.
            for name, model in reversed(targets):
                if (base / f"{name}.csv").exists():
                    s.execute(text(
                        f"TRUNCATE {prep.format_table(model.__table__)} RESTART IDENTITY CASCADE"
                    ))

        for name, model in targets:
            path = base / f"{name}.csv"
            if not path.exists():
                logger.info("seed: %s.csv not found, skipping", name)
                continue

            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))

            loaded = 0
            for row in rows:
                values = _coerce_row(model, row)
                if not values:
                    continue
                stmt = pg_insert(model).values(**values)
                update_cols = {c: stmt.excluded[c] for c in values if c != "id"}
                if "id" in values and update_cols:
                    stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
                elif "id" in values:
                    stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
                s.execute(stmt)
                loaded += 1

            _reset_sequence(s, model, prep.format_table(model.__table__))
            counts[name] = loaded
            logger.info("seed: loaded %d rows into core.%s", loaded, name)

    return counts
