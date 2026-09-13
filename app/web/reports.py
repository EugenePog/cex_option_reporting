"""JSON report endpoints backing the six dashboard reports.

All queries are scoped to the current user's subaccounts (admin => all). See
REPORT_GOLD_ATTRIBUTE_MAPPING.md for the column→visual mapping this implements.
Reports ①–⑤ read current/aggregate gold; the Analyze tab ⑥ recomputes from gold.deal_ledger so it
responds live to filters (the precomputed perf tables are used for fixed-period admin views).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models_core import Strategy
from app.db.models_gold import (
    AssetBalanceTimeseries,
    AssetPnlDaily,
    BalanceTimeseries,
    DealLedger,
    GreeksByExpiry,
    PnlDaily,
    ExpirySettlement,
    PositionCurrent,
    UnderlyingPrice,
)
from app.domain.metrics import deal_metrics, equity_metrics
from app.domain.pricing import black76_price, contract_size, payoff_intrinsic

# OKX crypto options settle at 08:00 UTC on the expiry date.
OKX_SETTLE_UTC = time(8, 0)
from app.web.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["reports"])


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def _subs(user: CurrentUser, subaccount: int | None) -> list[int]:
    """Effective subaccount scope: the user's subs, optionally narrowed to one they own."""
    if subaccount is not None and subaccount in user.subaccount_ids:
        return [subaccount]
    return user.subaccount_ids


def _date_bounds(frm: str | None, till: str | None):
    """Parse 'YYYY-MM-DD' from/till into (from_date, till_date, from_dt@00:00, till_dt@23:59:59) UTC.

    'from' includes the whole day from 00:00; 'till' includes the whole day up to 23:59:59.
    """
    def _d(x):
        try:
            return date.fromisoformat(x) if x else None
        except ValueError:
            return None
    fd, td = _d(frm), _d(till)
    lo = datetime.combine(fd, time.min, tzinfo=timezone.utc) if fd else None
    hi = datetime.combine(td, time.max, tzinfo=timezone.utc) if td else None
    return fd, td, lo, hi


def _period_start(period: str, today: date) -> date | None:
    if period == "mtd":
        return today.replace(day=1)
    if period == "ytd":
        return today.replace(month=1, day=1)
    if period.endswith("d") and period[:-1].isdigit():
        from datetime import timedelta
        return today - timedelta(days=int(period[:-1]))
    return None  # 'all'


