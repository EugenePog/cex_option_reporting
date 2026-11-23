"""Silver -> Gold transform.

Gold is fully derived and **rebuilt** each run (delete + insert, one transaction) — simplest and
always consistent at this scale. Reads only silver + core; never touches bronze.

Build 20 — no off-time for readers: the old rows are removed with DELETE, not TRUNCATE. TRUNCATE
takes an ACCESS EXCLUSIVE lock held until the commit, so every page, API call and the alerts worker
reading gold waited for the whole rebuild (and a slow report query in flight held the rebuild — and
everything queued behind it — up). DELETE only takes ROW EXCLUSIVE, which readers don't conflict
with: until this run commits they keep seeing the previous gold in full (MVCC), then the new gold
appears all at once. Never empty, never half-built. Costs: the old rows become dead tuples that
autovacuum removes, and the surrogate `id`s keep counting up instead of restarting at 1 (bigint;
nothing relies on them — TRUNCATE … RESTART IDENTITY renumbered them on every run anyway).

Exception: gold.index_candle (index price bars for the "Price with strategy boxes" chart) is market
data that only grows, so it is updated incrementally — each run recomputes only the 1h / 4h / 1d /
1do bars that received new 1-minute candles in silver (see _build_index_candles).

Definitions:
  * A "deal" = one silver.closed_position (realized, incl. expiry). realized_pnl is in the account
    settlement currency (coin) as OKX reports it.
  * "current" = rows from the latest snapshot per subaccount.
  * Periods: mtd = month-to-date, ytd = year-to-date, all = everything (UTC).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.db.base import session_scope
from app.db.contract_sizes import load_contract_sizes
from app.db.models_core import CexAccount, Subaccount
from app.db.models_gold import (
    AssetBalanceTimeseries,
    AssetPnlDaily,
    BalanceTimeseries,
    BoxShape,
    ClientPerformance,
    ClientPnlDaily,
    DealLedger,
    ExpirySettlement,
    GreeksByExpiry,
    PnlDaily,
    PositionCurrent,
    PositionLeg as GoldPositionLeg,
    StrategyPerformance,
    StrategySummary,
    SymbolPerformance,
    UnderlyingPrice,
)
from app.db.models_silver import (
    BalanceSnapshot,
    Bill,
    ClosedPosition,
    PositionLeg,
    PositionSnapshot,
    TradeFill,
)
from app.domain.boxes import BAR_OFFSET, BAR_STEP, BARS, Leg, build_boxes
from app.domain.contracts import ContractSizes, inst_type_of
from app.domain.instruments import expires_at
from app.domain.leg_cash import (
    Fill,
    LegCash,
    LegInfo,
    Snap,
    fill_events,
    fills_cover,
    leg_cash,
)
from app.domain.metrics import deal_metrics, equity_metrics

logger = logging.getLogger(__name__)

_GOLD_TABLES = [  # emptied (DELETE, build 20) in this order at the start of each rebuild
    "balance_timeseries", "asset_balance_timeseries", "pnl_daily", "asset_pnl_daily",
    "strategy_summary", "deal_ledger", "position_current", "underlying_price",
    "expiry_settlement", "greeks_by_expiry", "client_pnl_daily", "client_performance",
    "strategy_performance", "symbol_performance", "position_leg", "box_shape",
]  # NOT index_candle: incremental (see _build_index_candles)


def _fl(v) -> float:
    return float(v) if v is not None else 0.0


def _period_start(period: str, today: date) -> date:
    if period == "mtd":
        return today.replace(day=1)
    if period == "ytd":
        return today.replace(month=1, day=1)
    return date(1970, 1, 1)  # 'all'


EXPIRY_WINDOW = timedelta(minutes=5)


def _close_type(closed_at: datetime | None, expiry: date | None) -> str:
    """build 24 — 'expiry' when the position closed within ±5 minutes of its expiry (08:00 UTC on
    the expiry date), else 'close' (a trade). OKX positions-history `type` has no expiry code
    (1 partial close, 2 close all, 3 liquidation, 4 partial liquidation, 5 / 6 ADL): expiries come
    as 2, so the type is not used (before build 24, 3 was read as expiry)."""
    exp = expires_at(expiry)
    return "expiry" if (closed_at is not None and exp is not None
                        and abs(closed_at - exp) <= EXPIRY_WINDOW) else "close"


def run() -> dict[str, int]:
    """Rebuild all gold tables from silver. Returns {table: rows_written}."""
    now = datetime.now(timezone.utc)
    today = now.date()
    counts: dict[str, int] = {}

    with session_scope() as s:
        # subaccount -> user_id map
        sub_user = dict(s.execute(
            select(Subaccount.id, CexAccount.user_id)
            .join(CexAccount, CexAccount.id == Subaccount.cex_account_id)
        ).all())

        # Empty gold for the rebuild (build 20: DELETE, not TRUNCATE — readers keep seeing the
        # previous gold until this transaction commits instead of waiting for it; see the docstring)
        for t in _GOLD_TABLES:
            s.execute(text(f"DELETE FROM gold.{t}"))

        rates = _CoinUsd(s)  # coin→USD conversion for USD-denominated gold columns
        counts["balance_timeseries"] = _build_balance_ts(s)
        counts["asset_balance_timeseries"] = _build_asset_balance_ts(s)
        eod = _eod_equity(s)  # end-of-day USD equity per (subaccount, day) — used by rollups
        counts["position_current"] = _build_position_current(s)
        counts["underlying_price"] = _build_underlying_price(s)
        counts["expiry_settlement"] = _build_expiry_settlement(s)
        counts["greeks_by_expiry"] = _build_greeks_by_expiry(s)
        counts["strategy_summary"] = _build_strategy_summary(s, today)
        sizes = load_contract_sizes(s)   # contract size per exchange (core.contract_size)
        # build 25: each leg's cash at operation-time prices, computed once — graph ① (position_leg)
        # and, for closed positions, the USD of ② (pnl_daily), ②b (asset_pnl_daily) and Analyze
        # (deal_ledger): premium + opening fees at the opening prices, closing / settlement at the
        # closing prices, as graph ①'s hover
        leg_rows = s.execute(select(PositionLeg)).scalars().all()
        cash = _leg_cash_all(s, leg_rows, rates, sizes)
        closed_cash = {lg.id: cash[lg.id] for lg in leg_rows
                       if lg.status == "closed" and lg.id in cash}
        counts["asset_pnl_daily"] = _build_asset_pnl_daily(s, rates, closed_cash)
        counts["deal_ledger"] = _build_deal_ledger(s, rates, closed_cash)
        counts["position_leg"], legs = _build_position_leg(s, rates, sizes, leg_rows, cash)
        counts["box_shape"] = _build_box_shape(s, legs, now)
        counts["index_candle"] = _build_index_candles(s)
        counts["pnl_daily"] = _build_pnl_daily(s, rates, closed_cash)
        counts["client_pnl_daily"] = _build_client_pnl_daily(s, sub_user, eod)
        counts.update(_build_performance(s, sub_user, today, now, eod))

    for t, n in counts.items():
        logger.info("gold %s: %d rows", t, n)
    return counts


# --------------------------------------------------------------------------- #
def _latest_positions(s) -> list[PositionSnapshot]:
    """Open positions from the most recent snapshot per subaccount."""
    latest = dict(s.execute(
        select(PositionSnapshot.subaccount_id, func.max(PositionSnapshot.captured_at))
        .group_by(PositionSnapshot.subaccount_id)
    ).all())
    if not latest:
        return []
    rows = s.execute(select(PositionSnapshot)).scalars().all()
    return [r for r in rows if latest.get(r.subaccount_id) == r.captured_at
            and _fl(r.size) != 0]


def _build_balance_ts(s) -> int:
    # equity per (subaccount, captured_at) = sum of usd_value across ccy
    agg = s.execute(
        select(BalanceSnapshot.subaccount_id, BalanceSnapshot.captured_at,
               func.sum(BalanceSnapshot.usd_value))
        .group_by(BalanceSnapshot.subaccount_id, BalanceSnapshot.captured_at)
    ).all()
    for sub_id, cap, eq in agg:
        s.add(BalanceTimeseries(subaccount_id=sub_id, captured_at=cap, equity_usd=eq))
    return len(agg)


def _build_asset_balance_ts(s) -> int:
    # per-asset (coin) balance timeseries straight from silver.balance_snapshot
    rows = s.execute(select(BalanceSnapshot)).scalars().all()
    for b in rows:
        s.add(AssetBalanceTimeseries(
            subaccount_id=b.subaccount_id, ccy=b.ccy, captured_at=b.captured_at,
            amount=b.total, usd_value=b.usd_value))
    return len(rows)


def _asset_of(underlying: str | None) -> str | None:
    """Settlement asset of an option = base of the underlying (BTC-USD -> BTC)."""
    return underlying.split("-")[0] if underlying else None


class _CoinUsd:
    """coin -> USD conversion per (subaccount, ccy, day).

    Primary rate = usd_value/total at the last balance snapshot of the day (covers every held coin,
    incl. stablecoins ≈ 1). Fallback = underlying index price (idx_px) for option settlement coins.
    Missing exact days fall back to the nearest known day for that (subaccount, ccy)."""

    def __init__(self, s):
        best: dict[tuple, tuple[datetime, float]] = {}  # (sub,ccy,day) -> (captured_at, rate)
        for b in s.execute(select(BalanceSnapshot)).scalars():
            tot = _fl(b.total)
            if b.captured_at is None or tot == 0 or b.usd_value is None:
                continue
            k = (b.subaccount_id, b.ccy, b.captured_at.date())
            if k not in best or b.captured_at > best[k][0]:
                best[k] = (b.captured_at, _fl(b.usd_value) / tot)
        self._rate = {k: v[1] for k, v in best.items()}
        idx: dict[tuple, tuple[datetime, float]] = {}
        for p in s.execute(select(PositionSnapshot)).scalars():
            base = _asset_of(p.underlying)
            if base is None or p.idx_px is None or p.captured_at is None:
                continue
            k = (p.subaccount_id, base, p.captured_at.date())
            if k not in idx or p.captured_at > idx[k][0]:
                idx[k] = (p.captured_at, _fl(p.idx_px))
        for k, v in idx.items():
            self._rate.setdefault(k, v[1])
        # per (sub,ccy) sorted days for nearest-day fallback
        self._by_cc: dict[tuple, list[tuple[date, float]]] = defaultdict(list)
        for (sub, ccy, d), r in self._rate.items():
            self._by_cc[(sub, ccy)].append((d, r))
        for v in self._by_cc.values():
            v.sort()

    def rate(self, sub: int, ccy: str | None, d: date) -> float:
        if ccy is None:
            return 1.0
        r = self._rate.get((sub, ccy, d))
        if r is not None:
            return r
        cand = self._by_cc.get((sub, ccy))
        if cand:
            return min(cand, key=lambda t: abs((t[0] - d).days))[1]
        return 1.0 if ccy.upper().startswith("USD") else 1.0


def _deal_usd(cp, closed_cash: dict[int, LegCash], rates: _CoinUsd) -> tuple[float, float, str]:
    """build 25 — USD of one closed position: (net, fees, basis). 'trades' = as graph ①'s hover:
    the leg's premium + opening fees at the opening prices + closing trades / settlement and their
    fees at the closing prices (app.domain.leg_cash; its coin total = OKX realizedPnl). 'day rate'
    = the old rule, coin × the coin's USD rate on the closing day — used when the leg has no cash
    data or an operation had no price."""
    c = closed_cash.get(cp.position_leg_id) if cp.position_leg_id is not None else None
    if c is not None and not c.missing_px and c.premium_usd is not None and c.close_usd is not None:
        return c.premium_usd + c.close_usd, c.fee_usd or 0.0, "trades"
    r = rates.rate(cp.subaccount_id, cp.ccy, cp.closed_at.date()) if cp.closed_at else 1.0
    return _fl(cp.realized_pnl) * r, _fl(cp.fee) * r, "day rate"


def _upl_usd(p, rates: _CoinUsd) -> float:
    """build 25 — an open position's unrealized PnL in USD at the index price of its own snapshot
    (the moment), as graph ①'s "value now"; the day rate only when the snapshot has no price."""
    if p.idx_px is not None:
        return _fl(p.upl) * _fl(p.idx_px)
    return _fl(p.upl) * rates.rate(p.subaccount_id, _asset_of(p.underlying), p.captured_at.date())


