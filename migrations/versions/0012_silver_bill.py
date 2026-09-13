"""silver.bill — parsed account ledger (delivery rows carry the settlement price)

Revision ID: 0012_silver_bill
Revises: 0011_gold_underlying_price
Create Date: 2026-09-13
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_silver_bill"
down_revision: str | None = "0011_gold_underlying_price"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "silver"


def upgrade() -> None:
    op.create_table(
        "bill",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("bill_id", sa.String(64)),
        sa.Column("inst_id", sa.String(64), nullable=True),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("bill_type", sa.String(8), nullable=True),
        sa.Column("sub_type", sa.String(8), nullable=True),
        sa.Column("px", sa.Numeric(), nullable=True),
        sa.Column("pnl", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("ccy", sa.String(16), nullable=True),
        sa.Column("billed_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("cex_code", "bill_id", name="uq_silver_bill"),
        schema=S,
    )
    for col in ("subaccount_id", "inst_id", "bill_id", "expiry", "bill_type", "billed_at"):
        op.create_index(f"ix_silver_bill_{col}", "bill", [col], schema=S)


def downgrade() -> None:
    op.drop_table("bill", schema=S)