# --------------------------------------------------------------------------- #
@router.get("/filters")
def filters(user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = user.subaccount_ids
    with SessionLocal() as s:
        underlyings = sorted({u for (u,) in s.execute(
            select(PositionCurrent.underlying).where(PositionCurrent.subaccount_id.in_(subs))
        ) if u} | {u for (u,) in s.execute(
            select(DealLedger.underlying).where(DealLedger.subaccount_id.in_(subs))
        ) if u})
        strategies = [
            {"id": sid, "name": name, "color": color}
            for sid, name, color in s.execute(
                select(Strategy.id, Strategy.name, Strategy.color)
                .where(Strategy.subaccount_id.in_(subs)).order_by(Strategy.name)
            )
        ]
        assets = sorted({c for (c,) in s.execute(
            select(AssetBalanceTimeseries.ccy)
            .where(AssetBalanceTimeseries.subaccount_id.in_(subs))
        ) if c})
    return {"underlyings": underlyings, "strategies": strategies, "assets": assets,
            "subaccounts": subs, "is_admin": user.is_admin,
            "periods": ["mtd", "ytd", "all", "7d", "30d", "90d"]}


# ①b Per-asset (in-kind) equity & daily net P&L --------------------------- #
@router.get("/asset-equity")
def asset_equity(ccy: str | None = None, subaccount: int | None = None,
                 frm: str | None = None, till: str | None = None,
                 user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    fd, td, lo, hi = _date_bounds(frm, till)
    with SessionLocal() as s:
        if not ccy:  # default to the asset with the largest current balance
            latest = s.execute(
                select(AssetBalanceTimeseries.ccy, func.max(AssetBalanceTimeseries.amount))
                .where(AssetBalanceTimeseries.subaccount_id.in_(subs))
                .group_by(AssetBalanceTimeseries.ccy)
                .order_by(func.max(AssetBalanceTimeseries.amount).desc())
            ).first()
            ccy = latest[0] if latest else None
        bal_rows, pnl_rows = [], []
        if ccy:
            bq = select(AssetBalanceTimeseries.captured_at, func.sum(AssetBalanceTimeseries.amount)) \
                .where(AssetBalanceTimeseries.subaccount_id.in_(subs), AssetBalanceTimeseries.ccy == ccy)
            if lo is not None:
                bq = bq.where(AssetBalanceTimeseries.captured_at >= lo)
            if hi is not None:
                bq = bq.where(AssetBalanceTimeseries.captured_at <= hi)
            bal_rows = s.execute(bq.group_by(AssetBalanceTimeseries.captured_at)
                                 .order_by(AssetBalanceTimeseries.captured_at)).all()
            pq = select(AssetPnlDaily.date, func.sum(AssetPnlDaily.net_pnl),
                        func.sum(AssetPnlDaily.realized_pnl), func.sum(AssetPnlDaily.unrealized_pnl),
                        func.sum(AssetPnlDaily.fees)) \
                .where(AssetPnlDaily.subaccount_id.in_(subs), AssetPnlDaily.ccy == ccy)
            if fd is not None:
                pq = pq.where(AssetPnlDaily.date >= fd)
            if td is not None:
                pq = pq.where(AssetPnlDaily.date <= td)
            pnl_rows = s.execute(pq.group_by(AssetPnlDaily.date).order_by(AssetPnlDaily.date)).all()
    balance = [{"t": t.isoformat(), "v": _f(v)} for t, v in bal_rows]
    pnl = [{"date": d.isoformat(), "net": _f(n), "realized": _f(r), "unrealized": _f(u),
            "fees": _f(fee)} for d, n, r, u, fee in pnl_rows]
    header = {}
    if balance:
        first, last = balance[0]["v"], balance[-1]["v"]
        header = {"delta": last - first, "pct": (last / first - 1) * 100 if first else None,
                  "since": balance[0]["t"][:10]}
    return {"ccy": ccy, "balance": balance, "pnl": pnl, "header": header}


# ① Equity curve & daily net P&L ------------------------------------------- #
@router.get("/equity")
def equity(subaccount: int | None = None, frm: str | None = None, till: str | None = None,
           user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    fd, td, lo, hi = _date_bounds(frm, till)
    with SessionLocal() as s:
        eq_q = select(BalanceTimeseries.captured_at, func.sum(BalanceTimeseries.equity_usd)) \
            .where(BalanceTimeseries.subaccount_id.in_(subs))
        if lo is not None:
            eq_q = eq_q.where(BalanceTimeseries.captured_at >= lo)
        if hi is not None:
            eq_q = eq_q.where(BalanceTimeseries.captured_at <= hi)
        eq_rows = s.execute(eq_q.group_by(BalanceTimeseries.captured_at)
                            .order_by(BalanceTimeseries.captured_at)).all()

        # ①a is a USD report → return USD (fee-inclusive net); coin kept for reference/back-compat.
        pnl_q = select(PnlDaily.date,
                       func.sum(PnlDaily.net_pnl_usd), func.sum(PnlDaily.realized_pnl_usd),
                       func.sum(PnlDaily.unrealized_pnl_usd), func.sum(PnlDaily.fees_usd),
                       func.sum(PnlDaily.net_pnl)) \
            .where(PnlDaily.subaccount_id.in_(subs))
        if fd is not None:
            pnl_q = pnl_q.where(PnlDaily.date >= fd)
        if td is not None:
            pnl_q = pnl_q.where(PnlDaily.date <= td)
        pnl_rows = s.execute(pnl_q.group_by(PnlDaily.date).order_by(PnlDaily.date)).all()
    equity_series = [{"t": t.isoformat(), "v": _f(v)} for t, v in eq_rows]
    pnl = [{"date": d.isoformat(), "net": _f(n), "realized": _f(r),
            "unrealized": _f(u), "fees": _f(fee), "net_coin": _f(nc)}
           for d, n, r, u, fee, nc in pnl_rows]
    header = {}
    if equity_series:
        first, last = equity_series[0]["v"], equity_series[-1]["v"]
        header = {"delta": last - first, "pct": (last / first - 1) * 100 if first else None,
                  "since": equity_series[0]["t"][:10]}
    return {"equity": equity_series, "pnl": pnl, "header": header}


# ② Strike × expiry position map ------------------------------------------- #
@router.get("/position-map")
def position_map(underlying: str | None = None, subaccount: int | None = None,
                 user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        q = select(PositionCurrent).where(PositionCurrent.subaccount_id.in_(subs))
        if underlying:
            q = q.where(PositionCurrent.underlying == underlying)
        rows = s.execute(q).scalars().all()
    net: dict[tuple, float] = defaultdict(float)
    strikes, expiries, spot, captured = set(), set(), None, None
    for p in rows:
        if p.strike is None or p.expiry is None:
            continue
        net[(float(p.strike), p.expiry.isoformat())] += _f(p.size)  # size is signed (+long/−short)
        strikes.add(float(p.strike)); expiries.add(p.expiry.isoformat())
        if p.idx_px is not None:
            spot = _f(p.idx_px)
        captured = p.captured_at.isoformat() if p.captured_at else captured
    cells = [{"strike": k, "expiry": e, "net": round(v, 4)} for (k, e), v in net.items()]
    return {"strikes": sorted(strikes), "expiries": sorted(expiries), "cells": cells,
            "spot": spot, "as_of": captured}


# ③ Greeks term structure -------------------------------------------------- #
@router.get("/greeks")
def greeks(underlying: str | None = None, subaccount: int | None = None,
           user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        q = (select(GreeksByExpiry.expiry,
                    func.sum(GreeksByExpiry.net_vega), func.sum(GreeksByExpiry.net_theta),
                    func.sum(GreeksByExpiry.net_delta), func.sum(GreeksByExpiry.net_gamma),
                    func.sum(GreeksByExpiry.open_positions), func.sum(GreeksByExpiry.premium_usd))
             .where(GreeksByExpiry.subaccount_id.in_(subs),
                    GreeksByExpiry.strategy_id.is_(None))          # all-strategy roll-up rows
             .group_by(GreeksByExpiry.expiry).order_by(GreeksByExpiry.expiry))
        if underlying:
            q = q.where(GreeksByExpiry.underlying == underlying)
        rows = s.execute(q).all()
    out = {"expiries": [], "vega": [], "theta": [], "delta": [], "gamma": [],
           "open_positions": [], "premium_usd": []}
    for exp, v, t, d, g, n, prem in rows:
        if exp is None:
            continue
        out["expiries"].append(exp.isoformat())
        out["vega"].append(_f(v)); out["theta"].append(_f(t))
        out["delta"].append(_f(d)); out["gamma"].append(_f(g))
        out["open_positions"].append(int(n or 0)); out["premium_usd"].append(_f(prem))
    out["tiles"] = {"delta": sum(out["delta"]), "gamma": sum(out["gamma"]),
                    "theta": sum(out["theta"]), "vega": sum(out["vega"])}
    return out


# ⑤ Maturity ladder -------------------------------------------------------- #
@router.get("/ladder")
def ladder(underlying: str | None = None, subaccount: int | None = None,
           user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        q = select(PositionCurrent.expiry, PositionCurrent.opt_type,
                   func.sum(func.abs(PositionCurrent.premium_usd))).where(
            PositionCurrent.subaccount_id.in_(subs))
        if underlying:
            q = q.where(PositionCurrent.underlying == underlying)
        q = q.group_by(PositionCurrent.expiry, PositionCurrent.opt_type).order_by(
            PositionCurrent.expiry)
        rows = s.execute(q).all()
    calls: dict[str, float] = defaultdict(float)
    puts: dict[str, float] = defaultdict(float)
    for exp, opt, prem in rows:
        if exp is None:
            continue
        (calls if (opt or "").upper() == "C" else puts)[exp.isoformat()] += _f(prem)
    expiries = sorted(set(calls) | set(puts))
    return {"expiries": expiries,
            "calls": [round(calls.get(e, 0), 2) for e in expiries],
            "puts": [round(puts.get(e, 0), 2) for e in expiries]}


# ② Payoff / risk profile -------------------------------------------------- #
def _settlement_price(s, subs, underlying, chosen) -> float | None:
    """Expiration price: prefer the OKX official settlement (gold.expiry_settlement, from delivery
    bills); fall back to the nearest gold.underlying_price snapshot on/before the expiry day."""
    eq = select(ExpirySettlement.settle_price).where(
        ExpirySettlement.subaccount_id.in_(subs), ExpirySettlement.expiry == chosen,
        ExpirySettlement.settle_price.isnot(None))
    if underlying:
        eq = eq.where(ExpirySettlement.underlying == underlying)
    row = s.execute(eq.limit(1)).first()
    if row:
        return _f(row[0])
    # fallback: snapshot proxy
    hi = datetime.combine(chosen, time.max, tzinfo=timezone.utc)
    q = (select(UnderlyingPrice.idx_px)
         .where(UnderlyingPrice.subaccount_id.in_(subs), UnderlyingPrice.idx_px.isnot(None),
                UnderlyingPrice.captured_at <= hi)
         .order_by(UnderlyingPrice.captured_at.desc()).limit(1))
    if underlying:
        q = q.where(UnderlyingPrice.underlying == underlying)
    row = s.execute(q).first()
    return _f(row[0]) if row else None


@router.get("/payoff")
def payoff(underlying: str | None = None, expiry: str | None = None,
           subaccount: int | None = None, user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    now = datetime.now(timezone.utc)
    today = now.date()

    def _expired(exp: date | None) -> bool:
        """OKX options settle at 08:00 UTC on the expiry date — expired once that moment passed."""
        return exp is not None and now >= datetime.combine(exp, OKX_SETTLE_UTC, tzinfo=timezone.utc)

    with SessionLocal() as s:
        oq = select(PositionCurrent).where(PositionCurrent.subaccount_id.in_(subs),
                                           PositionCurrent.opt_type.isnot(None),
                                           PositionCurrent.strike.isnot(None))
        cq = select(DealLedger).where(DealLedger.subaccount_id.in_(subs),
                                      DealLedger.opt_type.isnot(None),
                                      DealLedger.strike.isnot(None),
                                      DealLedger.expiry.isnot(None))
        if underlying:
            oq = oq.where(PositionCurrent.underlying == underlying)
            cq = cq.where(DealLedger.underlying == underlying)
        open_all = s.execute(oq).scalars().all()
        closed_all = s.execute(cq).scalars().all()

        open_exps = {p.expiry for p in open_all if p.expiry}
        closed_exps = {c.expiry for c in closed_all if c.expiry}
        expiries = sorted(open_exps | closed_exps)
        # Default to the LAST known expiry (expired or active). An expiry past its 08:00 UTC
        # settlement is treated as expired even if it still lingers in the open snapshot.
        chosen = date.fromisoformat(expiry) if expiry else (max(expiries) if expiries else None)

        chosen_expired = _expired(chosen)
        open_legs = [p for p in open_all if p.expiry == chosen]
        closed_legs = [c for c in closed_all if c.expiry == chosen]

        # Normalize to (strike, opt_type, signed_size, premium_usd, iv); size scaled by contract
        # underlying units (ctVal). fees_usd accrues the (negative) fee cost to fold into the P&L.
        norm: list[tuple] = []
        fees_usd = 0.0
        realized_usd = None   # expired only: real OKX realized P&L (deal_ledger), for the marker
        if not chosen_expired and open_legs:            # OPEN: live book → spot marker (with time)
            mode = "open"
            marker_price = next((_f(p.idx_px) for p in open_legs if p.idx_px), None) \
                or _f(open_legs[0].strike)
            marker_at = next((p.captured_at for p in open_legs if p.idx_px and p.captured_at), None)
            marker = {"value": marker_price, "label": "spot",
                      "time": marker_at.strftime("%H:%M") if marker_at else None}
            include_t0 = True
            for p in open_legs:
                cs = contract_size(p.underlying)
                fees_usd += _f(p.fee) * (_f(p.idx_px) or marker_price)   # fee (coin) → USD
                norm.append((_f(p.strike), p.opt_type, _f(p.size) * cs,   # pos is signed (+long/−short)
                             _f(p.avg_px) * (_f(p.idx_px) or marker_price), _f(p.iv) or 0.5))
        elif closed_legs:                               # EXPIRED (settled): closed book → expiry price
            mode = "expired"
            marker_price = _settlement_price(s, subs, underlying, chosen) \
                or (sum(_f(c.strike) for c in closed_legs) / len(closed_legs))
            marker = {"value": marker_price, "label": "expiry", "time": None}
            include_t0 = False
            realized_usd = sum(_f(c.realized_pnl_usd) for c in closed_legs)  # real OKX figure (=①a)
            for c in closed_legs:
                sign = -1.0 if (c.side or "").lower() == "short" else 1.0
                cs = contract_size(c.underlying)
                fees_usd += _f(c.fee) * marker_price
                norm.append((_f(c.strike), c.opt_type, sign * _f(c.size) * cs,
                             _f(c.entry_px) * marker_price, 0.0))
        elif chosen_expired and open_legs:              # expired at 08:00 but not yet in deal ledger
            mode = "expired"
            marker_price = _settlement_price(s, subs, underlying, chosen) \
                or (sum(_f(p.strike) for p in open_legs) / len(open_legs))
            marker = {"value": marker_price, "label": "expiry", "time": None}
            include_t0 = False
            for p in open_legs:
                cs = contract_size(p.underlying)
                fees_usd += _f(p.fee) * (_f(p.idx_px) or marker_price)
                norm.append((_f(p.strike), p.opt_type, _f(p.size) * cs,
                             _f(p.avg_px) * (_f(p.idx_px) or marker_price), 0.0))
        else:
            return {"spot_grid": [], "at_expiry": [], "t0": [], "breakevens": [],
                    "marker": None, "mode": "empty",
                    "expiry": chosen.isoformat() if chosen else None,
                    "expiries": [e.isoformat() for e in expiries]}

    t_years = max((chosen - today).days, 0) / 365.0
    strikes = [k for k, *_ in norm if k]
    lo, hi = (min(strikes) * 0.95, max(strikes) * 1.05) if strikes \
        else (marker_price * 0.95, marker_price * 1.05)
    grid = [lo + (hi - lo) * i / 60 for i in range(61)]

    # fees_usd is the (negative) fee cost already paid — fold it in so P&L is net of fees.
    at_expiry, t0 = [], []
    for S in grid:
        at_expiry.append(round(sum(sz * (payoff_intrinsic(S, k, ot) - prem)
                                   for k, ot, sz, prem, _iv in norm) + fees_usd, 2))
        if include_t0:
            t0.append(round(sum(sz * (black76_price(S, k, t_years, iv, ot) - prem)
                                for k, ot, sz, prem, iv in norm) + fees_usd, 2))
    bes = []
    for i in range(1, len(grid)):
        y0, y1 = at_expiry[i - 1], at_expiry[i]
        if y0 == 0 or (y0 < 0) != (y1 < 0):
            x = grid[i - 1] + (grid[i] - grid[i - 1]) * (0 - y0) / (y1 - y0) if y1 != y0 else grid[i]
            bes.append(round(x, 1))
    return {"spot_grid": [round(x, 1) for x in grid], "at_expiry": at_expiry, "t0": t0,
            "breakevens": bes, "marker": marker, "mode": mode,
            "realized_usd": (round(realized_usd, 2) if realized_usd is not None else None),
            "expiry": chosen.isoformat() if chosen else None,
            "expiries": [e.isoformat() for e in expiries]}


# ⑥ Analyze tab (live recompute from deal ledger) -------------------------- #
@router.get("/analyze")
def analyze(period: str = "all", underlying: str | None = None, strategy: int | None = None,
            subaccount: int | None = None, user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    start = _period_start(period, datetime.now(timezone.utc).date())
    with SessionLocal() as s:
        q = select(DealLedger).where(DealLedger.subaccount_id.in_(subs))
        if underlying:
            q = q.where(DealLedger.underlying == underlying)
        if strategy is not None:
            q = q.where(DealLedger.strategy_id == strategy)
        deals = s.execute(q).scalars().all()
        strat_meta = {sid: (name, color) for sid, name, color in s.execute(
            select(Strategy.id, Strategy.name, Strategy.color).where(
                Strategy.subaccount_id.in_(subs)))}
        eq_rows = s.execute(
            select(BalanceTimeseries.captured_at, func.sum(BalanceTimeseries.equity_usd))
            .where(BalanceTimeseries.subaccount_id.in_(subs))
            .group_by(BalanceTimeseries.captured_at).order_by(BalanceTimeseries.captured_at)
        ).all()

    if start is not None:
        deals = [d for d in deals if d.closed_at and d.closed_at.date() >= start]
        eq_rows = [(t, v) for t, v in eq_rows if t.date() >= start]

    realized = [_f(d.realized_pnl) for d in deals]
    dm = deal_metrics(realized)
    em = equity_metrics([_f(v) for _, v in eq_rows])
    kpis = {"net_pnl": dm.net_pnl, "return_pct": em.return_pct, "win_rate": dm.win_rate,
            "n_deals": dm.n_deals, "avg_win": dm.avg_win, "avg_loss": dm.avg_loss,
            "profit_factor": dm.profit_factor, "max_drawdown_pct": em.max_drawdown_pct}

    by_strat: dict[int, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for d in deals:
        by_strat[d.strategy_id].append(_f(d.realized_pnl))
        by_symbol[d.underlying or "?"].append(_f(d.realized_pnl))

    strategies = []
    for sid, pnls in by_strat.items():
        m = deal_metrics(pnls)
        name, color = strat_meta.get(sid, ("(unassigned)", None))
        strategies.append({"strategy_id": sid, "name": name, "color": color,
                           "n_deals": m.n_deals, "win_rate": m.win_rate, "net_pnl": m.net_pnl,
                           "avg_pnl_per_deal": m.avg_pnl_per_deal,
                           "profit_factor": m.profit_factor})
    strategies.sort(key=lambda r: r["net_pnl"], reverse=True)

    symbols = []
    for uly, pnls in by_symbol.items():
        m = deal_metrics(pnls)
        symbols.append({"underlying": uly, "net_pnl": m.net_pnl, "n_deals": m.n_deals,
                        "win_rate": m.win_rate, "profit_factor": m.profit_factor})
    symbols.sort(key=lambda r: r["net_pnl"], reverse=True)

    return {"kpis": kpis, "strategies": strategies, "symbols": symbols}


@router.get("/deals")
def deals(period: str = "all", underlying: str | None = None, strategy: int | None = None,
          subaccount: int | None = None, limit: int = 200,
          user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    start = _period_start(period, datetime.now(timezone.utc).date())
    with SessionLocal() as s:
        q = select(DealLedger).where(DealLedger.subaccount_id.in_(subs))
        if underlying:
            q = q.where(DealLedger.underlying == underlying)
        if strategy is not None:
            q = q.where(DealLedger.strategy_id == strategy)
        q = q.order_by(DealLedger.closed_at.desc()).limit(limit)
        rows = s.execute(q).scalars().all()
    out = []
    for d in rows:
        if start and d.closed_at and d.closed_at.date() < start:
            continue
        out.append({
            "inst_id": d.inst_id, "underlying": d.underlying, "opt_type": d.opt_type,
            "strike": _f(d.strike), "expiry": d.expiry.isoformat() if d.expiry else None,
            "side": d.side, "close_type": d.close_type,
            "opened_at": d.opened_at.isoformat() if d.opened_at else None,
            "closed_at": d.closed_at.isoformat() if d.closed_at else None,
            "entry_px": _f(d.entry_px), "exit_px": _f(d.exit_px), "size": _f(d.size),
            "fee": _f(d.fee), "realized_pnl": _f(d.realized_pnl), "hold_days": d.hold_days,
        })
    return {"deals": out}