def _build_asset_pnl_daily(s, rates: _CoinUsd, closed_cash: dict[int, LegCash]) -> int:
    # coin-denominated. OKX realizedPnl already NETS fees (= pnl + fee + funding), so it IS net_pnl;
    # gross realized is derived as net - fees (fee is negative), and fees are shown separately.
    # build 25: USD as graph ① (operation-time prices, `_deal_usd`; unrealized at the snapshot's
    # index price) instead of × the day rate
    agg: dict[tuple, dict] = defaultdict(lambda: dict(net=0.0, fee=0.0, net_usd=0.0, fee_usd=0.0))
    for cp in s.execute(select(ClosedPosition)).scalars():
        if not cp.closed_at or not cp.ccy:
            continue
        k = (cp.subaccount_id, cp.ccy, cp.closed_at.date())
        net_usd, fee_usd, _basis = _deal_usd(cp, closed_cash, rates)
        agg[k]["net"] += _fl(cp.realized_pnl)   # OKX net (fees included)
        agg[k]["fee"] += _fl(cp.fee)
        agg[k]["net_usd"] += net_usd
        agg[k]["fee_usd"] += fee_usd

    # end-of-day unrealized (coin, USD) per (subaccount, asset, day) from position snapshots
    eod: dict[tuple, dict[datetime, tuple[float, float]]] = defaultdict(dict)
    for p in s.execute(select(PositionSnapshot)).scalars():
        ccy = _asset_of(p.underlying)
        if p.captured_at is None or ccy is None:
            continue
        k = (p.subaccount_id, ccy, p.captured_at.date())
        cur = eod[k].get(p.captured_at, (0.0, 0.0))
        eod[k][p.captured_at] = (cur[0] + _fl(p.upl), cur[1] + _upl_usd(p, rates))
    eod_level = {k: v[max(v)] for k, v in eod.items()}

    keys = set(agg) | set(eod_level)
    for (sub_id, ccy, d) in keys:
        a = agg.get((sub_id, ccy, d), {})
        net, fees = a.get("net", 0.0), a.get("fee", 0.0)
        net_usd, fees_usd = a.get("net_usd", 0.0), a.get("fee_usd", 0.0)
        upl, upl_usd = eod_level.get((sub_id, ccy, d), (None, None))
        s.add(AssetPnlDaily(
            subaccount_id=sub_id, ccy=ccy, date=d, realized_pnl=net - fees,
            unrealized_pnl=upl, fees=fees, net_pnl=net,
            realized_pnl_usd=net_usd - fees_usd, unrealized_pnl_usd=upl_usd,
            fees_usd=fees_usd, net_pnl_usd=net_usd))
    return len(keys)


