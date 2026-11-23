"""Premium − fees per position leg — graph ① hover: premium per day (build 23)

  gold (rebuilt every pipeline run — the next run fills the columns for all history; nothing to
  re-collect, bronze is not touched)
    * position_leg: + premium_net_coin  premium − fees of the leg in its coin: the premium of every
                                        trade (sold +, bought −; opening and closing trades) plus
                                        every fee; the expiry settlement is NOT included
                    + premium_net_usd   the same in USD at the index price of each operation (the
                                        prices of graph ①'s PnL switches, app.domain.leg_cash)

Revision ID: 0029_leg_premium
Revises: 0028_box_hedged
Create Date: 2026-11-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_leg_premium"
down_revision: str | None = "0028_box_hedged"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name in ("premium_net_coin", "premium_net_usd"):
        op.add_column("position_leg", sa.Column(name, sa.Numeric(), nullable=True), schema="gold")


def downgrade() -> None:
    for name in ("premium_net_usd", "premium_net_coin"):
        op.drop_column("position_leg", name, schema="gold")
