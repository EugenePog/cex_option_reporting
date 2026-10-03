"""Silver-layer ORM models (schema: silver) — cleaned, typed, deduplicated, strategy-tagged.

Derived from bronze by the bronze->silver pipeline. One row per (entity, snapshot) for snapshots;
one row per event for fills / closed positions. Scoped to a core.subaccount.

Strategy tagging is done ONCE per position leg (`position_leg`, key = OKX posId + cTime):
manual pin (core.strategy_link) → strategy_rule → unassigned. Snapshots and closed positions
inherit the leg's strategy_id (+ strategy_source); fills carry no strategy of their own — they
link to their leg via `position_leg_id`.

Market data: `index_candle` holds the typed 1-minute index candles (e.g. BTC-USD) from
bronze.raw_index_candle — no account columns (added in 0021).
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
    # Leg link (0017): OKX posId + cTime from the payload, and the resolved silver.position_leg row.
    # manual | rule | default
    strategy_source: Mapped[str | None] = mapped_column(String(8), nullable=True)
    pos_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pos_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    position_leg_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("silver.position_leg.id", ondelete="SET NULL"), nullable=True,
        index=True,
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
    """One execution. Deduped on (cex_code, inst_id, trade_id) — OKX tradeId is per-instrument.

    No strategy_id of its own (removed in 0017): a fill belongs to a position leg
    (`position_leg_id`, resolved by subaccount + inst_id + fill time inside the leg's
    [cTime, uTime] window) and takes the leg's strategy via that link.
    """

    __tablename__ = "trade_fill"
    __table_args__ = (
        UniqueConstraint("cex_code", "inst_id", "trade_id", name="uq_silver_trade_fill"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    position_leg_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("silver.position_leg.id", ondelete="SET NULL"), nullable=True,
        index=True,
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


class Bill(Base):
    """Account ledger entries (parsed from bronze.raw_bill). Delivery rows (bill_type='3') carry
    the underlying **settlement price** (`px`) at expiry — the source for report ③ (payoff)'s expiry marker."""

    __tablename__ = "bill"
    __table_args__ = (
        UniqueConstraint("cex_code", "bill_id", name="uq_silver_bill"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    bill_id: Mapped[str] = mapped_column(String(64), index=True)
    inst_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    bill_type: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    sub_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    px: Mapped[float | None] = mapped_column(Numeric, nullable=True)      # settlement/fill price
    pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    billed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingest_id: Mapped[str] = mapped_column(String(36))


class ClosedPosition(Base):
    """Closed positions incl. expiry/delivery — typed realized PnL, one row per closed position."""

    __tablename__ = "closed_position"
    __table_args__ = (
        # leg key: (posId, cTime) — posId alone is re-used by OKX on reopen within 30 days (0017)
        UniqueConstraint("cex_code", "ext_id", "opened_at", name="uq_silver_closed_position",
                         postgresql_nulls_not_distinct=True),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"), index=True)
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True, index=True
    )
    # manual | rule | default
    strategy_source: Mapped[str | None] = mapped_column(String(8), nullable=True)
    position_leg_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("silver.position_leg.id", ondelete="SET NULL"), nullable=True,
        index=True,
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


class PositionLeg(Base):
    """One position lifecycle ("leg") — the single entity the Box builder moves between strategies.

    Key = (cex_code, subaccount_id, pos_id = OKX posId, pos_opened_at = OKX cTime). Built from
    bronze.raw_position (open legs, latest snapshot) and bronze.raw_closed_position (closed legs).
    The strategy is decided here ONCE (pin → rule → unassigned) and inherited by the leg's
    position_snapshot / closed_position rows; trade_fill rows link here via position_leg_id.
    Rows are upserted on the key, so `id` is stable across pipeline runs.
    """

    __tablename__ = "position_leg"
    __table_args__ = (
        UniqueConstraint("cex_code", "subaccount_id", "pos_id", "pos_opened_at",
                         name="uq_position_leg"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    subaccount_id: Mapped[int] = mapped_column(Integer, ForeignKey("core.subaccount.id"),
                                               index=True)
    pos_id: Mapped[str] = mapped_column(String(64), index=True)
    pos_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)        # long | short
    status: Mapped[str] = mapped_column(String(8))           # open | closed | stale (see pipeline)
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)        # contracts (max held)
    entry_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exit_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # coin (closed)
    upl: Mapped[float | None] = mapped_column(Numeric, nullable=True)           # coin (latest snap)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    idx_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)        # index at last snap
    n_fills: Mapped[int] = mapped_column(Integer, default=0)
    n_snapshots: Mapped[int] = mapped_column(Integer, default=0)
    strategy_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("core.strategy.id"), nullable=True, index=True
    )
    # manual | rule | default
    strategy_source: Mapped[str | None] = mapped_column(String(8), nullable=True)
    rule_strategy_id: Mapped[int | None] = mapped_column(          # what rules alone would assign
        Integer, ForeignKey("core.strategy.id"), nullable=True
    )
    strategy_link_id: Mapped[int | None] = mapped_column(          # the pin applied, if any
        BigInteger, ForeignKey("core.strategy_link.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IndexCandle(Base):
    """Index candle (e.g. OKX BTC-USD index), 1 minute, typed from bronze.raw_index_candle.
    Added in 0021.

    Market data — no account columns. One row per (cex_code, inst_id, bar, ts); `ts` is the candle
    OPEN time (UTC). Loaded incrementally: each run copies only bronze rows with an id above the
    highest `bronze_id` already here, so a gap the collector fills later (a new bronze row with a
    higher id) still reaches silver.
    """

    __tablename__ = "index_candle"
    __table_args__ = (
        UniqueConstraint("cex_code", "inst_id", "bar", "ts", name="uq_silver_index_candle"),
        {"schema": SILVER},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    inst_id: Mapped[str] = mapped_column(String(32))            # index, e.g. "BTC-USD"
    bar: Mapped[str] = mapped_column(String(8))                 # "1m"
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # candle open time (UTC)
    open: Mapped[float] = mapped_column(Numeric)
    high: Mapped[float] = mapped_column(Numeric)
    low: Mapped[float] = mapped_column(Numeric)
    close: Mapped[float] = mapped_column(Numeric)
    bronze_id: Mapped[int] = mapped_column(BigInteger, index=True)   # bronze.raw_index_candle.id
    ingest_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
