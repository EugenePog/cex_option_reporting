"""JSON report endpoints backing the six dashboard reports.

All queries are scoped to the current user's subaccounts (admin => all). See
REPORT_GOLD_ATTRIBUTE_MAPPING.md for the column→visual mapping this implements.
Reports ①–⑤ read current/aggregate gold; the Analyze tab ⑥ recomputes from gold.deal_ledger so it
responds live to filters (the precomputed perf tables are used for fixed-period admin views).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone

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
    PositionCurrent,
)
from app.domain.metrics import deal_metrics, equity_metrics
from app.domain.pricing import black76_price, payoff_intrinsic
from app.web.deps import CurrentUser, get_current_user

router = APIRouter(prefix="/api", tags=["reports"])


def _f(v) -> float:
    return float(v) if v is not None else 0.0


def _subs(user: CurrentUser, subaccount: int | None) -> list[int]:
    """Effective subaccount scope: the user's subs, optionally narrowed to one they own."""
    if subaccount is not None and subaccount in user.subaccount_ids:
        return [subaccount]
    return user.subaccount_ids


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
                 user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        if not ccy:  # default to the asset with the largest current balance
            latest = s.execute(
                select(AssetBalanceTimeseries.ccy, func.max(AssetBalanceTimeseries.amount))
                .where(AssetBalanceTimeseries.subaccount_id.in_(subs))
                .group_by(AssetBalanceTimeseries.ccy)
                .order_by(func.max(AssetBalanceTimeseries.amount).desc())
            ).first()
            ccy = latest[0] if latest else None
        bal_rows = s.execute(
            select(AssetBalanceTimeseries.captured_at, func.sum(AssetBalanceTimeseries.amount))
            .where(AssetBalanceTimeseries.subaccount_id.in_(subs), AssetBalanceTimeseries.ccy == ccy)
            .group_by(AssetBalanceTimeseries.captured_at).order_by(AssetBalanceTimeseries.captured_at)
        ).all() if ccy else []
        pnl_rows = s.execute(
            select(AssetPnlDaily.date, func.sum(AssetPnlDaily.net_pnl),
                   func.sum(AssetPnlDaily.realized_pnl), func.sum(AssetPnlDaily.unrealized_pnl),
                   func.sum(AssetPnlDaily.fees))
            .where(AssetPnlDaily.subaccount_id.in_(subs), AssetPnlDaily.ccy == ccy)
            .group_by(AssetPnlDaily.date).order_by(AssetPnlDaily.date)
        ).all() if ccy else []
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
def equity(subaccount: int | None = None, user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        eq_rows = s.execute(
            select(BalanceTimeseries.captured_at, func.sum(BalanceTimeseries.equity_usd))
            .where(BalanceTimeseries.subaccount_id.in_(subs))
            .group_by(BalanceTimeseries.captured_at).order_by(BalanceTimeseries.captured_at)
        ).all()
        pnl_rows = s.execute(
            select(PnlDaily.date, func.sum(PnlDaily.net_pnl), func.sum(PnlDaily.realized_pnl),
                   func.sum(PnlDaily.unrealized_pnl), func.sum(PnlDaily.fees))
            .where(PnlDaily.subaccount_id.in_(subs))
            .group_by(PnlDaily.date).order_by(PnlDaily.date)
        ).all()
    equity_series = [{"t": t.isoformat(), "v": _f(v)} for t, v in eq_rows]
    pnl = [{"date": d.isoformat(), "net": _f(n), "realized": _f(r),
            "unrealized": _f(u), "fees": _f(fee)} for d, n, r, u, fee in pnl_rows]
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


# ④ Payoff / risk profile -------------------------------------------------- #
@router.get("/payoff")
def payoff(underlying: str | None = None, expiry: str | None = None,
           subaccount: int | None = None, user: CurrentUser = Depends(get_current_user)) -> dict:
    subs = _subs(user, subaccount)
    with SessionLocal() as s:
        q = select(PositionCurrent).where(PositionCurrent.subaccount_id.in_(subs),
                                          PositionCurrent.opt_type.isnot(None),
                                          PositionCurrent.strike.isnot(None))
        if underlying:
            q = q.where(PositionCurrent.underlying == underlying)
        legs = s.execute(q).scalars().all()
    # pick an expiry (nearest) if not specified
    exps = sorted({p.expiry for p in legs if p.expiry})
    chosen = None
    if expiry:
        chosen = date.fromisoformat(expiry)
    elif exps:
        chosen = exps[0]
    legs = [p for p in legs if p.expiry == chosen]
    if not legs:
        return {"spot_grid": [], "at_expiry": [], "t0": [], "breakevens": [],
                "spot": None, "expiry": chosen.isoformat() if chosen else None, "expiries": [e.isoformat() for e in exps]}

    spot = next((_f(p.idx_px) for p in legs if p.idx_px), None) or _f(legs[0].strike)
    today = datetime.now(timezone.utc).date()
    t_years = max((chosen - today).days, 0) / 365.0
    lo, hi = spot * 0.8, spot * 1.2
    grid = [lo + (hi - lo) * i / 60 for i in range(61)]

    def premium_usd(p) -> float:  # entry premium per contract in USD (avg_px in coin × spot)
        return _f(p.avg_px) * (_f(p.idx_px) or spot)

    at_expiry, t0 = [], []
    for S in grid:
        ae = sum(_f(p.size) * (payoff_intrinsic(S, _f(p.strike), p.opt_type) - premium_usd(p))
                 for p in legs)
        tv = sum(_f(p.size) * (black76_price(S, _f(p.strike), t_years, _f(p.iv) or 0.5, p.opt_type)
                               - premium_usd(p)) for p in legs)
        at_expiry.append(round(ae, 2)); t0.append(round(tv, 2))
    # breakevens = sign changes of the at-expiry curve (linear interpolation)
    bes = []
    for i in range(1, len(grid)):
        y0, y1 = at_expiry[i - 1], at_expiry[i]
        if y0 == 0 or (y0 < 0) != (y1 < 0):
            x = grid[i - 1] + (grid[i] - grid[i - 1]) * (0 - y0) / (y1 - y0) if y1 != y0 else grid[i]
            bes.append(round(x, 1))
    return {"spot_grid": [round(x, 1) for x in grid], "at_expiry": at_expiry, "t0": t0,
            "breakevens": bes, "spot": spot,
            "expiry": chosen.isoformat() if chosen else None,
            "expiries": [e.isoformat() for e in exps]}


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
