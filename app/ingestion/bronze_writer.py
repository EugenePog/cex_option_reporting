"""BronzeWriter — persists raw connector rows to the bronze layer, tracked by an ingest_run.

Every write is tied to a run so bronze is auditable and replayable. Fills are upserted on
(cex_code, trade_id) so daily overlaps and full backfills never create duplicates.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import session_scope
from app.db.models import (
    IngestRun,
    RawBalance,
    RawMargin,
    RawOptSummary,
    RawPosition,
    RawTradeFill,
)


class BronzeWriter:
    def __init__(self, cex_code: str, account_label: str) -> None:
        self.cex_code = cex_code
        self.account_label = account_label

    # -- run lifecycle ------------------------------------------------------ #
    def start_run(self, mode: str) -> str:
        """Create an ingest_run row (mode = 'daily' | 'backfill'); returns its ingest_id."""
        ingest_id = str(uuid.uuid4())
        with session_scope() as s:
            s.add(IngestRun(
                ingest_id=ingest_id,
                cex_code=self.cex_code,
                account_label=self.account_label,
                mode=mode,
                status="RUNNING",
            ))
        return ingest_id

    def finish_run(self, ingest_id: str, status: str, row_count: int,
                   error_text: str | None = None) -> None:
        with session_scope() as s:
            run = s.get(IngestRun, ingest_id)
            if run is not None:
                run.status = status
                run.row_count = row_count
                run.error_text = error_text
                run.finished_at = datetime.now(timezone.utc)

    # -- snapshot writes (append-only) -------------------------------------- #
    def write_snapshot(self, ingest_id: str, model: type, rows: list[Any],
                       subacct: str = "") -> int:
        """Write normalized rows (each having `.raw` + `.captured_at`) to a raw_* table."""
        if not rows:
            return 0
        with session_scope() as s:
            for r in rows:
                s.add(model(
                    ingest_id=ingest_id,
                    cex_code=self.cex_code,
                    account_label=self.account_label,
                    subacct_name=subacct,
                    captured_at=r.captured_at,
                    payload=r.raw,
                ))
        return len(rows)

    def write_positions(self, ingest_id, rows, subacct="") -> int:
        return self.write_snapshot(ingest_id, RawPosition, rows, subacct)

    def write_balances(self, ingest_id, rows, subacct="") -> int:
        return self.write_snapshot(ingest_id, RawBalance, rows, subacct)

    def write_margin(self, ingest_id, rows, subacct="") -> int:
        return self.write_snapshot(ingest_id, RawMargin, rows, subacct)

    def write_opt_summary(self, ingest_id, rows, subacct="") -> int:
        return self.write_snapshot(ingest_id, RawOptSummary, rows, subacct)

    # -- fills (idempotent upsert) ------------------------------------------ #
    def write_fills(self, ingest_id: str, rows: list[Any], subacct: str = "") -> int:
        """Upsert fills; existing (cex_code, trade_id) rows are left as-is (do-nothing)."""
        if not rows:
            return 0
        written = 0
        with session_scope() as s:
            for r in rows:
                stmt = pg_insert(RawTradeFill).values(
                    ingest_id=ingest_id,
                    cex_code=self.cex_code,
                    account_label=self.account_label,
                    subacct_name=subacct,
                    trade_id=r.trade_id,
                    captured_at=r.filled_at,
                    payload=r.raw,
                ).on_conflict_do_nothing(constraint="uq_raw_trade_fill_cex_trade")
                result = s.execute(stmt)
                written += result.rowcount or 0
        return written
