"""no-data alerts: notification codes no_data_warning / no_data_error + core.notification_feed_state

The alerts worker can only check a box's price alerts when it has a fresh index price for the box's
asset (the live OKX index, else the newest stored 1-minute candle). Two new notification codes tell
the chat when it can't:

    g  no_data_warning   ⚠ alerts can't check ASSET: no price for X min   X = threshold, minutes
    h  no_data_error     the same, later (critical)                        X = threshold, minutes

They are rows of core.notification_setting like the other codes (Settings → Telegram
notifications), per asset; the worker checks them per exchange × asset of the alive boxes.

  core
    * notification_setting: the code check constraint allows the two new codes; for every asset
      that already has notification settings, a g row (10 min, on) and an h row (30 min, on,
      critical) are added — the protection is on right after the upgrade. Existing rows untouched.
    * notification_feed_state (NEW, worker-written): one row per exchange × asset × code — armed,
      sent (a message went out for the current outage), last_sent_at, last_value (minutes without
      a price when sent), last_message, updated_at.

Revision ID: 0026_no_data_alerts
Revises: 0025_hedged_position
Create Date: 2026-11-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_no_data_alerts"
down_revision: str | None = "0025_hedged_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"
OLD_CODES = ("top_strike", "bottom_strike", "near_top_pct", "near_bottom_pct", "last_expiry",
             "last_expiry_days")
NEW_CODES = (*OLD_CODES, "no_data_warning", "no_data_error")


def _check(codes: tuple[str, ...]) -> str:
    return "code IN (" + ", ".join(f"'{c}'" for c in codes) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_notification_setting_code", "notification_setting", schema=C,
                       type_="check")
    op.create_check_constraint("ck_notification_setting_code", "notification_setting",
                               _check(NEW_CODES), schema=C)
    op.execute("""
        INSERT INTO core.notification_setting
               (asset, code, threshold, is_enabled, is_critical, note)
        SELECT a.asset, d.code, d.threshold, true, d.critical, d.note
          FROM (SELECT DISTINCT asset FROM core.notification_setting) a
         CROSS JOIN (VALUES
               ('no_data_warning', 10, false,
                'g: no fresh price for X min (X = threshold, minutes)'),
               ('no_data_error',   30, true,
                'h: no fresh price for X min (X = threshold, minutes)')
               ) AS d(code, threshold, critical, note)
        ON CONFLICT ON CONSTRAINT uq_notification_setting DO NOTHING
    """)
    op.create_table(
        "notification_feed_state",
        sa.Column("cex_code", sa.String(16), primary_key=True),
        sa.Column("asset", sa.String(32), primary_key=True),
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("armed", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sent", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_value", sa.Numeric(), nullable=True),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        schema=C,
    )


def downgrade() -> None:
    op.drop_table("notification_feed_state", schema=C)
    op.execute("DELETE FROM core.notification_setting "
               "WHERE code IN ('no_data_warning', 'no_data_error')")
    op.drop_constraint("ck_notification_setting_code", "notification_setting", schema=C,
                       type_="check")
    op.create_check_constraint("ck_notification_setting_code", "notification_setting",
                               _check(OLD_CODES), schema=C)
