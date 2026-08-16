"""core schema: user, cex_account, subaccount, strategy, strategy_rule, instrument, audit_log, watermark

Revision ID: 0004_core
Revises: 0003_bills
Create Date: 2026-08-15
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_core"
down_revision: str | None = "0003_bills"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE = "core"


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {CORE}")

    op.create_table(
        "user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", sa.String(16), server_default="client"),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_user_email"),
        schema=CORE,
    )

    op.create_table(
        "cex_account",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("core.user.id")),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("label", sa.String(64)),
        sa.Column("api_key_enc", sa.Text(), nullable=True),
        sa.Column("api_secret_enc", sa.Text(), nullable=True),
        sa.Column("passphrase_enc", sa.Text(), nullable=True),
        sa.Column("flag", sa.String(4), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=CORE,
    )

    op.create_table(
        "subaccount",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cex_account_id", sa.Integer(), sa.ForeignKey("core.cex_account.id")),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("subacct_name", sa.String(64), server_default=""),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true()),
        schema=CORE,
    )

    op.create_table(
        "strategy",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("name", sa.String(64)),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=CORE,
    )

    op.create_table(
        "strategy_rule",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id")),
        sa.Column("match_json", postgresql.JSONB()),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id")),
        sa.Column("priority", sa.Integer(), server_default="100"),
        schema=CORE,
    )

    op.create_table(
        "instrument",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cex_code", sa.String(16)),
        sa.Column("inst_id", sa.String(64)),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        sa.Column("contract_ccy", sa.String(16), nullable=True),
        sa.Column("tick_size", sa.Numeric(), nullable=True),
        sa.UniqueConstraint("cex_code", "inst_id", name="uq_instrument_cex_inst"),
        schema=CORE,
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("core.user.id"), nullable=True),
        sa.Column("action", sa.String(64)),
        sa.Column("target", sa.String(255), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema=CORE,
    )

    op.create_table(
        "pipeline_watermark",
        sa.Column("stage", sa.String(16), primary_key=True),
        sa.Column("cex_code", sa.String(16), primary_key=True),
        sa.Column("last_processed_ts", sa.DateTime(timezone=True), nullable=True),
        schema=CORE,
    )


def downgrade() -> None:
    for t in ("pipeline_watermark", "audit_log", "instrument", "strategy_rule", "strategy",
              "subaccount", "cex_account", "user"):
        op.drop_table(t, schema=CORE)
    op.execute(f"DROP SCHEMA IF EXISTS {CORE}")
