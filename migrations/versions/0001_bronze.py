"""bronze layer: ingest_run + raw_* snapshot tables

Revision ID: 0001_bronze
Revises:
Create Date: 2026-08-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_bronze"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BRONZE = "bronze"


def _create_raw_table(name: str, extra_cols: list[sa.Column] | None = None) -> None:
    cols = [
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ingest_id", sa.String(36),
                  sa.ForeignKey(f"{BRONZE}.ingest_run.ingest_id")),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("account_label", sa.String(64)),
        sa.Column("subacct_name", sa.String(64), server_default=""),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB()),
    ]
    if extra_cols:
        cols.extend(extra_cols)
    op.create_table(name, *cols, schema=BRONZE)
    # op.create_table ignores Column(index=True), so create indexes explicitly.
    for col in ("ingest_id", "cex_code", "account_label", "captured_at"):
        op.create_index(f"ix_{name}_{col}", name, [col], schema=BRONZE)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {BRONZE}")

    op.create_table(
        "ingest_run",
        sa.Column("ingest_id", sa.String(36), primary_key=True),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("account_label", sa.String(64)),
        sa.Column("mode", sa.String(16)),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="RUNNING"),
        sa.Column("row_count", sa.Integer(), server_default="0"),
        sa.Column("error_text", sa.Text(), nullable=True),
        schema=BRONZE,
    )

    _create_raw_table("raw_balance")
    _create_raw_table("raw_position")
    _create_raw_table("raw_margin")
    _create_raw_table("raw_opt_summary")
    _create_raw_table(
        "raw_trade_fill",
        extra_cols=[sa.Column("trade_id", sa.String(64))],
    )
    op.create_index("ix_raw_trade_fill_trade_id", "raw_trade_fill", ["trade_id"], schema=BRONZE)
    op.create_unique_constraint(
        "uq_raw_trade_fill_cex_trade", "raw_trade_fill", ["cex_code", "trade_id"], schema=BRONZE
    )


def downgrade() -> None:
    for t in ("raw_trade_fill", "raw_opt_summary", "raw_margin", "raw_position", "raw_balance",
              "ingest_run"):
        op.drop_table(t, schema=BRONZE)
    op.execute(f"DROP SCHEMA IF EXISTS {BRONZE}")