def _build_underlying_price(s) -> int:
    # spot/forward per (subaccount, underlying, captured_at) from position snapshots
    rows = s.execute(
        select(PositionSnapshot.subaccount_id, PositionSnapshot.underlying,
               PositionSnapshot.captured_at, func.max(PositionSnapshot.idx_px),
               func.max(PositionSnapshot.fwd_px))
        .where(PositionSnapshot.underlying.isnot(None), PositionSnapshot.idx_px.isnot(None))
        .group_by(PositionSnapshot.subaccount_id, PositionSnapshot.underlying,
                  PositionSnapshot.captured_at)
    ).all()
    for sub_id, uly, cap, idx, fwd in rows:
        s.add(UnderlyingPrice(subaccount_id=sub_id, underlying=uly, captured_at=cap,
                              idx_px=idx, fwd_px=fwd))
    return len(rows)


def _build_expiry_settlement(s) -> int:
    # official settlement price per (subaccount, underlying, expiry) from delivery bills (type '3')
    agg: dict[tuple, tuple] = {}  # key -> (settle_price, settled_at)
    for b in s.execute(select(Bill).where(Bill.bill_type == "3")).scalars():
        if b.underlying is None or b.expiry is None or b.px is None:
            continue
        key = (b.subaccount_id, b.underlying, b.expiry)
        # all legs of an expiry share the same settlement px; keep the latest billed_at
        if key not in agg or (b.billed_at and agg[key][1] and b.billed_at > agg[key][1]):
            agg[key] = (_fl(b.px), b.billed_at)
    for (sub_id, uly, exp), (px, at) in agg.items():
        s.add(ExpirySettlement(subaccount_id=sub_id, underlying=uly, expiry=exp,
                               settle_price=px, settled_at=at))
    return len(agg)


