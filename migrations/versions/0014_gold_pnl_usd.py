"""gold pnl USD columns + position_current.fee

Adds USD-equivalent P&L columns to gold.pnl_daily and gold.asset_pnl_daily
(so report ①a can show USD, fee-inclusive) and a coin `fee` column to
gold.position_current (so report ② payoff can subtract fees).

Revision ID: 0014_gold_pnl_usd
Revises: 0013_gold_expiry_settlement
Create Date: 2026-09-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_gold_pnl_usd"
down_revision: str | None = "0013_gold_expiry_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"
_USD_COLS = ("realized_pnl_usd", "unrealized_pnl_usd", "fees_usd", "net_pnl_usd")


def upgrade() -> None:
    for tbl in ("pnl_daily", "asset_pnl_daily"):
        for col in _USD_COLS:
            op.add_column(tbl, sa.Column(col, sa.Numeric(), nullable=True), schema=G)
    op.add_column("position_current", sa.Column("fee", sa.Numeric(), nullable=True), schema=G)


def downgrade() -> None:
    op.drop_column("position_current", "fee", schema=G)
    for tbl in ("pnl_daily", "asset_pnl_daily"):
        for col in _USD_COLS:
            op.drop_column(tbl, col, schema=G)
