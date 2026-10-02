"""position legs + manual strategy links (Box builder)

The single movable entity is the POSITION LEG = one OKX position lifecycle, keyed by
(cex_code, subaccount_id, posId, cTime). OKX re-uses a posId when the same instrument is reopened
within 30 days of a full close, so cTime is part of the key everywhere.

  core
    * strategy: + UNIQUE (id, subaccount_id) — target of strategy_link's composite FK
    * strategy_link (NEW): append-only manual pins written by the admin Box builder; the current
      row per leg (superseded_at IS NULL) beats every strategy_rule.
  bronze
    * raw_closed_position: + pos_opened_at (= payload cTime, back-filled); dedupe key becomes
      (cex_code, ext_id, pos_opened_at) instead of (cex_code, ext_id).
  silver
    * position_leg (NEW): one row per leg; strategy decided here once (pin → rule → unassigned).
    * position_snapshot: + pos_id, pos_opened_at, position_leg_id, strategy_source.
    * closed_position: + position_leg_id, strategy_source; unique key (cex_code, ext_id, opened_at).
    * trade_fill: − strategy_id (fills inherit the leg's strategy); + position_leg_id.
  gold
    * position_leg (NEW): board read model with USD P&L (rebuilt each run).

Silver/gold link columns are filled by the next pipeline run (`python -m app.cli pipeline`).

Revision ID: 0017_position_leg_strategy_link
Revises: 0016_fill_dedupe_inst
Create Date: 2026-10-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_position_leg_strategy_link"
down_revision: str | None = "0016_fill_dedupe_inst"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C, B, S, G = "core", "bronze", "silver", "gold"


def upgrade() -> None:
    # ---------------------------------------------------------------- core
    op.create_unique_constraint("uq_strategy_id_subaccount", "strategy", ["id", "subaccount_id"],
                                schema=C)
    op.create_table(
        "strategy_link",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  nullable=False),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("pos_id", sa.String(64), nullable=False),
        sa.Column("pos_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inst_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("strategy_id", sa.Integer(), nullable=True),
        sa.Column("prev_strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"),
                  nullable=True),
        sa.Column("changeset_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("core.user.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["strategy_id", "subaccount_id"],
                                ["core.strategy.id", "core.strategy.subaccount_id"],
                                name="fk_strategy_link_strategy_same_subaccount"),
        sa.CheckConstraint("action IN ('pin', 'unpin')", name="ck_strategy_link_action"),
        sa.CheckConstraint("(action = 'pin') = (strategy_id IS NOT NULL)",
                           name="ck_strategy_link_pin_has_strategy"),
        schema=C,
    )
    op.create_index("uq_strategy_link_current", "strategy_link",
                    ["cex_code", "subaccount_id", "pos_id", "pos_opened_at"], unique=True,
                    postgresql_where=sa.text("superseded_at IS NULL"), schema=C)
    op.create_index("ix_strategy_link_changeset", "strategy_link", ["changeset_id"], schema=C)

    # ---------------------------------------------------------------- bronze
    op.add_column("raw_closed_position",
                  sa.Column("pos_opened_at", sa.DateTime(timezone=True), nullable=True), schema=B)
    op.execute(f"""
        UPDATE {B}.raw_closed_position
           SET pos_opened_at = to_timestamp((payload->>'cTime')::bigint / 1000.0)
         WHERE COALESCE(payload->>'cTime', '') ~ '^[0-9]+$'
    """)
    op.drop_constraint("uq_raw_closed_position_cex_ext", "raw_closed_position", type_="unique",
                       schema=B)
    op.create_unique_constraint("uq_raw_closed_position_leg", "raw_closed_position",
                                ["cex_code", "ext_id", "pos_opened_at"], schema=B,
                                postgresql_nulls_not_distinct=True)

    # ---------------------------------------------------------------- silver
    op.create_table(
        "position_leg",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  nullable=False),
        sa.Column("pos_id", sa.String(64), nullable=False),
        sa.Column("pos_opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inst_id", sa.String(64), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("size", sa.Numeric(), nullable=True),
        sa.Column("entry_px", sa.Numeric(), nullable=True),
        sa.Column("exit_px", sa.Numeric(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_type", sa.String(8), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("upl", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("ccy", sa.String(16), nullable=True),
        sa.Column("idx_px", sa.Numeric(), nullable=True),
        sa.Column("n_fills", sa.Integer(), server_default="0", nullable=False),
        sa.Column("n_snapshots", sa.Integer(), server_default="0", nullable=False),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"), nullable=True),
        sa.Column("strategy_source", sa.String(8), nullable=True),
        sa.Column("rule_strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"),
                  nullable=True),
        sa.Column("strategy_link_id", sa.BigInteger(),
                  sa.ForeignKey("core.strategy_link.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("cex_code", "subaccount_id", "pos_id", "pos_opened_at",
                            name="uq_position_leg"),
        schema=S,
    )
    for col in ("subaccount_id", "pos_id", "inst_id", "strategy_id"):
        op.create_index(f"ix_position_leg_{col}", "position_leg", [col], schema=S)

    def _leg_fk_col() -> sa.Column:
        return sa.Column("position_leg_id", sa.BigInteger(),
                         sa.ForeignKey("silver.position_leg.id", ondelete="SET NULL"),
                         nullable=True)

    # position_snapshot: leg key from the payload + resolved leg + strategy source
    op.add_column("position_snapshot", sa.Column("strategy_source", sa.String(8), nullable=True),
                  schema=S)
    op.add_column("position_snapshot", sa.Column("pos_id", sa.String(64), nullable=True), schema=S)
    op.add_column("position_snapshot",
                  sa.Column("pos_opened_at", sa.DateTime(timezone=True), nullable=True), schema=S)
    op.add_column("position_snapshot", _leg_fk_col(), schema=S)
    op.create_index("ix_position_snapshot_position_leg_id", "position_snapshot",
                    ["position_leg_id"], schema=S)

    # closed_position: leg link + source; key on (posId, cTime)
    op.add_column("closed_position", sa.Column("strategy_source", sa.String(8), nullable=True),
                  schema=S)
    op.add_column("closed_position", _leg_fk_col(), schema=S)
    op.create_index("ix_closed_position_position_leg_id", "closed_position", ["position_leg_id"],
                    schema=S)
    op.drop_constraint("uq_silver_closed_position", "closed_position", type_="unique", schema=S)
    op.create_unique_constraint("uq_silver_closed_position", "closed_position",
                                ["cex_code", "ext_id", "opened_at"], schema=S,
                                postgresql_nulls_not_distinct=True)

    # trade_fill: no strategy of its own any more — linked to its leg instead
    op.drop_column("trade_fill", "strategy_id", schema=S)
    op.add_column("trade_fill", _leg_fk_col(), schema=S)
    op.create_index("ix_trade_fill_position_leg_id", "trade_fill", ["position_leg_id"], schema=S)

    # ---------------------------------------------------------------- gold
    op.create_table(
        "position_leg",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("position_leg_id", sa.BigInteger(), nullable=False),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"), nullable=True),
        sa.Column("strategy_source", sa.String(8), nullable=True),
        sa.Column("rule_strategy_id", sa.Integer(), nullable=True),
        sa.Column("pos_id", sa.String(64)),
        sa.Column("pos_opened_at", sa.DateTime(timezone=True)),
        sa.Column("inst_id", sa.String(64)),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("status", sa.String(8)),
        sa.Column("size", sa.Numeric(), nullable=True),
        sa.Column("entry_px", sa.Numeric(), nullable=True),
        sa.Column("exit_px", sa.Numeric(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_type", sa.String(8), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("realized_pnl_usd", sa.Numeric(), nullable=True),
        sa.Column("upl", sa.Numeric(), nullable=True),
        sa.Column("upl_usd", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("ccy", sa.String(16), nullable=True),
        sa.Column("n_fills", sa.Integer(), nullable=True),
        sa.Column("n_snapshots", sa.Integer(), nullable=True),
        schema=G,
    )
    for col in ("position_leg_id", "subaccount_id", "strategy_id", "pos_opened_at", "inst_id",
                "underlying"):
        op.create_index(f"ix_gold_position_leg_{col}", "position_leg", [col], schema=G)


def downgrade() -> None:
    op.drop_table("position_leg", schema=G)

    op.drop_index("ix_trade_fill_position_leg_id", "trade_fill", schema=S)
    op.drop_column("trade_fill", "position_leg_id", schema=S)
    op.add_column("trade_fill", sa.Column("strategy_id", sa.Integer(),
                                          sa.ForeignKey("core.strategy.id"), nullable=True),
                  schema=S)

    op.drop_constraint("uq_silver_closed_position", "closed_position", type_="unique", schema=S)
    op.create_unique_constraint("uq_silver_closed_position", "closed_position",
                                ["cex_code", "ext_id"], schema=S)
    op.drop_index("ix_closed_position_position_leg_id", "closed_position", schema=S)
    op.drop_column("closed_position", "position_leg_id", schema=S)
    op.drop_column("closed_position", "strategy_source", schema=S)

    op.drop_index("ix_position_snapshot_position_leg_id", "position_snapshot", schema=S)
    for col in ("position_leg_id", "pos_opened_at", "pos_id", "strategy_source"):
        op.drop_column("position_snapshot", col, schema=S)
    op.drop_table("position_leg", schema=S)

    # NOTE: fails if a posId was legitimately re-used (two lifecycles) — dedupe first.
    op.drop_constraint("uq_raw_closed_position_leg", "raw_closed_position", type_="unique",
                       schema=B)
    op.create_unique_constraint("uq_raw_closed_position_cex_ext", "raw_closed_position",
                                ["cex_code", "ext_id"], schema=B)
    op.drop_column("raw_closed_position", "pos_opened_at", schema=B)

    op.drop_table("strategy_link", schema=C)
    op.drop_constraint("uq_strategy_id_subaccount", "strategy", type_="unique", schema=C)
