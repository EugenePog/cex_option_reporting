"""fix fill dedupe: key fills on (cex_code, inst_id, trade_id)

OKX `tradeId` is a counter PER INSTRUMENT, not unique across an account. Bronze and silver
deduplicated fills on (cex_code, trade_id), so a new fill whose tradeId was already used by an
older fill on a DIFFERENT instrument was silently skipped (ON CONFLICT DO NOTHING).

  * bronze.raw_trade_fill: + inst_id (back-filled from payload->>'instId'); unique key becomes
    (cex_code, inst_id, trade_id).
  * silver.trade_fill: unique key becomes (cex_code, inst_id, trade_id).

Existing rows are unique under the old key, hence also under the new (wider) one. Fills that were
dropped before this fix are recovered by re-collecting history (`python -m app.cli backfill`)
— only within OKX's fills-history window (~3 months).

Revision ID: 0016_fill_dedupe_inst
Revises: 0015_gold_deal_realized_usd
Create Date: 2026-10-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_fill_dedupe_inst"
down_revision: str | None = "0015_gold_deal_realized_usd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

B, S = "bronze", "silver"


def upgrade() -> None:
    # bronze: add + back-fill inst_id, then swap the unique key
    op.add_column("raw_trade_fill", sa.Column("inst_id", sa.String(64), nullable=True), schema=B)
    op.execute(f"UPDATE {B}.raw_trade_fill SET inst_id = COALESCE(payload->>'instId', '')")
    op.alter_column("raw_trade_fill", "inst_id", nullable=False, schema=B)
    op.create_index("ix_raw_trade_fill_inst_id", "raw_trade_fill", ["inst_id"], schema=B)
    op.drop_constraint("uq_raw_trade_fill_cex_trade", "raw_trade_fill", type_="unique", schema=B)
    op.create_unique_constraint("uq_raw_trade_fill_cex_inst_trade", "raw_trade_fill",
                                ["cex_code", "inst_id", "trade_id"], schema=B)

    # silver: widen the unique key (same constraint name, new columns)
    op.drop_constraint("uq_silver_trade_fill", "trade_fill", type_="unique", schema=S)
    op.create_unique_constraint("uq_silver_trade_fill", "trade_fill",
                                ["cex_code", "inst_id", "trade_id"], schema=S)


def downgrade() -> None:
    # NOTE: fails if fills now legitimately share a tradeId across instruments (expected after
    # the fix) — delete the extra rows first if you really need to go back.
    op.drop_constraint("uq_silver_trade_fill", "trade_fill", type_="unique", schema=S)
    op.create_unique_constraint("uq_silver_trade_fill", "trade_fill", ["cex_code", "trade_id"],
                                schema=S)
    op.drop_constraint("uq_raw_trade_fill_cex_inst_trade", "raw_trade_fill", type_="unique",
                       schema=B)
    op.create_unique_constraint("uq_raw_trade_fill_cex_trade", "raw_trade_fill",
                                ["cex_code", "trade_id"], schema=B)
    op.drop_index("ix_raw_trade_fill_inst_id", "raw_trade_fill", schema=B)
    op.drop_column("raw_trade_fill", "inst_id", schema=B)
