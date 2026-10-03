"""Gold-layer ORM models (schema: gold) — business-ready aggregates the UI reads.

Fully derived from silver; the silver->gold pipeline rebuilds them (truncate + insert), so they're
always reproducible. Each table has a surrogate `id` PK; grains are documented per table.

Currency: `*_usd` columns are USD; PnL columns (`realized_pnl`, `net_pnl`, `upl`, deal PnL) are in
the account settlement currency (coin, e.g. BTC) as OKX reports it. `return_pct`/`max_drawdown_pct`/
`sharpe` describe the USD equity curve.

Exception to the rebuild: `index_candle` (market data, added in 0021) is updated incrementally —
only the bars touched by new silver candles are recomputed — because it grows by ~1,440 minutes a
day and never changes otherwise.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
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


class UnderlyingPrice(Base):
    """Grain: (subaccount_id, underlying, captured_at). Underlying spot/forward over time.

    Powers the payoff report's spot marker and the settlement (expiration) price for expired
    options — keeps that report gold-only (no silver reads at query time).
    """

    __tablename__ = "underlying_price"
    __table_args__ = (UniqueConstraint("subaccount_id", "underlying", "captured_at",
                                       name="uq_gold_underlying_price"), {"schema": GOLD})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    idx_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fwd_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)


class ExpirySettlement(Base):
    """Grain: (subaccount_id, underlying, expiry). Official OKX settlement price at expiry
    (from delivery bills) — the true expiration price for report ③ (payoff)'s expired-mode marker."""

    __tablename__ = "expiry_settlement"
    __table_args__ = (UniqueConstraint("subaccount_id", "underlying", "expiry",
                                       name="uq_gold_expiry_settlement"), {"schema": GOLD})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    expiry: Mapped[date] = mapped_column(Date, index=True)
    settle_price: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssetBalanceTimeseries(Base):
    """Grain: (subaccount_id, ccy, captured_at). Per-asset balance in coin + its USD value.

    Powers the in-kind equity chart (e.g. BTC balance over time, denominated in BTC).
    """

    __tablename__ = "asset_balance_timeseries"
    __table_args__ = (UniqueConstraint("subaccount_id", "ccy", "captured_at",
                                       name="uq_gold_asset_balance_ts"), {"schema": GOLD})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    ccy: Mapped[str] = mapped_column(String(16), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[float | None] = mapped_column(Numeric, nullable=True)      # coin balance
    usd_value: Mapped[float | None] = mapped_column(Numeric, nullable=True)   # reference


class AssetPnlDaily(Base):
    """Grain: (subaccount_id, ccy, date). Daily P&L denominated in the asset (coin), not USD."""

    __tablename__ = "asset_pnl_daily"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    ccy: Mapped[str] = mapped_column(String(16), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unrealized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fees: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    # USD equivalents (coin × coin→USD rate at close), fee-inclusive net:
    realized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unrealized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fees_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)


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
    # USD equivalents (coin × coin→USD rate at close); net_pnl_usd is fee-inclusive (OKX nets fees):
    realized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    unrealized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fees_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    net_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)


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
    realized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # coin×rate (=② basis)
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
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # accrued fee (coin), for payoff
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


