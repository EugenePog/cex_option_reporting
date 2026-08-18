"""fix: add missing sharpe column to gold.client_performance

Revision ID: 0009_gold_client_perf_sharpe
Revises: 0008_gold
Create Date: 2026-08-18

Migration 0008 built client_performance from a shared perf-columns helper that omitted `sharpe`
(the model + ERD include it). Forward-only patch: add the column. Additive, gold-only.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_gold_client_perf_sharpe"
down_revision: str | None = "0008_gold"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("client_performance", sa.Column("sharpe", sa.Numeric(), nullable=True),
                  schema="gold")


def downgrade() -> None:
    op.drop_column("client_performance", "sharpe", schema="gold")
