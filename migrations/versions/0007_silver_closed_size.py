"""add size to silver.closed_position (needed by gold.deal_ledger)

Revision ID: 0007_silver_closed_size
Revises: 0006_silver_position_enrich
Create Date: 2026-08-18

ADDITIVE ONLY — one nullable column on silver.closed_position. Does not touch bronze.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_silver_closed_size"
down_revision: str | None = "0006_silver_position_enrich"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("closed_position", sa.Column("size", sa.Numeric(), nullable=True), schema="silver")


def downgrade() -> None:
    op.drop_column("closed_position", "size", schema="silver")
