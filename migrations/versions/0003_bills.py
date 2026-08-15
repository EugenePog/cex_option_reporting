"""bronze.raw_bill — account ledger (bills-archive, ~1yr): fees, settlements, deliveries

Revision ID: 0003_bills
Revises: 0002_closed_positions
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_bills"
down_revision: str | None = "0002_closed_positions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BRONZE = "bronze"
TABLE = "raw_bill"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ingest_id", sa.String(36), sa.ForeignKey(f"{BRONZE}.ingest_run.ingest_id")),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("account_label", sa.String(64)),
        sa.Column("subacct_name", sa.String(64), server_default=""),
        sa.Column("bill_id", sa.String(64)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB()),
        schema=BRONZE,
    )
    for col in ("ingest_id", "cex_code", "account_label", "captured_at", "bill_id"):
        op.create_index(f"ix_{TABLE}_{col}", TABLE, [col], schema=BRONZE)
    op.create_unique_constraint("uq_raw_bill_cex_bill", TABLE, ["cex_code", "bill_id"], schema=BRONZE)


def downgrade() -> None:
    op.drop_table(TABLE, schema=BRONZE)
