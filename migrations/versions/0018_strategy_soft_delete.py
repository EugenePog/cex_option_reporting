"""box (strategy) delete from the Box builder = soft delete

Deleting a box in the Box builder pins every leg it holds to the account's `unassigned` box (one
changeset in core.strategy_link) and marks the strategy as deleted instead of removing the row:
silver/gold rows and the append-only pin history keep valid foreign keys, and Undo of that
changeset can restore the box (with its rules and pins).

  core
    * strategy: + deleted_at (NULL = active), deleted_by → core.user, deleted_changeset_id (the
      core.strategy_link changeset that moved its legs to unassigned; Undo restores the box).

A deleted strategy is hidden in the GUI (Box builder, report filters) and its strategy_rule rows
stop applying in bronze_to_silver; the rows themselves are kept so a restore brings them back.

Revision ID: 0018_strategy_soft_delete
Revises: 0017_position_leg_strategy_link
Create Date: 2026-10-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_strategy_soft_delete"
down_revision: str | None = "0017_position_leg_strategy_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"


def upgrade() -> None:
    op.add_column("strategy", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
                  schema=C)
    op.add_column("strategy", sa.Column("deleted_by", sa.Integer(), nullable=True), schema=C)
    op.add_column("strategy", sa.Column("deleted_changeset_id", postgresql.UUID(as_uuid=False),
                                        nullable=True), schema=C)
    op.create_foreign_key("fk_strategy_deleted_by_user", "strategy", "user", ["deleted_by"], ["id"],
                          source_schema=C, referent_schema=C)


def downgrade() -> None:
    # NOTE: deleted boxes become active (visible) again; their legs stay pinned to unassigned.
    op.drop_constraint("fk_strategy_deleted_by_user", "strategy", type_="foreignkey", schema=C)
    op.drop_column("strategy", "deleted_changeset_id", schema=C)
    op.drop_column("strategy", "deleted_by", schema=C)
    op.drop_column("strategy", "deleted_at", schema=C)
