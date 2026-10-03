"""silver/gold for the dashboard chart "Price with strategy boxes"

The chart draws the index price (candles) with every box (= strategy) on it: each option leg is a
line at its strike from open time to expiry, the box is the rectangle around all its legs, and its
label is the total size in coin. Bronze already has the 1-minute index candles (0019) and gold the
legs (0017); this adds the layers in between.

  silver
    * index_candle (NEW): typed 1-minute OHLC from bronze.raw_index_candle, one row per
      (cex_code, inst_id, bar, ts). Incremental: `bronze_id` = source row id, the watermark.
  gold
    * index_candle (NEW): 1h / 4h / 1d bars (UTC boundaries) from silver.index_candle, with
      `n_minutes` (partial bar) and `src_max_id` (watermark). Updated incrementally — only bars
      that received new minutes are recomputed; never truncated by the gold rebuild.
    * box_shape (NEW): one row per (subaccount, strategy, underlying) — rectangle edges (first open
      → last expiry, lowest → highest strike), leg count, total size (contracts and coin), P&L, the
      leg ids. Rebuilt each run from gold.position_leg.
    * position_leg: + expires_at (expiry 08:00 UTC = end of the leg's line), ct_val, size_coin,
      coin (contract size from core.contract_size, 0020).

Revision ID: 0021_price_boxes
Revises: 0020_contract_size
Create Date: 2026-10-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_price_boxes"
down_revision: str | None = "0020_contract_size"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S, G = "silver", "gold"


def _ohlc() -> list[sa.Column]:
    return [sa.Column(c, sa.Numeric(), nullable=False) for c in ("open", "high", "low", "close")]


def upgrade() -> None:
    # ---------------------------------------------------------------- silver
    op.create_table(
        "index_candle",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("inst_id", sa.String(32), nullable=False),
        sa.Column("bar", sa.String(8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        *_ohlc(),
        sa.Column("bronze_id", sa.BigInteger(), nullable=False),
        sa.Column("ingest_id", sa.String(36), nullable=True),
        sa.UniqueConstraint("cex_code", "inst_id", "bar", "ts", name="uq_silver_index_candle"),
        schema=S,
    )
    op.create_index("ix_index_candle_bronze_id", "index_candle", ["bronze_id"], schema=S)

    # ---------------------------------------------------------------- gold
    op.create_table(
        "index_candle",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("inst_id", sa.String(32), nullable=False),
        sa.Column("bar", sa.String(8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        *_ohlc(),
        sa.Column("n_minutes", sa.Integer(), nullable=False),
        sa.Column("src_max_id", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("cex_code", "inst_id", "bar", "ts", name="uq_gold_index_candle"),
        schema=G,
    )

    op.create_table(
        "box_shape",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"),
                  nullable=False),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"), nullable=True),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("coin", sa.String(16), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("strike_lo", sa.Numeric(), nullable=False),
        sa.Column("strike_hi", sa.Numeric(), nullable=False),
        sa.Column("n_legs", sa.Integer(), nullable=False),
        sa.Column("n_open_legs", sa.Integer(), nullable=False),
        sa.Column("size_contracts", sa.Numeric(), nullable=True),
        sa.Column("size_coin", sa.Numeric(), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("net_pnl_usd", sa.Numeric(), nullable=True),
        sa.Column("leg_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=G,
    )
    for col in ("subaccount_id", "strategy_id", "underlying"):
        op.create_index(f"ix_box_shape_{col}", "box_shape", [col], schema=G)

    op.add_column("position_leg", sa.Column("expires_at", sa.DateTime(timezone=True),
                                            nullable=True), schema=G)
    op.add_column("position_leg", sa.Column("ct_val", sa.Numeric(), nullable=True), schema=G)
    op.add_column("position_leg", sa.Column("size_coin", sa.Numeric(), nullable=True), schema=G)
    op.add_column("position_leg", sa.Column("coin", sa.String(16), nullable=True), schema=G)


def downgrade() -> None:
    for col in ("coin", "size_coin", "ct_val", "expires_at"):
        op.drop_column("position_leg", col, schema=G)
    op.drop_table("box_shape", schema=G)
    op.drop_table("index_candle", schema=G)
    op.drop_table("index_candle", schema=S)