def _build_position_current(s) -> int:
    n = 0
    for p in _latest_positions(s):
        s.add(PositionCurrent(
            subaccount_id=p.subaccount_id, strategy_id=p.strategy_id, inst_id=p.inst_id,
            underlying=p.underlying, opt_type=p.opt_type, strike=p.strike, expiry=p.expiry,
            side=p.side, size=p.size, avg_px=p.avg_px, mark_px=p.mark_px, idx_px=p.idx_px,
            fwd_px=p.fwd_px, upl=p.upl, fee=p.fee,
            premium_usd=(p.notional_usd if p.notional_usd is not None else p.opt_val),
            delta=p.delta_bs, gamma=p.gamma_bs, theta=p.theta_bs, vega=p.vega_bs, iv=p.iv,
            captured_at=p.captured_at,
        ))
        n += 1
    return n


def _build_greeks_by_expiry(s) -> int:
    positions = _latest_positions(s)
    # bucket by (subaccount, strategy_or_None, underlying, expiry); also all-strategy roll-up
    buckets: dict[tuple, dict] = defaultdict(lambda: dict(
        d=0.0, g=0.0, t=0.0, v=0.0, n=0, prem=0.0, cap=None))

    def add(key, p):
        b = buckets[key]
        b["d"] += _fl(p.delta_bs); b["g"] += _fl(p.gamma_bs)
        b["t"] += _fl(p.theta_bs); b["v"] += _fl(p.vega_bs)
        b["n"] += 1
        b["prem"] += _fl(p.notional_usd if p.notional_usd is not None else p.opt_val)
        b["cap"] = p.captured_at

    for p in positions:
        add((p.subaccount_id, p.strategy_id, p.underlying, p.expiry), p)   # per-strategy
        add((p.subaccount_id, None, p.underlying, p.expiry), p)            # all-strategy roll-up
    for (sub_id, strat_id, uly, exp), b in buckets.items():
        s.add(GreeksByExpiry(
            subaccount_id=sub_id, strategy_id=strat_id, underlying=uly, expiry=exp,
            net_delta=b["d"], net_gamma=b["g"], net_theta=b["t"], net_vega=b["v"],
            open_positions=b["n"], premium_usd=b["prem"], captured_at=b["cap"],
        ))
    return len(buckets)


def _build_strategy_summary(s, today: date) -> int:
    positions = _latest_positions(s)
    mtd_start = today.replace(day=1)
    # month-to-date realized per (subaccount, strategy)
    mtd_real: dict[tuple, float] = defaultdict(float)
    for cp in s.execute(select(ClosedPosition)).scalars():
        if cp.closed_at and cp.closed_at.date() >= mtd_start:
            mtd_real[(cp.subaccount_id, cp.strategy_id)] += _fl(cp.realized_pnl)

    agg: dict[tuple, dict] = defaultdict(lambda: dict(n=0, d=0.0, g=0.0, t=0.0, v=0.0, upl=0.0))
    for p in positions:
        a = agg[(p.subaccount_id, p.strategy_id)]
        a["n"] += 1; a["d"] += _fl(p.delta_bs); a["g"] += _fl(p.gamma_bs)
        a["t"] += _fl(p.theta_bs); a["v"] += _fl(p.vega_bs); a["upl"] += _fl(p.upl)

    keys = set(agg) | set(mtd_real)
    for (sub_id, strat_id) in keys:
        a = agg.get((sub_id, strat_id), {})
        s.add(StrategySummary(
            subaccount_id=sub_id, strategy_id=strat_id,
            open_positions=a.get("n", 0), net_delta=a.get("d", 0.0), net_gamma=a.get("g", 0.0),
            net_theta=a.get("t", 0.0), net_vega=a.get("v", 0.0), upl=a.get("upl", 0.0),
            mtd_pnl=mtd_real.get((sub_id, strat_id), 0.0) + a.get("upl", 0.0),
        ))
    return len(keys)


def _build_deal_ledger(s, rates: _CoinUsd, closed_cash: dict[int, LegCash]) -> int:
    n = 0
    for cp in s.execute(select(ClosedPosition)).scalars():
        hold = None
        if cp.opened_at and cp.closed_at:
            hold = (cp.closed_at - cp.opened_at).days
        net_usd, fee_usd, basis = _deal_usd(cp, closed_cash, rates)   # build 25
        s.add(DealLedger(
            subaccount_id=cp.subaccount_id, strategy_id=cp.strategy_id, inst_id=cp.inst_id,
            underlying=cp.underlying, opt_type=cp.opt_type, strike=cp.strike, expiry=cp.expiry,
            side=cp.side, close_type=_close_type(cp.closed_at, cp.expiry), opened_at=cp.opened_at,
            closed_at=cp.closed_at, entry_px=cp.open_avg_px, exit_px=cp.close_avg_px,
            size=cp.size, fee=cp.fee, realized_pnl=cp.realized_pnl,
            realized_pnl_usd=net_usd, fees_usd=fee_usd, usd_basis=basis, hold_days=hold,
        ))
        n += 1
    return n


