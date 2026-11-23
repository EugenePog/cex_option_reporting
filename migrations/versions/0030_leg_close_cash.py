"""P&L split per position leg — premium − fees (opening) + closing, coin and USD (build 24)

  gold (rebuilt every pipeline run — the next run fills / recomputes them for all history)
    * position_leg: premium_net_coin / premium_net_usd (0029) now hold the OPENING side only: the
                    premium of the trades that opened or added to the position with their fees,
                    USD at the opening prices
                    + close_net_coin  the rest of the leg's cash: trades that reduced / closed it,
                                      the expiry settlement (payout), their fees
                    + close_net_usd   the same in USD at the closing prices (expiry: settlement)
                    premium_net + close_net = the leg's P&L (closed: OKX realizedPnl, in coin)
    * position_leg.close_type / deal_ledger.close_type: 'expiry' now = closed within ±5 min of the
      expiry (08:00 UTC); OKX `type` 3 is liquidation, not expiry (no schema change)

Revision ID: 0030_leg_close_cash
Revises: 0029_leg_premium
Create Date: 2026-11-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_leg_close_cash"
down_revision: str | None = "0029_leg_premium"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("close_net_coin", "close_net_usd"):
        op.add_column("position_leg", sa.Column(name, sa.Numeric(), nullable=True), schema="gold")


def downgrade() -> None:
    for name in ("close_net_usd", "close_net_coin"):
        op.drop_column("position_leg", name, schema="gold")
