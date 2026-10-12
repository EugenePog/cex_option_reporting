"""core.strategy.send_notifications — switch a box's Telegram alerts on / off

The alerts worker (0022) watches every alive box. This adds a per-box switch, edited in the Box
builder's New box / ✎ edit dialog ("Send notifications"):

  core
    * strategy: + send_notifications boolean NOT NULL DEFAULT true — every existing box keeps
      getting alerts. When false the worker skips the box completely: nothing is sent and no
      core.notification_state row is written; rows written while it was on are kept and used again
      when it is switched back on (a box that never had rows starts fresh, like a new box).

Revision ID: 0023_strategy_notifications
Revises: 0022_notifications
Create Date: 2026-10-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_strategy_notifications"
down_revision: str | None = "0022_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("strategy", sa.Column("send_notifications", sa.Boolean(), nullable=False,
                                        server_default=sa.true()), schema="core")


def downgrade() -> None:
    op.drop_column("strategy", "send_notifications", schema="core")