def _build_position_leg(s, rates: _CoinUsd, sizes: ContractSizes, rows,
                        cash: dict[int, LegCash]) -> tuple[int, list[Leg]]:
    """One row per silver.position_leg (Box builder read model) with USD P&L on the same basis as
    deal_ledger (realized × close-day rate) and pnl_daily (unrealized × last-snapshot-day rate).

    For the price-with-boxes chart each row also gets its line end (`expires_at` = expiry 08:00 UTC)
    and its size in coin (size × ct_val of its exchange / type / underlying from core.contract_size;
    NULL when there is no such row). Returns (row count, the legs as domain objects for
    box_shape)."""
    n = 0
    legs: list[Leg] = []
    # rows = silver.position_leg, cash = their A / B / split (build 21; computed once in run())
    for lg in rows:
        itype = inst_type_of(lg.inst_id)
        ct_val = sizes.ct_val(lg.cex_code, lg.underlying, itype)
        size_coin = (_fl(lg.size) * ct_val) if (ct_val is not None and lg.size is not None) \
            else None
        coin = sizes.coin(lg.cex_code, lg.underlying, itype) if lg.underlying else None
        exp_at = expires_at(lg.expiry)
        r_real = rates.rate(lg.subaccount_id, lg.ccy, lg.closed_at.date()) if lg.closed_at else None
        seen = lg.last_seen_at or lg.pos_opened_at
        r_upl = rates.rate(lg.subaccount_id, lg.ccy or _asset_of(lg.underlying), seen.date()) \
            if seen else 1.0
        realized_usd = (_fl(lg.realized_pnl) * r_real
                        if lg.realized_pnl is not None and r_real is not None else None)
        upl_usd = _fl(lg.upl) * r_upl if lg.upl is not None else None
        s.add(GoldPositionLeg(
            position_leg_id=lg.id, subaccount_id=lg.subaccount_id, strategy_id=lg.strategy_id,
            strategy_source=lg.strategy_source, rule_strategy_id=lg.rule_strategy_id,
            pos_id=lg.pos_id, pos_opened_at=lg.pos_opened_at, inst_id=lg.inst_id,
            underlying=lg.underlying, opt_type=lg.opt_type, strike=lg.strike, expiry=lg.expiry,
            side=lg.side, status=lg.status, size=lg.size, entry_px=lg.entry_px,
            exit_px=lg.exit_px, closed_at=lg.closed_at,
            close_type=_close_type(lg.closed_at, lg.expiry) if lg.close_type is not None else None,
            last_seen_at=lg.last_seen_at, realized_pnl=lg.realized_pnl,
            realized_pnl_usd=realized_usd, upl=lg.upl, upl_usd=upl_usd,
            fee=lg.fee, ccy=lg.ccy, n_fills=lg.n_fills, n_snapshots=lg.n_snapshots,
            expires_at=exp_at, ct_val=ct_val, size_coin=size_coin, coin=coin,
            open_cash_usd=cash[lg.id].open_usd if lg.id in cash else None,
            realized_cash_usd=cash[lg.id].realized_usd if lg.id in cash else None,
            cash_source=cash[lg.id].source if lg.id in cash else None,
            cash_missing_px=cash[lg.id].missing_px if lg.id in cash else None,
            premium_net_coin=cash[lg.id].premium_coin if lg.id in cash else None,   # build 23
            premium_net_usd=cash[lg.id].premium_usd if lg.id in cash else None,
            close_net_coin=cash[lg.id].close_coin if lg.id in cash else None,       # build 24
            close_net_usd=cash[lg.id].close_usd if lg.id in cash else None,
        ))
        legs.append(Leg(
            leg_id=lg.id, subaccount_id=lg.subaccount_id, strategy_id=lg.strategy_id,
            underlying=lg.underlying, strike=float(lg.strike) if lg.strike is not None else None,
            opened_at=lg.pos_opened_at, expires_at=exp_at, status=lg.status,
            closed_at=lg.closed_at, last_seen_at=lg.last_seen_at,
            size=float(lg.size) if lg.size is not None else None, size_coin=size_coin, coin=coin,
            pnl_usd=realized_usd if realized_usd is not None else upl_usd,
        ))
        n += 1
    return n, legs


def _opt(v) -> float | None:
    return float(v) if v is not None else None


