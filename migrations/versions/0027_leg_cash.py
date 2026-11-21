"""Option cash in USD at operation-time prices — graph ① future-PnL switches (build 21)

The PnL panel next to graph ① can add (switch "Premiums & fees") the premium received / paid and
the fees of the still-open options, and (switch "Realized") the realized PnL of closed options and
of partial closes, each converted to USD at the index price of the moment it happened.

  silver (filled from what bronze already holds — the collectors store OKX's whole message; no
  re-collection, nothing in bronze is rewritten)
    * position_snapshot: + pnl          OKX positions `pnl` (coin): accumulated PnL of the
                                        position's closing trades, without fees
                         + realized_pnl OKX positions `realizedPnl` (coin) = pnl + fee (+ funding)
    * trade_fill:        + idx_px       OKX fills `fillIdxPx`: the index price at the trade (USD)

  gold (rebuilt every pipeline run)
    * position_leg: + open_cash_usd     premium + fees of the still-open quantity (average cost),
                                        USD at each trade's index price; + received / − paid
                    + realized_cash_usd realized PnL of the closed quantity, USD: opening cash at
                                        the opening prices + closing cash and fees at the closing
                                        prices (expiry: settlement)
                    + cash_source       fills | snapshots | summary — where it was computed from
                    + cash_missing_px   events priced with no index price found (0 = all priced)

Revision ID: 0027_leg_cash
Revises: 0026_no_data_alerts
Create Date: 2026-11-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_leg_cash"
down_revision: str | None = "0026_no_data_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("position_snapshot", sa.Column("pnl", sa.Numeric(), nullable=True),
                  schema="silver")
    op.add_column("position_snapshot", sa.Column("realized_pnl", sa.Numeric(), nullable=True),
                  schema="silver")
    op.add_column("trade_fill", sa.Column("idx_px", sa.Numeric(), nullable=True), schema="silver")
    for name, typ in (("open_cash_usd", sa.Numeric()), ("realized_cash_usd", sa.Numeric()),
                      ("cash_source", sa.String(10)), ("cash_missing_px", sa.Integer())):
        op.add_column("position_leg", sa.Column(name, typ, nullable=True), schema="gold")


def downgrade() -> None:
    for name in ("cash_missing_px", "cash_source", "realized_cash_usd", "open_cash_usd"):
        op.drop_column("position_leg", name, schema="gold")
    op.drop_column("trade_fill", "idx_px", schema="silver")
    op.drop_column("position_snapshot", "realized_pnl", schema="silver")
    op.drop_column("position_snapshot", "pnl", schema="silver")
