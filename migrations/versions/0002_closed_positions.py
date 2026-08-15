"""bronze.raw_closed_position — closed positions incl. expiry/delivery (realized PnL)

Revision ID: 0002_closed_positions
Revises: 0001_bronze
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_closed_positions"
down_revision: str | None = "0001_bronze"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BRONZE = "bronze"
TABLE = "raw_closed_position"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ingest_id", sa.String(36), sa.ForeignKey(f"{BRONZE}.ingest_run.ingest_id")),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("account_label", sa.String(64)),
        sa.Column("subacct_name", sa.String(64), server_default=""),
        sa.Column("ext_id", sa.String(64)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB()),
        schema=BRONZE,
    )
    for col in ("ingest_id", "cex_code", "account_label", "captured_at", "ext_id"):
        op.create_index(f"ix_{TABLE}_{col}", TABLE, [col], schema=BRONZE)
    op.create_unique_constraint(
        "uq_raw_closed_position_cex_ext", TABLE, ["cex_code", "ext_id"], schema=BRONZE
    )


def downgrade() -> None:
    op.drop_table(TABLE, schema=BRONZE)