def _leg_cash_all(s, rows, rates: _CoinUsd, sizes: ContractSizes) -> dict[int, LegCash]:
    """build 21 — per leg A (premium + fees of the open quantity) and B (realized) in USD at
    operation-time prices (app.domain.leg_cash): trades from silver.trade_fill (their fillIdxPx),
    position snapshots replayed for open legs whose trades are not all there, positions-history
    otherwise. Other prices: the middle of the 1-minute index candle of that minute
    (silver.index_candle), else the day's coin rate; expiries at the official settlement price."""
    fills: dict[int, list[Fill]] = defaultdict(list)
    for f in s.execute(select(TradeFill).where(TradeFill.position_leg_id.isnot(None))).scalars():
        if f.filled_at is None or f.size is None or f.price is None:
            continue
        fills[f.position_leg_id].append(Fill(
            at=f.filled_at, side=f.side or "", size=float(f.size), price=float(f.price),
            fee=_fl(f.fee), fee_ccy=f.fee_ccy, idx_px=_opt(f.idx_px)))
    s.flush()                                            # this run's expiry_settlement rows
    settle = {(e.subaccount_id, e.underlying, e.expiry): _opt(e.settle_price)
              for e in s.execute(select(ExpirySettlement)).scalars()}

    def info(lg, snaps=()) -> LegInfo:
        return LegInfo(status=lg.status, side=lg.side, opened_at=lg.pos_opened_at,
                       closed_at=lg.closed_at,
                       close_type=_close_type(lg.closed_at, lg.expiry) if lg.close_type is not None else None,
                       size=_opt(lg.size), entry_px=_opt(lg.entry_px), fee=_opt(lg.fee),
                       realized_pnl=_opt(lg.realized_pnl), fills=fills.get(lg.id, ()), snaps=snaps)

    def ct(lg):
        return sizes.ct_val(lg.cex_code, lg.underlying, inst_type_of(lg.inst_id))

    # open legs whose trades don't account for the position → replay their snapshots (change
    # points only: the rows where size, average price, fee or PnL moved)
    need = [lg.id for lg in rows if lg.status == "open" and ct(lg) is not None
            and not fills_cover(fill_events(fills.get(lg.id, ()), lg.side, ct(lg), lambda _a: None),
                                info(lg), _fl(lg.size) * ct(lg))]
    snaps: dict[int, list[Snap]] = defaultdict(list)
    if need:
        for r in s.execute(text("""
            SELECT position_leg_id, captured_at, abs(size) AS size, avg_px, fee, pnl, realized_pnl,
                   idx_px
              FROM (SELECT p.*, row_number() OVER w AS rn,
                           lag(size) OVER w AS l_size, lag(avg_px) OVER w AS l_avg,
                           lag(fee) OVER w AS l_fee, lag(pnl) OVER w AS l_pnl,
                           lag(realized_pnl) OVER w AS l_real
                      FROM silver.position_snapshot p
                     WHERE p.position_leg_id = ANY(:ids)
                    WINDOW w AS (PARTITION BY p.position_leg_id ORDER BY p.captured_at)) x
             WHERE rn = 1 OR size IS DISTINCT FROM l_size OR avg_px IS DISTINCT FROM l_avg
                OR fee IS DISTINCT FROM l_fee OR pnl IS DISTINCT FROM l_pnl
                OR realized_pnl IS DISTINCT FROM l_real
             ORDER BY position_leg_id, captured_at"""), {"ids": need}):
            snaps[r.position_leg_id].append(Snap(
                at=r.captured_at, size=_fl(r.size), avg_px=_opt(r.avg_px), fee=_opt(r.fee),
                pnl=_opt(r.pnl), realized_pnl=_opt(r.realized_pnl), idx_px=_opt(r.idx_px)))

    def minute(at: datetime) -> datetime:
        return at.replace(second=0, microsecond=0)

    def compute(px_of) -> dict[int, LegCash]:
        out = {}
        for lg in rows:
            c = ct(lg)
            if c is None:
                continue
            coin = lg.ccy or _asset_of(lg.underlying)

            def px_at(at, lg=lg, coin=coin):
                return px_of(lg, coin, at) if at is not None else None
            sp = settle.get((lg.subaccount_id, lg.underlying, lg.expiry))
            r = leg_cash(info(lg, snaps.get(lg.id, ())), c, _fl(lg.size) * c, px_at, sp)
            if r is not None:
                out[lg.id] = r
        return out

    # pass 1 records which minutes need a candle; one query fetches them; pass 2 prices
    wanted: set[tuple[str, str, datetime]] = set()
    compute(lambda lg, coin, at: wanted.add((lg.cex_code, lg.underlying, minute(at))))
    mids: dict[tuple[str, str, datetime], float] = {}
    if wanted:
        for r in s.execute(text("""
            SELECT cex_code, inst_id, ts, (high + low) / 2 AS mid FROM silver.index_candle
             WHERE bar = '1m' AND ts = ANY(:ts)"""), {"ts": sorted({w[2] for w in wanted})}):
            mids[(r.cex_code, r.inst_id, r.ts)] = float(r.mid)

    def priced(lg, coin, at):
        m = mids.get((lg.cex_code, lg.underlying, minute(at)))
        if m is not None:
            return m
        # no candle for that minute: the account's coin rate of the nearest day, if it has any
        # (never the 1.0 the rate table falls back to for unknown coins)
        return rates.rate(lg.subaccount_id, coin, at.date()) \
            if (lg.subaccount_id, coin) in rates._by_cc else None
    return compute(priced)


def _build_box_shape(s, legs: list[Leg], now: datetime) -> int:
    """One rectangle per (subaccount, strategy, underlying) — see app.domain.boxes.build_boxes."""
    boxes = build_boxes(legs)
    for b in boxes:
        s.add(BoxShape(
            subaccount_id=b.subaccount_id, strategy_id=b.strategy_id, underlying=b.underlying,
            coin=b.coin, started_at=b.started_at, ends_at=b.ends_at, strike_lo=b.strike_lo,
            strike_hi=b.strike_hi, n_legs=b.n_legs, n_open_legs=b.n_open_legs,
            size_contracts=b.size_contracts, size_coin=b.size_coin, status=b.status,
            net_pnl_usd=b.net_pnl_usd, leg_ids=b.leg_ids, updated_at=now,
        ))
    return len(boxes)


_PG_INTERVAL = {"1h": "1 hour", "4h": "4 hours", "1d": "1 day", "1do": "1 day"}
assert set(_PG_INTERVAL) == set(BARS) == set(BAR_STEP) == set(BAR_OFFSET)
_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)   # date_bin origin + BAR_OFFSET per size