class PositionLeg(Base):
    """Grain: one row per position leg (silver.position_leg) — the Box builder board's read model.

    Copies the leg's identity, strategy assignment (+ source and what rules alone would give) and
    P&L, adding USD figures with the same coin→USD basis as gold.deal_ledger / pnl_daily:
    realized at the close-day rate, unrealized at the last-snapshot-day rate. Added in 0017.
    """

    __tablename__ = "position_leg"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    position_leg_id: Mapped[int] = mapped_column(BigInteger, index=True)   # silver.position_leg.id
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    # manual | rule | default
    strategy_source: Mapped[str | None] = mapped_column(String(8), nullable=True)
    rule_strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pos_id: Mapped[str] = mapped_column(String(64))
    pos_opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    inst_id: Mapped[str] = mapped_column(String(64), index=True)
    underlying: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    opt_type: Mapped[str | None] = mapped_column(String(2), nullable=True)
    strike: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    side: Mapped[str | None] = mapped_column(String(8), nullable=True)
    status: Mapped[str] = mapped_column(String(8))                  # open | closed | stale
    size: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    entry_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    exit_px: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_type: Mapped[str | None] = mapped_column(String(8), nullable=True)   # 'close' | 'expiry'
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Numeric, nullable=True)      # coin
    realized_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    upl: Mapped[float | None] = mapped_column(Numeric, nullable=True)               # coin
    upl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fee: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    ccy: Mapped[str | None] = mapped_column(String(16), nullable=True)
    n_fills: Mapped[int | None] = mapped_column(Integer, nullable=True)
    n_snapshots: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # price-with-boxes chart (0021): the leg is drawn as a line at `strike` from `pos_opened_at` to
    # `expires_at`; its size in coin = size (contracts) × ct_val from core.contract_size.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ct_val: Mapped[float | None] = mapped_column(Numeric, nullable=True)       # coin per contract
    size_coin: Mapped[float | None] = mapped_column(Numeric, nullable=True)    # size × ct_val
    coin: Mapped[str | None] = mapped_column(String(16), nullable=True)        # e.g. BTC


class BoxShape(Base):
    """Grain: (subaccount_id, strategy_id, underlying) — one rectangle of the dashboard chart
    "Price with strategy boxes". Added in 0021; rebuilt each run from gold.position_leg.

    A box (= strategy, see the Box builder) is drawn as the rectangle that covers all its legs of
    one underlying: left = first leg opened, right = last leg expiry (08:00 UTC), bottom / top =
    lowest / highest strike. Each leg is a line at its strike from open to expiry (the lines come
    from gold.position_leg). Label = total size of the legs, in coin. The `unassigned` box is stored
    too; the chart draws its legs without a rectangle.
    """

    __tablename__ = "box_shape"
    __table_args__ = ({"schema": GOLD},)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    subaccount_id: Mapped[int] = _sub_fk()
    strategy_id: Mapped[int | None] = _strat_fk_nullable()
    underlying: Mapped[str] = mapped_column(String(32), index=True)
    coin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # first leg opened
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))      # last leg expiry
    strike_lo: Mapped[float] = mapped_column(Numeric)
    strike_hi: Mapped[float] = mapped_column(Numeric)
    n_legs: Mapped[int] = mapped_column(Integer)
    n_open_legs: Mapped[int] = mapped_column(Integer)
    size_contracts: Mapped[float | None] = mapped_column(Numeric, nullable=True)   # Σ leg size
    size_coin: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # Σ size × ct_val
    status: Mapped[str] = mapped_column(String(8))                  # open (any leg open) | closed
    net_pnl_usd: Mapped[float | None] = mapped_column(Numeric, nullable=True)  # realized + upl
    leg_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger))   # silver.position_leg ids
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IndexCandle(Base):
    """Grain: (cex_code, inst_id, bar, ts) — index candles (e.g. BTC-USD) for the price chart.
    Added in 0021.

    Bars '1h', '4h', '1d' on UTC boundaries (4h = 00/04/08/12/16/20 UTC), aggregated from the
    1-minute silver.index_candle: open = first minute, close = last, high / low = max / min.
    `n_minutes` < bar length marks the still-forming last bar (or a gap). Updated incrementally:
    `src_max_id` is the highest silver id in the bar, so max(src_max_id) is the watermark and a
    run recomputes only the bars that received new minutes. Market data — no account columns.
    """

    __tablename__ = "index_candle"
    __table_args__ = (UniqueConstraint("cex_code", "inst_id", "bar", "ts",
                                       name="uq_gold_index_candle"), {"schema": GOLD})
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    cex_code: Mapped[str] = mapped_column(String(16))
    inst_id: Mapped[str] = mapped_column(String(32))
    bar: Mapped[str] = mapped_column(String(8))                 # '1h' | '4h' | '1d'
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # bar open time (UTC)
    open: Mapped[float] = mapped_column(Numeric)
    high: Mapped[float] = mapped_column(Numeric)
    low: Mapped[float] = mapped_column(Numeric)
    close: Mapped[float] = mapped_column(Numeric)
    n_minutes: Mapped[int] = mapped_column(Integer)
    src_max_id: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
