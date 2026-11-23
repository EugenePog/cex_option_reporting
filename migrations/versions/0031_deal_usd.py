"""Graph ② / ②b / Analyze in USD at operation-time prices, as graph ① (build 25)

  gold (rebuilt every pipeline run — the next run recomputes all history)
    * deal_ledger: realized_pnl_usd now = premium + opening fees at the opening prices + closing
                   trades / settlement and their fees at the closing prices (graph ①'s hover);
                   coin × the close day's rate only without cash data
                   + fees_usd   all fees of the position, USD at their operations' prices
                   + usd_basis  'trades' | 'day rate' — which rule gave the USD
    * pnl_daily, asset_pnl_daily: *_usd columns follow the same rule (no schema change);
      unrealized at the index price of the snapshot

Revision ID: 0031_deal_usd
Revises: 0030_leg_close_cash
Create Date: 2026-11-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_deal_usd"
down_revision: str | None = "0030_leg_close_cash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("deal_ledger", sa.Column("fees_usd", sa.Numeric(), nullable=True), schema="gold")
    op.add_column("deal_ledger", sa.Column("usd_basis", sa.String(10), nullable=True), schema="gold")


def downgrade() -> None:
    op.drop_column("deal_ledger", "usd_basis", schema="gold")
    op.drop_column("deal_ledger", "fees_usd", schema="gold")