def _build_index_candles(s) -> int:
    """silver.index_candle (1m) → gold.index_candle (1h / 4h / 1d / 1do), incrementally.

    Per bar size: watermark = max(src_max_id) already in gold; the bars ("buckets", UTC
    boundaries via date_bin) that contain a silver minute with a higher id are recomputed in full
    from silver and upserted. A first run (empty gold) builds everything — also for a size added
    later (build 14: 1do, the option day 08:00 → 07:59 UTC: origin 08:00). Returns bars written."""
    total = 0
    for bar in BARS:
        total += s.execute(text("""
            WITH wm AS (
                SELECT COALESCE(MAX(src_max_id), 0) AS id FROM gold.index_candle WHERE bar = :bar
            ), touched AS (
                SELECT DISTINCT c.cex_code, c.inst_id,
                       date_bin(CAST(:step AS interval), c.ts, CAST(:origin AS timestamptz)) AS b
                  FROM silver.index_candle c, wm
                 WHERE c.id > wm.id AND c.bar = '1m'
            ), agg AS (
                -- LATERAL: one index range scan per touched bar (a plain join makes the planner
                -- match every minute of the instrument against every bar)
                SELECT t.cex_code, t.inst_id, t.b AS ts, x.*
                  FROM touched t
                 CROSS JOIN LATERAL (
                       SELECT (array_agg(c.open ORDER BY c.ts))[1] AS open,
                              max(c.high) AS high, min(c.low) AS low,
                              (array_agg(c.close ORDER BY c.ts DESC))[1] AS close,
                              count(*) AS n_minutes, max(c.id) AS src_max_id
                         FROM silver.index_candle c
                        WHERE c.cex_code = t.cex_code AND c.inst_id = t.inst_id AND c.bar = '1m'
                          AND c.ts >= t.b AND c.ts < t.b + CAST(:step AS interval)) x
            )
            INSERT INTO gold.index_candle
                   (cex_code, inst_id, bar, ts, open, high, low, close, n_minutes, src_max_id,
                    updated_at)
            SELECT cex_code, inst_id, :bar, ts, open, high, low, close, n_minutes, src_max_id, now()
              FROM agg
            ON CONFLICT ON CONSTRAINT uq_gold_index_candle DO UPDATE
               SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                   close = EXCLUDED.close, n_minutes = EXCLUDED.n_minutes,
                   src_max_id = EXCLUDED.src_max_id, updated_at = EXCLUDED.updated_at
        """), {"bar": bar, "step": _PG_INTERVAL[bar], "origin": _ORIGIN + BAR_OFFSET[bar]}).rowcount
    return total


def _build_pnl_daily(s, rates: _CoinUsd, closed_cash: dict[int, LegCash]) -> int:
    # OKX realizedPnl already NETS fees (= pnl + fee + funding), so it IS net_pnl; gross realized
    # is derived as net - fees (fee is negative), fees shown separately. USD amounts convert each
    # closed position's coin PnL by that coin's rate at close (a strategy may mix settlement coins).
    agg: dict[tuple, dict] = defaultdict(
        lambda: dict(net=0.0, fee=0.0, net_usd=0.0, fee_usd=0.0))
    for cp in s.execute(select(ClosedPosition)).scalars():
        if not cp.closed_at:
            continue
        d = cp.closed_at.date()
        k = (cp.subaccount_id, cp.strategy_id, d)
        net_usd, fee_usd, _basis = _deal_usd(cp, closed_cash, rates)   # build 25: as graph ①
        agg[k]["net"] += _fl(cp.realized_pnl)   # OKX net (fees included)
        agg[k]["fee"] += _fl(cp.fee)
        agg[k]["net_usd"] += net_usd
        agg[k]["fee_usd"] += fee_usd

    # end-of-day unrealized level per (subaccount, strategy, day) from position snapshots (coin+USD)
    eod: dict[tuple, dict[datetime, tuple[float, float]]] = defaultdict(dict)
    for p in s.execute(select(PositionSnapshot)).scalars():
        if p.captured_at is None:
            continue
        d = p.captured_at.date()
        k = (p.subaccount_id, p.strategy_id, d)
        cur = eod[k].get(p.captured_at, (0.0, 0.0))
        eod[k][p.captured_at] = (cur[0] + _fl(p.upl), cur[1] + _upl_usd(p, rates))   # build 25
    eod_level = {k: v[max(v)] for k, v in eod.items()}  # upl at the latest snapshot of the day

    keys = set(agg) | set(eod_level)
    for (sub_id, strat_id, d) in keys:
        a = agg.get((sub_id, strat_id, d), {})
        net, fees = a.get("net", 0.0), a.get("fee", 0.0)
        net_usd, fees_usd = a.get("net_usd", 0.0), a.get("fee_usd", 0.0)
        upl_coin, upl_usd = eod_level.get((sub_id, strat_id, d), (None, None))
        s.add(PnlDaily(
            subaccount_id=sub_id, strategy_id=strat_id, date=d,
            realized_pnl=net - fees, unrealized_pnl=upl_coin, fees=fees, net_pnl=net,
            realized_pnl_usd=net_usd - fees_usd, unrealized_pnl_usd=upl_usd,
            fees_usd=fees_usd, net_pnl_usd=net_usd,
        ))
    return len(keys)


