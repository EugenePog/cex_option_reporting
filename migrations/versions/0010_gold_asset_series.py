"""gold: per-asset (in-kind) balance timeseries + daily P&L (coin-denominated)

Revision ID: 0010_gold_asset_series
Revises: 0009_gold_client_perf_sharpe
Create Date: 2026-08-22

Adds two gold tables for the in-kind equity report. Gold-only; does not touch bronze/silver.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_gold_asset_series"
down_revision: str | None = "0009_gold_client_perf_sharpe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"


def upgrade() -> None:
    op.create_table(
        "asset_balance_timeseries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("ccy", sa.String(16)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("amount", sa.Numeric(), nullable=True),
        sa.Column("usd_value", sa.Numeric(), nullable=True),
        sa.UniqueConstraint("subaccount_id", "ccy", "captured_at", name="uq_gold_asset_balance_ts"),
        schema=G,
    )
    op.create_index("ix_gold_asset_bal_sub_ccy", "asset_balance_timeseries",
                    ["subaccount_id", "ccy"], schema=G)

    op.create_table(
        "asset_pnl_daily",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("ccy", sa.String(16)),
        sa.Column("date", sa.Date()),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("unrealized_pnl", sa.Numeric(), nullable=True),
        sa.Column("fees", sa.Numeric(), nullable=True),
        sa.Column("net_pnl", sa.Numeric(), nullable=True),
        schema=G,
    )
    op.create_index("ix_gold_asset_pnl_sub_ccy_date", "asset_pnl_daily",
                    ["subaccount_id", "ccy", "date"], schema=G)


def downgrade() -> None:
    op.drop_table("asset_pnl_daily", schema=G)
    op.drop_table("asset_balance_timeseries", schema=G)
