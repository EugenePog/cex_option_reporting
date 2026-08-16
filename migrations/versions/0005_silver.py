"""silver schema: position/balance/margin snapshots, trade_fill, closed_position

Revision ID: 0005_silver
Revises: 0004_core
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_silver"
down_revision: str | None = "0004_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SILVER = "silver"


def _subaccount_fk() -> sa.Column:
    return sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"))


def _strategy_fk() -> sa.Column:
    return sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"), nullable=True)


def _instrument_cols() -> list[sa.Column]:
    return [
        sa.Column("inst_id", sa.String(64)),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
    ]


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SILVER}")

    op.create_table(
        "position_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        _subaccount_fk(), _strategy_fk(), *_instrument_cols(),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("size", sa.Numeric(), nullable=True),
        sa.Column("avg_px", sa.Numeric(), nullable=True),
        sa.Column("mark_px", sa.Numeric(), nullable=True),
        sa.Column("upl", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("delta", sa.Numeric(), nullable=True),
        sa.Column("gamma", sa.Numeric(), nullable=True),
        sa.Column("theta", sa.Numeric(), nullable=True),
        sa.Column("vega", sa.Numeric(), nullable=True),
        sa.Column("iv", sa.Numeric(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("subaccount_id", "inst_id", "side", "captured_at",
                            name="uq_position_snapshot"),
        schema=SILVER,
    )
    op.create_index("ix_position_snapshot_subaccount_id", "position_snapshot",
                    ["subaccount_id"], schema=SILVER)
    op.create_index("ix_position_snapshot_captured_at", "position_snapshot",
                    ["captured_at"], schema=SILVER)

    op.create_table(
        "balance_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        _subaccount_fk(),
        sa.Column("ccy", sa.String(32)),
        sa.Column("total", sa.Numeric(), nullable=True),
        sa.Column("available", sa.Numeric(), nullable=True),
        sa.Column("usd_value", sa.Numeric(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("subaccount_id", "ccy", "captured_at", name="uq_balance_snapshot"),
        schema=SILVER,
    )
    op.create_index("ix_balance_snapshot_subaccount_id", "balance_snapshot",
                    ["subaccount_id"], schema=SILVER)

    op.create_table(
        "margin_snapshot",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        _subaccount_fk(),
        sa.Column("scope", sa.String(16)),
        sa.Column("eq_usd", sa.Numeric(), nullable=True),
        sa.Column("imr_usd", sa.Numeric(), nullable=True),
        sa.Column("mmr_usd", sa.Numeric(), nullable=True),
        sa.Column("margin_ratio", sa.Numeric(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("subaccount_id", "scope", "captured_at", name="uq_margin_snapshot"),
        schema=SILVER,
    )

    op.create_table(
        "trade_fill",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        _subaccount_fk(), _strategy_fk(), *_instrument_cols(),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("size", sa.Numeric(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("fee_ccy", sa.String(16), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("trade_id", sa.String(64)),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("cex_code", "trade_id", name="uq_silver_trade_fill"),
        schema=SILVER,
    )
    op.create_index("ix_trade_fill_subaccount_id", "trade_fill", ["subaccount_id"], schema=SILVER)
    op.create_index("ix_trade_fill_filled_at", "trade_fill", ["filled_at"], schema=SILVER)

    op.create_table(
        "closed_position",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16)),
        _subaccount_fk(), _strategy_fk(), *_instrument_cols(),
        sa.Column("close_type", sa.String(8), nullable=True),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("open_avg_px", sa.Numeric(), nullable=True),
        sa.Column("close_avg_px", sa.Numeric(), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(), nullable=True),
        sa.Column("pnl", sa.Numeric(), nullable=True),
        sa.Column("fee", sa.Numeric(), nullable=True),
        sa.Column("ccy", sa.String(16), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("ext_id", sa.String(64)),
        sa.Column("ingest_id", sa.String(36)),
        sa.UniqueConstraint("cex_code", "ext_id", name="uq_silver_closed_position"),
        schema=SILVER,
    )
    op.create_index("ix_closed_position_subaccount_id", "closed_position",
                    ["subaccount_id"], schema=SILVER)
    op.create_index("ix_closed_position_closed_at", "closed_position",
                    ["closed_at"], schema=SILVER)


def downgrade() -> None:
    for t in ("closed_position", "trade_fill", "margin_snapshot", "balance_snapshot",
              "position_snapshot"):
        op.drop_table(t, schema=SILVER)
    op.execute(f"DROP SCHEMA IF EXISTS {SILVER}")
