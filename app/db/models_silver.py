"""Silver-layer ORM models (schema: silver) — cleaned, typed, deduplicated, strategy-tagged.

Derived from bronze by the bronze->silver pipeline. One row per (entity, snapshot) for snapshots;
one row per event for fills / closed positions. Scoped to a core.subaccount; positions/fills/closed
carry a nullable strategy_id.
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

SILVER = "silver"


class PositionSnapshot(Base):
    __tablename__ = "position_snapshot"
    __table_args__ = (
        UniqueConstraint("subaccount_id", "inst_id", "side", "captured_at",
                         name="uq_position_snapshot"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True, index=True
    )
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    avg_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mark_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    idx_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)      # underlying index/spot (positions.idxPx)
    fwd_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)      # forward price (opt-summary.fwdPx)
    upl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    notional_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # positions.notionalUsd
    opt_val: Mapped[float | None] = mapped_column(Numeric, nullable=True)       # positions.optVal
    # Coin greeks (per-contract) from opt-summary:
    delta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    gamma: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    theta: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    vega: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    iv: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    # Black-Scholes dollar greeks (position-level) from positions:
    delta_bs: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    gamma_bs: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    theta_bs: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    vega_bs: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshot"
    __table_args__ = (
        UniqueConstraint("subaccount_id", "ccy", "captured_at", name="uq_balance_snapshot"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    ccy: Mapped[str] = mapped_column(String(32))
    total: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    available: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    usd_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))


class MarginSnapshot(Base):
    __tablename__ = "margin_snapshot"
    __table_args__ = (
        UniqueConstraint("subaccount_id", "scope", "captured_at", name="uq_margin_snapshot"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    scope: Mapped[str] = mapped_column(String(16))     # 'ACCOUNT' or a ccy code
    eq_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    imr_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    mmr_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    margin_ratio: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))


class TradeFill(Base):
    __tablename__ = "trade_fill"
    __table_args__ = (
        UniqueConstraint("cex_code", "trade_id", name="uq_silver_trade_fill"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True, index=True
    )
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee_ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    trade_id: Mapped[str] = mapped_column(String(64), index=True)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))


class ClosedPosition(Base):
    """Closed positions incl. expiry/delivery — typed realized PnL, one row per closed position."""

    __tablename__ = "closed_position"
    __table_args__ = (
        UniqueConstraint("cex_code", "ext_id", name="uq_silver_closed_position"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True, index=True
    )
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    close_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # closeTotalPos (contracts)
    open_avg_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    close_avg_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ext_id: Mapped[str] = mapped_column(String(64), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))
