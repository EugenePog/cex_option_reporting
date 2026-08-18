"""Gold-layer ORM models (schema: gold) — business-ready aggregates the UI reads.

Fully derived from silver; the silver->gold pipeline rebuilds them (truncate + insert), so they're
always reproducible. Each table has a surrogate `id` PK; grains are documented per table.

Currency: `*_usd` columns are USD; PnL columns (`realized_pnl`, `net_pnl`, `upl`, deal PnL) are in
the account settlement currency (coin, e.g. BTC) as OKX reports it. `return_pct`/`max_drawdown_pct`/
`sharpe` describe the USD equity curve.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

GOLD = "gold"


def _sub_fk() -> Mapped[int]:
    return mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)


def _strat_fk_nullable() -> Mapped[int | None]:
    return mapped_column(Integer, ForeignKey("core.strategy.id"), nullable=True, index=True)


def _user_fk() -> Mapped[int]:
    return mapped_column(Integer, ForeignKey("core.user.id"), index=True)


class BalanceTimeseries(Base):
    """Grain: (subaccount_id, captured_at). Total USD equity at each snapshot."""

    __tablename__ = "balance_timeseries"
    __table_args__ = (UniqueConstraint("subaccount_id", "captured_at", name="uq_gold_balance_ts"),
                      {"schema": GOLD})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class PnlDaily(Base):
    """Grain: (subaccount_id, strategy_id, date). realized net of fees; unrealized = EOD level."""

    __tablename__ = "pnl_daily"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    date: Mapped[date] = mapped_column(Date, index=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fees: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class StrategySummary(Base):
    """Grain: (subaccount_id, strategy_id). Current open-book snapshot per strategy."""

    __tablename__ = "strategy_summary"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    open_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    net_delta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_gamma: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_theta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_vega: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    upl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mtd_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class DealLedger(Base):
    """Grain: one row per closed/expired position (= a realized deal)."""

    __tablename__ = "deal_ledger"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    close_type: Mapped[str | None] = mapped_column(String(8), nullable=True)   # 'close' | 'expiry'
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    entry_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exit_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    hold_days: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PositionCurrent(Base):
    """Grain: (subaccount_id, inst_id) — the latest snapshot's open positions."""

    __tablename__ = "position_current"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mark_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    idx_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fwd_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    upl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    premium_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    delta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    theta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    vega: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    iv: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GreeksByExpiry(Base):
    """Grain: (subaccount_id, strategy_id?, underlying, expiry). strategy_id NULL = all-strategy roll-up."""

    __tablename__ = "greeks_by_expiry"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    net_delta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_gamma: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_theta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_vega: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    open_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    premium_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Admin / cross-client rollups ---------------------------------------- #
class ClientPnlDaily(Base):
    """Grain: (user_id, date)."""

    __tablename__ = "client_pnl_daily"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = _user_fk()
    date: Mapped[date] = mapped_column(Date, index=True)
    equity_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fees: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class ClientPerformance(Base):
    """Grain: (user_id, period). period ∈ {mtd, ytd, all}."""

    __tablename__ = "client_performance"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = _user_fk()
    period: Mapped[str] = mapped_column(String(8))
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    sharpe: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_win: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_loss: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_pnl_per_deal: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    n_deals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyPerformance(Base):
    """Grain: (user_id, subaccount_id, strategy_id, period)."""

    __tablename__ = "strategy_performance"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = _user_fk()
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    period: Mapped[str] = mapped_column(String(8))
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_win: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_loss: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_pnl_per_deal: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    n_deals: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SymbolPerformance(Base):
    """Grain: (user_id, subaccount_id, underlying, period)."""

    __tablename__ = "symbol_performance"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = _user_fk()
    subaccount_id: Mapped[int] = _sub_fk()
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    period: Mapped[str] = mapped_column(String(8))
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    profit_factor: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    n_deals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
