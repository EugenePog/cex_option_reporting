"""core.contract_size — contract size (ctVal) per exchange, instrument type and underlying

Moves the contract size out of code (app/domain/pricing.py `_CONTRACT_SIZE`, BTC 0.01 / ETH 0.1 /
SOL 1) into the core schema, keyed by exchange — another exchange can size the same underlying
differently.

  core
    * contract_size (NEW): (cex_code, inst_type, underlying) → ct_val (coin units per contract),
      ct_val_ccy (that coin). Seeded here with the OKX option rows the code used to hardcode, and
      editable via seed/contract_size.csv (`make seed`, upsert by id).

Readers: the silver→gold pipeline (gold.position_leg.size_coin, gold.box_shape.size_coin — migration
0021) and the payoff report.

Revision ID: 0020_contract_size
Revises: 0019_raw_index_candle
Create Date: 2026-10-03
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_contract_size"
down_revision: str | None = "0019_raw_index_candle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

C = "core"

# Same values the code hardcoded until now (OKX public instruments ctVal for options).
_OKX_OPTIONS = [
    (1, "OKX", "OPTION", "BTC-USD", 0.01, "BTC", "OKX BTC-USD options: 1 contract = 0.01 BTC"),
    (2, "OKX", "OPTION", "ETH-USD", 0.1, "ETH", "OKX ETH-USD options: 1 contract = 0.1 ETH"),
    (3, "OKX", "OPTION", "SOL-USD", 1, "SOL", "OKX SOL-USD options: 1 contract = 1 SOL"),
]


def upgrade() -> None:
    t = op.create_table(
        "contract_size",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cex_code", sa.String(16), nullable=False),
        sa.Column("inst_type", sa.String(16), nullable=False),
        sa.Column("underlying", sa.String(32), nullable=False),
        sa.Column("ct_val", sa.Numeric(), nullable=False),
        sa.Column("ct_val_ccy", sa.String(16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.UniqueConstraint("cex_code", "inst_type", "underlying", name="uq_contract_size"),
        sa.CheckConstraint("ct_val > 0", name="ck_contract_size_positive"),
        schema=C,
    )
    op.bulk_insert(t, [dict(id=i, cex_code=cex, inst_type=it, underlying=u, ct_val=v,
                            ct_val_ccy=ccy, note=note)
                       for i, cex, it, u, v, ccy, note in _OKX_OPTIONS])
    op.execute("SELECT setval(pg_get_serial_sequence('core.contract_size', 'id'), "
               "(SELECT MAX(id) FROM core.contract_size))")


def downgrade() -> None:
    op.drop_table("contract_size", schema=C)
