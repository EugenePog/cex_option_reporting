"""core notification settings + state — Telegram alerts for graph ① "Price with strategy boxes"

The alerts worker (pm2 `worker`, `python -m app.cli worker --loop`) watches the boxes drawn on
graph ① and sends Telegram messages. Only *alive* boxes are watched: at least one leg still open
(not closed, not expired).

  core
    * notification_setting (NEW, seeded): one row per asset × notification code — the same
      settings for every account and box of that asset. Columns: asset (e.g. 'BTC-USD'), code,
      threshold (0 when the code has none), is_enabled (on / off), is_critical (🔴 CRITICAL in the
      chat). Codes:
          a  top_strike        price ≥ top strike of the box
          b  bottom_strike     price ≤ bottom strike
          c  near_top_pct      price ≥ top strike − X %          threshold = X
          d  near_bottom_pct   price ≤ bottom strike + Y %       threshold = Y
          e  last_expiry       time reaches the box's last expiry
          f  last_expiry_days  time reaches the last expiry − N days   threshold = N
      Seeded for BTC-USD (all on; a, b, e critical; X = Y = 2 %, N = 1 day); editable in
      seed/notification_setting.csv (`make seed`, upsert by id) or directly in the table.
    * notification_state (NEW, app-written): the worker's bookkeeping per box × code, so every
      notification is sent once per crossing (price codes re-arm when the price is back on the
      other side; time codes remember the last expiry they were sent for).

Revision ID: 0022_notifications
Revises: 0021_price_boxes
Create Date: 2026-10-10
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_notifications"
down_revision: str | None = "0021_price_boxes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"

CODES = ("top_strike", "bottom_strike", "near_top_pct", "near_bottom_pct", "last_expiry",
         "last_expiry_days")

# (id, asset, code, threshold, is_enabled, is_critical, note)
_DEFAULTS = [
    (1, "BTC-USD", "top_strike", 0, True, True, "a: price reaches the top strike of a box"),
    (2, "BTC-USD", "bottom_strike", 0, True, True, "b: price reaches the bottom strike of a box"),
    (3, "BTC-USD", "near_top_pct", 2, True, False,
     "c: price reaches top strike - X % (X = threshold)"),
    (4, "BTC-USD", "near_bottom_pct", 2, True, False,
     "d: price reaches bottom strike + Y % (Y = threshold)"),
    (5, "BTC-USD", "last_expiry", 0, True, True, "e: time reaches the last expiry of a box"),
    (6, "BTC-USD", "last_expiry_days", 1, True, False,
     "f: time reaches last expiry - N days (N = threshold)"),
]


def upgrade() -> None:
    t = op.create_table(
        "notification_setting",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset", sa.String(32), nullable=False),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("threshold", sa.Numeric(), server_default="0", nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_critical", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("asset", "code", name="uq_notification_setting"),
        sa.CheckConstraint("code IN (" + ", ".join(f"'{c}'" for c in CODES) + ")",
                           name="ck_notification_setting_code"),
        sa.CheckConstraint("threshold >= 0", name="ck_notification_setting_threshold"),
        schema=C,
    )
    op.bulk_insert(t, [dict(id=i, asset=a, code=c, threshold=th, is_enabled=en, is_critical=cr,
                            note=note)
                       for i, a, c, th, en, cr, note in _DEFAULTS])
    op.execute("SELECT setval(pg_get_serial_sequence('core.notification_setting', 'id'), "
               "(SELECT MAX(id) FROM core.notification_setting))")

    op.create_table(
        "notification_state",
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"),
                  primary_key=True),
        sa.Column("underlying", sa.String(32), primary_key=True),
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("armed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("fired_for_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_value", sa.Numeric(), nullable=True),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        schema=C,
    )


def downgrade() -> None:
    op.drop_table("notification_state", schema=C)
    op.drop_table("notification_setting", schema=C)
