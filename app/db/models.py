"""Bronze-layer ORM models.

Bronze is append-only and stores the *raw* exchange payload verbatim (JSONB) plus ingestion
metadata. Rows are self-contained (they carry `cex_code` / `account_label` / `subacct_name` as
text) so bronze does not depend on the core dimension tables being populated yet — the collector
can write here before the rest of the schema exists.

`captured_at` = the moment the snapshot represents; `ingest_ts` = the moment we wrote the row.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Registering core + silver + gold models here means a single `import app.db.models` pulls in
# every table onto Base.metadata — used by create_all and Alembic autogenerate.
from app.db import models_core  # noqa: E402,F401
from app.db import models_silver  # noqa: E402,F401
from app.db import models_gold  # noqa: E402,F401

BRONZE = "bronze"


class IngestRun(Base):
    """One collection attempt (a batch). Every raw row points back to its run for audit/replay."""

    __tablename__ = "ingest_run"
    __table_args__ = {"schema": BRONZE}

    ingest_id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid4
    cex_code: Mapped[str] = mapped_column(String(16))
    account_label: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))  # 'daily' | 'backfill'
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")  # RUNNING|OK|ERROR
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class _RawBase(Base):
    """Shared columns for every raw snapshot table."""

    __abstract__ = True

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ingest_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(f"{BRONZE}.ingest_run.ingest_id"), index=True
    )
    cex_code: Mapped[str] = mapped_column(String(16), index=True)
    account_label: Mapped[str] = mapped_column(String(64), index=True)
    subacct_name: Mapped[str] = mapped_column(String(64), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB)


class RawBalance(_RawBase):
    __tablename__ = "raw_balance"
    __table_args__ = {"schema": BRONZE}


class RawPosition(_RawBase):
    __tablename__ = "raw_position"
    __table_args__ = {"schema": BRONZE}


class RawMargin(_RawBase):
    __tablename__ = "raw_margin"
    __table_args__ = {"schema": BRONZE}


class RawOptSummary(_RawBase):
    __tablename__ = "raw_opt_summary"
    __table_args__ = {"schema": BRONZE}


class RawTradeFill(_RawBase):
    """Fills are deduplicated on (cex_code, inst_id, trade_id) so re-runs/backfills are idempotent.

    OKX `tradeId` is a counter PER INSTRUMENT (not unique across an account), so `inst_id` is part
    of the key — deduping on (cex_code, trade_id) alone silently dropped fills (fixed in 0016).
    """

    __tablename__ = "raw_trade_fill"
    __table_args__ = (
        UniqueConstraint("cex_code", "inst_id", "trade_id",
                         name="uq_raw_trade_fill_cex_inst_trade"),
        {"schema": BRONZE},
    )

    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    trade_id: Mapped[str] = mapped_column(String(64), index=True)


class RawClosedPosition(_RawBase):
    """Closed positions incl. expiry/delivery — the source of realized PnL on expired options.

    Deduplicated on the position-leg key (cex_code, ext_id = OKX posId, pos_opened_at = OKX cTime):
    OKX re-uses a posId when the same instrument is reopened within 30 days of a full close, so the
    open time is part of the key (one row per position lifecycle). Added in 0017.
    """

    __tablename__ = "raw_closed_position"
    __table_args__ = (
        UniqueConstraint("cex_code", "ext_id", "pos_opened_at", name="uq_raw_closed_position_leg",
                         postgresql_nulls_not_distinct=True),
        {"schema": BRONZE},
    )

    ext_id: Mapped[str] = mapped_column(String(64), index=True)
    pos_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RawBill(_RawBase):
    """Account ledger (bills-archive, ~1yr): fees, settlements, deliveries, transfers.

    Supplementary money-movement detail. Deduplicated on (cex_code, bill_id).
    """

    __tablename__ = "raw_bill"
    __table_args__ = (
        UniqueConstraint("cex_code", "bill_id", name="uq_raw_bill_cex_bill"),
        {"schema": BRONZE},
    )

    bill_id: Mapped[str] = mapped_column(String(64), index=True)