def _eod_equity(s) -> dict[tuple[int, date], float]:
    """End-of-day USD equity per (subaccount_id, day) = equity at the last snapshot that day."""
    latest: dict[tuple[int, date], tuple[datetime, float]] = {}
    for bt in s.execute(select(BalanceTimeseries)).scalars():
        if bt.captured_at is None:
            continue
        key = (bt.subaccount_id, bt.captured_at.date())
        cur = latest.get(key)
        if cur is None or bt.captured_at > cur[0]:
            latest[key] = (bt.captured_at, _fl(bt.equity_usd))
    return {k: v[1] for k, v in latest.items()}


def _build_client_pnl_daily(s, sub_user: dict[int, int], eod: dict[tuple[int, date], float]) -> int:
    # roll subaccount pnl_daily up to the owning user, per day
    agg: dict[tuple, dict] = defaultdict(lambda: dict(real=0.0, fee=0.0, net=0.0, eq=0.0, upl=0.0))
    for r in s.execute(select(PnlDaily)).scalars():
        u = sub_user.get(r.subaccount_id)
        if u is None:
            continue
        a = agg[(u, r.date)]
        a["real"] += _fl(r.realized_pnl); a["fee"] += _fl(r.fees); a["net"] += _fl(r.net_pnl)
        a["upl"] += _fl(r.unrealized_pnl)
    # equity = sum of end-of-day equity across the user's subaccounts (no intraday double count)
    for (sub_id, d), equity in eod.items():
        u = sub_user.get(sub_id)
        if u is not None:
            agg[(u, d)]["eq"] += equity

    for (u, d), a in agg.items():
        s.add(ClientPnlDaily(
            user_id=u, date=d, equity_usd=a["eq"] or None, realized_pnl=a["real"],
            unrealized_pnl=a["upl"], fees=a["fee"], net_pnl=a["net"],
        ))
    return len(agg)


def _equity_series_for(eod: dict[tuple[int, date], float], subaccount_ids: set[int],
                       start: date) -> list[float]:
    """Ordered daily USD equity across the given subaccounts (EOD per sub, summed), since `start`."""
    per_day: dict[date, float] = defaultdict(float)
    for (sub_id, d), equity in eod.items():
        if sub_id in subaccount_ids and d >= start:
            per_day[d] += equity
    return [per_day[d] for d in sorted(per_day)]


def _build_performance(s, sub_user, today, now, eod) -> dict[str, int]:
    periods = ["mtd", "ytd", "all"]
    deals = s.execute(select(ClosedPosition)).scalars().all()
    user_subs: dict[int, set[int]] = defaultdict(set)
    for sub_id, u in sub_user.items():
        user_subs[u].add(sub_id)

    n_client = n_strategy = n_symbol = 0
    for period in periods:
        start = _period_start(period, today)
        in_period = [d for d in deals if d.closed_at and d.closed_at.date() >= start]

        # --- client_performance ---
        by_user: dict[int, list[float]] = defaultdict(list)
        for d in in_period:
            u = sub_user.get(d.subaccount_id)
            if u is not None:
                by_user[u].append(_fl(d.realized_pnl))
        for u in user_subs:
            dm = deal_metrics(by_user.get(u, []))
            em = equity_metrics(_equity_series_for(eod, user_subs[u], start))
            s.add(ClientPerformance(
                user_id=u, period=period, net_pnl=dm.net_pnl, return_pct=em.return_pct,
                max_drawdown_pct=em.max_drawdown_pct, sharpe=em.sharpe, win_rate=dm.win_rate,
                profit_factor=dm.profit_factor, avg_win=dm.avg_win, avg_loss=dm.avg_loss,
                avg_pnl_per_deal=dm.avg_pnl_per_deal, n_deals=dm.n_deals, updated_at=now,
            ))
            n_client += 1

        # --- strategy_performance ---
        by_strat: dict[tuple, list[float]] = defaultdict(list)
        for d in in_period:
            by_strat[(d.subaccount_id, d.strategy_id)].append(_fl(d.realized_pnl))
        for (sub_id, strat_id), pnls in by_strat.items():
            dm = deal_metrics(pnls)
            em = equity_metrics(_equity_series_for(eod, {sub_id}, start))
            s.add(StrategyPerformance(
                user_id=sub_user.get(sub_id), subaccount_id=sub_id, strategy_id=strat_id,
                period=period, net_pnl=dm.net_pnl, return_pct=em.return_pct,
                max_drawdown_pct=em.max_drawdown_pct, win_rate=dm.win_rate,
                profit_factor=dm.profit_factor, avg_win=dm.avg_win, avg_loss=dm.avg_loss,
                avg_pnl_per_deal=dm.avg_pnl_per_deal, n_deals=dm.n_deals,
            ))
            n_strategy += 1

        # --- symbol_performance ---
        by_symbol: dict[tuple, list[float]] = defaultdict(list)
        for d in in_period:
            by_symbol[(d.subaccount_id, d.underlying)].append(_fl(d.realized_pnl))
        for (sub_id, uly), pnls in by_symbol.items():
            dm = deal_metrics(pnls)
            em = equity_metrics(_equity_series_for(eod, {sub_id}, start))
            s.add(SymbolPerformance(
                user_id=sub_user.get(sub_id), subaccount_id=sub_id, underlying=uly, period=period,
                net_pnl=dm.net_pnl, return_pct=em.return_pct, win_rate=dm.win_rate,
                profit_factor=dm.profit_factor, n_deals=dm.n_deals, updated_at=now,
            ))
            n_symbol += 1

    return {"client_performance": n_client, "strategy_performance": n_strategy,
            "symbol_performance": n_symbol}
