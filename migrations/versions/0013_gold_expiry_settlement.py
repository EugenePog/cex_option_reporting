"""gold.expiry_settlement — official settlement price per (underlying, expiry)

Revision ID: 0013_gold_expiry_settlement
Revises: 0012_silver_bill
Create Date: 2026-09-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_gold_expiry_settlement"
down_revision: str | None = "0012_silver_bill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"


def upgrade() -> None:
    op.create_table(
        "expiry_settlement",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("underlying", sa.String(32)),
        sa.Column("expiry", sa.Date()),
        sa.Column("settle_price", sa.Numeric(), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("subaccount_id", "underlying", "expiry",
                            name="uq_gold_expiry_settlement"),
        schema=G,
    )
    op.create_index("ix_gold_expiry_settlement_uly", "expiry_settlement",
                    ["subaccount_id", "underlying", "expiry"], schema=G)


def downgrade() -> None:
    op.drop_table("expiry_settlement", schema=G)
