"""Silver -> Gold transform.

Gold is fully derived and **rebuilt** each run (truncate + insert) — simplest and always
consistent at this scale. Reads only silver + core; never touches bronze.

Definitions:
  * A "deal" = one silver.closed_position (realized, incl. expiry). realized_pnl is in the account
    settlement currency (coin) as OKX reports it.
  * "current" = rows from the latest snapshot per subaccount.
  * Periods: mtd = month-to-date, ytd = year-to-date, all = everything (UTC).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text

from app.db.base import session_scope
from app.db.models_core import CexAccount, Subaccount
from app.db.models_gold import (
    BalanceTimeseries,
    ClientPerformance,
    ClientPnlDaily,
    DealLedger,
    GreeksByExpiry,
    PnlDaily,
    PositionCurrent,
    StrategyPerformance,
    StrategySummary,
    SymbolPerformance,
)
from app.db.models_silver import (
    BalanceSnapshot,
    ClosedPosition,
    PositionSnapshot,
)
from app.domain.metrics import deal_metrics, equity_metrics

logger = logging.getLogger(__name__)

_GOLD_TABLES = [  # truncated in this order at the start of each rebuild
    "balance_timeseries", "pnl_daily", "strategy_summary", "deal_ledger", "position_current",
    "greeks_by_expiry", "client_pnl_daily", "client_performance", "strategy_performance",
    "symbol_performance",
]


def _fl(v) -> float:
    return float(v) if v is not None else 0.0


def _period_start(period: str, today: date) -> date:
    if period == "mtd":
        return today.replace(day=1)
    if period == "ytd":
        return today.replace(month=1, day=1)
    return date(1970, 1, 1)  # 'all'


def _close_type(raw_type: str | None) -> str:
    # OKX positions-history 'type': 3 = delivery/exercise (expiry); others = traded close.
    return "expiry" if str(raw_type) == "3" else "close"


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

        # Truncate gold (rebuild). RESTART IDENTITY resets the surrogate ids.
        s.execute(text("TRUNCATE " + ", ".join(f"gold.{t}" for t in _GOLD_TABLES)
                       + " RESTART IDENTITY"))

        counts["balance_timeseries"] = _build_balance_ts(s)
        eod = _eod_equity(s)  # end-of-day USD equity per (subaccount, day) — used by rollups
        counts["position_current"] = _build_position_current(s)
        counts["greeks_by_expiry"] = _build_greeks_by_expiry(s)
        counts["strategy_summary"] = _build_strategy_summary(s, today)
        counts["deal_ledger"] = _build_deal_ledger(s)
        counts["pnl_daily"] = _build_pnl_daily(s)
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


def _build_position_current(s) -> int:
    n = 0
    for p in _latest_positions(s):
        s.add(PositionCurrent(
            subaccount_id=p.subaccount_id, strategy_id=p.strategy_id, inst_id=p.inst_id,
            underlying=p.underlying, opt_type=p.opt_type, strike=p.strike, expiry=p.expiry,
            side=p.side, size=p.size, avg_px=p.avg_px, mark_px=p.mark_px, idx_px=p.idx_px,
            fwd_px=p.fwd_px, upl=p.upl, premium_usd=(p.notional_usd if p.notional_usd is not None
                                                     else p.opt_val),
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


def _build_deal_ledger(s) -> int:
    n = 0
    for cp in s.execute(select(ClosedPosition)).scalars():
        hold = None
        if cp.opened_at and cp.closed_at:
            hold = (cp.closed_at - cp.opened_at).days
        s.add(DealLedger(
            subaccount_id=cp.subaccount_id, strategy_id=cp.strategy_id, inst_id=cp.inst_id,
            underlying=cp.underlying, opt_type=cp.opt_type, strike=cp.strike, expiry=cp.expiry,
            side=cp.side, close_type=_close_type(cp.close_type), opened_at=cp.opened_at,
            closed_at=cp.closed_at, entry_px=cp.open_avg_px, exit_px=cp.close_avg_px,
            size=cp.size, fee=cp.fee, realized_pnl=cp.realized_pnl, hold_days=hold,
        ))
        n += 1
    return n


def _build_pnl_daily(s) -> int:
    # realized + fees per (subaccount, strategy, day) from closed positions
    agg: dict[tuple, dict] = defaultdict(lambda: dict(real=0.0, fee=0.0))
    for cp in s.execute(select(ClosedPosition)).scalars():
        if not cp.closed_at:
            continue
        k = (cp.subaccount_id, cp.strategy_id, cp.closed_at.date())
        agg[k]["real"] += _fl(cp.realized_pnl)
        agg[k]["fee"] += _fl(cp.fee)

    # end-of-day unrealized level per (subaccount, strategy, day) from position snapshots
    eod: dict[tuple, dict[tuple, float]] = defaultdict(dict)  # (sub,strat,day) -> {captured_at: upl}
    for p in s.execute(select(PositionSnapshot)).scalars():
        if p.captured_at is None:
            continue
        d = p.captured_at.date()
        k = (p.subaccount_id, p.strategy_id, d)
        eod[k][p.captured_at] = eod[k].get(p.captured_at, 0.0) + _fl(p.upl)
    eod_level = {k: v[max(v)] for k, v in eod.items()}  # upl at the latest snapshot of the day

    keys = set(agg) | set(eod_level)
    for (sub_id, strat_id, d) in keys:
        a = agg.get((sub_id, strat_id, d), {})
        realized = a.get("real", 0.0)
        fees = a.get("fee", 0.0)
        s.add(PnlDaily(
            subaccount_id=sub_id, strategy_id=strat_id, date=d,
            realized_pnl=realized, unrealized_pnl=eod_level.get((sub_id, strat_id, d)),
            fees=fees, net_pnl=realized - fees,
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
