"""enrich silver.position_snapshot: idx/fwd px, notional_usd, opt_val, dollar greeks

Revision ID: 0006_silver_position_enrich
Revises: 0005_silver
Create Date: 2026-08-16

ADDITIVE ONLY — adds nullable columns to silver.position_snapshot. Does NOT touch bronze (or any
other table), so no ingested data is read, moved, or erased. Existing silver rows get NULL for the
new columns until the next `pipeline` run re-upserts them from bronze.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_silver_position_enrich"
down_revision: str | None = "0005_silver"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SILVER = "silver"
TABLE = "position_snapshot"

_NEW_COLUMNS = [
    "idx_px", "fwd_px", "notional_usd", "opt_val",
    "delta_bs", "gamma_bs", "theta_bs", "vega_bs",
]


def upgrade() -> None:
    for col in _NEW_COLUMNS:
        op.add_column(TABLE, sa.Column(col, sa.Numeric(), nullable=True), schema=SILVER)


def downgrade() -> None:
    for col in reversed(_NEW_COLUMNS):
        op.drop_column(TABLE, col, schema=SILVER)
