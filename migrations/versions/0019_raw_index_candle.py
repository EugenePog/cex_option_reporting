"""bronze.raw_index_candle — OKX index candles (BTC-USD, 1 minute)

Source: OKX public market data, GET /api/v5/market/history-index-candles (no API key needed;
max 100 candles per request, paged backwards with `after`). Filled by:
  * `backfill` — after every account's positions are loaded, from the day of the earliest open or
    closed position in bronze up to now (missing minutes only);
  * the snapshot and history collector loops (and their one-shot commands) — from the latest
    stored candle up to now, re-checking the last INGEST_DAILY_LOOKBACK_DAYS + 1 days for gaps.

  bronze
    * raw_index_candle (NEW): one row per (cex_code, inst_id, bar, ts = candle open time);
      payload = the raw OKX array ["ts","o","h","l","c","confirm"]. Only completed candles.

Revision ID: 0019_raw_index_candle
Revises: 0018_strategy_soft_delete
Create Date: 2026-10-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_raw_index_candle"
down_revision: str | None = "0018_strategy_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

B = "bronze"


def upgrade() -> None:
    op.create_table(
        "raw_index_candle",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ingest_id", sa.String(36), sa.ForeignKey(f"{B}.ingest_run.ingest_id"),
                  nullable=False),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("inst_id", sa.String(32), nullable=False),
        sa.Column("bar", sa.String(8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("cex_code", "inst_id", "bar", "ts", name="uq_raw_index_candle"),
        schema=B,
    )


def downgrade() -> None:
    op.drop_table("raw_index_candle", schema=B)
