"""Hedged amount Q moves from account × asset to the BOX (build 22)

Q — the coin held besides the options and hedged by them, for the future-PnL panel of graph ① —
is now set per box (strategy) in the Box builder's New box / ✎ dialog, "Position hedged". The panel
adds up the Q of the boxes it shows (in the legend and switched on), for the coin of the chosen
underlying.

  core
    * strategy: + hedged_qty  numeric NULL (≥ 0; NULL = not set)
                + hedged_ccy  varchar(16) NULL (the asset, e.g. 'BTC'; set together with the qty)
    * hedged_position (0025, account × asset): DROPPED — an account's Q can't be split into its
      boxes automatically. Its rows are printed by this migration so they can be re-entered on
      the boxes (Box builder → ✎ → Position hedged).

Revision ID: 0028_box_hedged
Revises: 0027_leg_cash
Create Date: 2026-11-22
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_box_hedged"
down_revision: str | None = "0027_leg_cash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"


def upgrade() -> None:
    op.add_column("strategy", sa.Column("hedged_qty", sa.Numeric(), nullable=True), schema=C)
    op.add_column("strategy", sa.Column("hedged_ccy", sa.String(16), nullable=True), schema=C)
    op.create_check_constraint("ck_strategy_hedged_qty", "strategy", "hedged_qty >= 0", schema=C)

    conn = op.get_bind()
    if not op.get_context().as_sql:                  # offline (--sql) runs can't read the rows
        rows = conn.execute(sa.text(
            "SELECT h.subaccount_id, a.label, s.display_name, s.subacct_name, h.ccy, h.qty, h.note "
            "FROM core.hedged_position h JOIN core.subaccount s ON s.id = h.subaccount_id "
            "JOIN core.cex_account a ON a.id = s.cex_account_id ORDER BY 1, 5")).all()
        if rows:
            print("\n0028: core.hedged_position is dropped — Q is set per box now. Re-enter these "
                  "on the boxes (Box builder → ✎ → Position hedged):")
            for sub_id, label, disp, sub_name, ccy, qty, note in rows:
                name = f"{label} · {disp or sub_name or 'main'}"
                print(f"  account {sub_id} ({name}): {qty} {ccy}" + (f" — {note}" if note else ""))
    op.drop_table("hedged_position", schema=C)


def downgrade() -> None:
    op.create_table(
        "hedged_position",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  nullable=False),
        sa.Column("ccy", sa.String(16), nullable=False),
        sa.Column("qty", sa.Numeric(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("subaccount_id", "ccy", name="uq_hedged_position"),
        sa.CheckConstraint("qty >= 0", name="ck_hedged_position_qty"),
        schema=C,
    )
    op.drop_constraint("ck_strategy_hedged_qty", "strategy", schema=C, type_="check")
    op.drop_column("strategy", "hedged_ccy", schema=C)
    op.drop_column("strategy", "hedged_qty", schema=C)
