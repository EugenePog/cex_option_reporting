"""core.hedged_position — the hedged amount (Q) per account and asset (Settings → Position hedged)

The future-PnL profile next to graph ① ("Price with strategy boxes") shows the PnL at expiry of
Q units of the asset hedged by the account's options, against the same Q without options. Q is not
read from the exchange: the asset balance OKX reports (`eq`, gold.asset_balance_timeseries) is the
account's equity and includes the options' own PnL, so it is not "the amount held". An admin sets
Q by hand, per account and asset, in Settings → Position hedged.

  core
    * hedged_position (NEW, edited in Settings, seedable): one row per sub-account × asset.
      Columns: subaccount_id → core.subaccount, ccy (the asset as in
      gold.asset_balance_timeseries.ccy, e.g. 'BTC'), qty (hedged amount in that asset, ≥ 0),
      note, updated_at. No row = not set. Unique (subaccount_id, ccy).

Revision ID: 0025_hedged_position
Revises: 0024_seed_natural_keys
Create Date: 2026-11-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_hedged_position"
down_revision: str | None = "0024_seed_natural_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"


def upgrade() -> None:
    op.create_table(
        "hedged_position",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  nullable=False),
        sa.Column("ccy", sa.String(16), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("subaccount_id", "ccy", name="uq_hedged_position"),
        sa.CheckConstraint("qty >= 0", name="ck_hedged_position_qty"),
        schema=C,
    )


def downgrade() -> None:
    op.drop_table("hedged_position", schema=C)
