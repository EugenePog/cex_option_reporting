"""gold: underlying_price (spot/forward per underlying over time) for the payoff report

Revision ID: 0011_gold_underlying_price
Revises: 0010_gold_asset_series
Create Date: 2026-08-22

Gold-only; lets report ② read spot + expiration price from gold instead of silver.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_gold_underlying_price"
down_revision: str | None = "0010_gold_asset_series"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"


def upgrade() -> None:
    op.create_table(
        "underlying_price",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("underlying", sa.String(32)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("idx_px", sa.Numeric(), nullable=True),
        sa.Column("fwd_px", sa.Numeric(), nullable=True),
        sa.UniqueConstraint("subaccount_id", "underlying", "captured_at",
                            name="uq_gold_underlying_price"),
        schema=G,
    )
    op.create_index("ix_gold_underlying_price_sub_uly", "underlying_price",
                    ["subaccount_id", "underlying", "captured_at"], schema=G)


def downgrade() -> None:
    op.drop_table("underlying_price", schema=G)
