"""gold schema: balance_timeseries, pnl_daily, strategy_summary, deal_ledger, position_current,
greeks_by_expiry, client_pnl_daily, client_performance, strategy_performance, symbol_performance

Revision ID: 0008_gold
Revises: 0007_silver_closed_size
Create Date: 2026-08-18

Creates the gold schema + tables. Does not touch bronze or silver data.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_gold"
down_revision: str | None = "0007_silver_closed_size"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

G = "gold"


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True)


def _sub() -> sa.Column:
    return sa.Column("subaccount_id", sa.Integer(), sa.ForeignKey("core.subaccount.id"))


def _strat() -> sa.Column:
    return sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("core.strategy.id"), nullable=True)


def _user() -> sa.Column:
    return sa.Column("user_id", sa.Integer(), sa.ForeignKey("core.user.id"))


def _num(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(), nullable=True)


def _instrument_cols() -> list[sa.Column]:
    return [
        sa.Column("inst_id", sa.String(64)),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("opt_type", sa.String(2), nullable=True),
        sa.Column("strike", sa.Numeric(), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
    ]


def _perf_cols() -> list[sa.Column]:
    return [_num("net_pnl"), _num("return_pct"), _num("max_drawdown_pct"),
            _num("win_rate"), _num("profit_factor"), _num("avg_win"), _num("avg_loss"),
            _num("avg_pnl_per_deal"), sa.Column("n_deals", sa.Integer(), nullable=True)]


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {G}")

    op.create_table(
        "balance_timeseries", _id(), _sub(),
        sa.Column("captured_at", sa.DateTime(timezone=True)), _num("equity_usd"),
        sa.UniqueConstraint("subaccount_id", "captured_at", name="uq_gold_balance_ts"),
        schema=G,
    )
    op.create_index("ix_gold_bal_ts_sub", "balance_timeseries", ["subaccount_id"], schema=G)

    op.create_table(
        "pnl_daily", _id(), _sub(), _strat(),
        sa.Column("date", sa.Date()),
        _num("realized_pnl"), _num("unrealized_pnl"), _num("fees"), _num("net_pnl"),
        schema=G,
    )
    op.create_index("ix_gold_pnl_daily_sub_date", "pnl_daily", ["subaccount_id", "date"], schema=G)

    op.create_table(
        "strategy_summary", _id(), _sub(), _strat(),
        sa.Column("open_positions", sa.Integer(), nullable=True),
        _num("net_delta"), _num("net_gamma"), _num("net_theta"), _num("net_vega"),
        _num("upl"), _num("mtd_pnl"),
        schema=G,
    )

    op.create_table(
        "deal_ledger", _id(), _sub(), _strat(), *_instrument_cols(),
        sa.Column("side", sa.String(8), nullable=True),
        sa.Column("close_type", sa.String(8), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        _num("entry_px"), _num("exit_px"), _num("size"), _num("fee"), _num("realized_pnl"),
        sa.Column("hold_days", sa.Integer(), nullable=True),
        schema=G,
    )
    op.create_index("ix_gold_deal_sub_closed", "deal_ledger", ["subaccount_id", "closed_at"], schema=G)
    op.create_index("ix_gold_deal_underlying", "deal_ledger", ["underlying"], schema=G)

    op.create_table(
        "position_current", _id(), _sub(), _strat(), *_instrument_cols(),
        sa.Column("side", sa.String(8), nullable=True),
        _num("size"), _num("avg_px"), _num("mark_px"), _num("idx_px"), _num("fwd_px"),
        _num("upl"), _num("premium_usd"),
        _num("delta"), _num("gamma"), _num("theta"), _num("vega"), _num("iv"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        schema=G,
    )
    op.create_index("ix_gold_poscur_sub", "position_current", ["subaccount_id"], schema=G)

    op.create_table(
        "greeks_by_expiry", _id(), _sub(), _strat(),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("expiry", sa.Date(), nullable=True),
        _num("net_delta"), _num("net_gamma"), _num("net_theta"), _num("net_vega"),
        sa.Column("open_positions", sa.Integer(), nullable=True), _num("premium_usd"),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        schema=G,
    )
    op.create_index("ix_gold_greeks_sub_exp", "greeks_by_expiry",
                    ["subaccount_id", "underlying", "expiry"], schema=G)

    op.create_table(
        "client_pnl_daily", _id(), _user(),
        sa.Column("date", sa.Date()),
        _num("equity_usd"), _num("realized_pnl"), _num("unrealized_pnl"), _num("fees"), _num("net_pnl"),
        schema=G,
    )
    op.create_index("ix_gold_client_pnl_user_date", "client_pnl_daily", ["user_id", "date"], schema=G)

    op.create_table(
        "client_performance", _id(), _user(),
        sa.Column("period", sa.String(8)), *_perf_cols(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=G,
    )

    op.create_table(
        "strategy_performance", _id(), _user(), _sub(), _strat(),
        sa.Column("period", sa.String(8)), *_perf_cols(),
        schema=G,
    )

    op.create_table(
        "symbol_performance", _id(), _user(), _sub(),
        sa.Column("underlying", sa.String(32), nullable=True),
        sa.Column("period", sa.String(8)),
        _num("net_pnl"), _num("return_pct"), _num("win_rate"), _num("profit_factor"),
        sa.Column("n_deals", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        schema=G,
    )


def downgrade() -> None:
    for t in ("symbol_performance", "strategy_performance", "client_performance",
              "client_pnl_daily", "greeks_by_expiry", "position_current", "deal_ledger",
              "strategy_summary", "pnl_daily", "balance_timeseries"):
        op.drop_table(t, schema=G)
    op.execute(f"DROP SCHEMA IF EXISTS {G}")
