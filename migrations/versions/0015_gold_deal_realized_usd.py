"""gold.deal_ledger.realized_pnl_usd

Stores each deal's realized P&L in USD (coin × coin→USD rate at close — same basis as
gold.pnl_daily.net_pnl_usd), so report ② can show the real OKX realized figure at the
settlement marker for expired options instead of the reconstructed payoff value.

Revision ID: 0015_gold_deal_realized_usd
Revises: 0014_gold_pnl_usd
Create Date: 2026-09-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_gold_deal_realized_usd"
down_revision: str | None = "0014_gold_pnl_usd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"


def upgrade() -> None:
    op.add_column("deal_ledger", sa.Column("realized_pnl_usd", sa.Numeric(), nullable=True), schema=G)


def downgrade() -> None:
    op.drop_column("deal_ledger", "realized_pnl_usd", schema=G)
