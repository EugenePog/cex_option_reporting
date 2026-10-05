"""JSON report endpoints backing the dashboard reports and the Analyze tab.

All queries are scoped to the current user's subaccounts (admin => all). See
REPORT_GOLD_ATTRIBUTE_MAPPING.md for the column→visual mapping this implements.
The Dashboard reads current/aggregate gold — graph ① "Price with strategy boxes" (/price-boxes)
reads gold.index_candle + gold.box_shape + gold.position_leg; the Analyze tab recomputes from
gold.deal_ledger so it responds live to filters (the precomputed perf tables are used for
fixed-period admin views).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.contract_sizes import load_contract_sizes
from app.db.models_core import Strategy, Subaccount
from app.db.models_gold import (
    AssetBalanceTimeseries,
    AssetPnlDaily,
    BalanceTimeseries,
    BoxShape,
    DealLedger,
    GreeksByExpiry,
    IndexCandle,
    PnlDaily,
    ExpirySettlement,
    PositionCurrent,
    PositionLeg as GoldPositionLeg,
    UnderlyingPrice,
)
from app.domain.boxes import (
    BAR_STEP,
    BARS,
    Leg,
    auto_bar,
    build_boxes,
    closed_early,
    exposure_segments,
    in_period,
)
from app.domain.metrics import deal_metrics, equity_metrics
from app.domain.instruments import OPTION_SETTLE_UTC as OKX_SETTLE_UTC  # 08:00 UTC on expiry day
from app.domain.pricing import black76_price, payoff_intrinsic
from app.web.accounts import account_labels, account_options
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
                .where(Strategy.subaccount_id.in_(subs), Strategy.deleted_at.is_(None))
                .order_by(Strategy.name)
            )
        ]
        assets = sorted({c for (c,) in s.execute(
            select(AssetBalanceTimeseries.ccy)
            .where(AssetBalanceTimeseries.subaccount_id.in_(subs))
        ) if c})
        # Defaults the Dashboard fills into its selectors BEFORE drawing anything, so every graph
        # uses (and the page shows) the same values: the underlying with the most position legs
        # (ties: alphabetical), and the user's only account — or "" = all accounts when several.
        leg_counts = dict(s.execute(
            select(GoldPositionLeg.underlying, func.count())
            .where(GoldPositionLeg.subaccount_id.in_(subs), GoldPositionLeg.underlying.isnot(None))
            .group_by(GoldPositionLeg.underlying)).all())
        # underlyings with index candles on the user's exchanges (graph ① can show their price even
        # before there are positions in them)
        cexes = select(Subaccount.cex_code).where(Subaccount.id.in_(subs))
        priced = {u for (u,) in s.execute(
            select(IndexCandle.inst_id).where(IndexCandle.cex_code.in_(cexes)).distinct())}
        # account names for the Account selectors — the same names as on the Box builder
        accounts = [{"id": a["id"], "label": a["label"]} for a in account_options(s, subs)]
    underlyings = sorted(set(underlyings) | set(leg_counts) | priced)
    default_ul = (min(underlyings, key=lambda u: (-leg_counts.get(u, 0), u))
                  if underlyings else None)
    return {"underlyings": underlyings, "strategies": strategies, "assets": assets,
            "subaccounts": subs, "accounts": accounts, "is_admin": user.is_admin,
            "periods": ["mtd", "ytd", "all", "7d", "30d", "90d"],
            "defaults": {"underlying": default_ul,
                         "subaccount": subs[0] if len(subs) == 1 else ""}}


# ②b Per-asset (in-kind) equity & daily net P&L --------------------------- #
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


# ① Price with strategy boxes --------------------------------------------- #
def _iso(v: datetime | None) -> str | None:
    """UTC ISO string — the chart's time axis is UTC (expiries are at 08:00 UTC)."""
    return v.astimezone(timezone.utc).isoformat() if v else None


def _num(v) -> float | None:
    return float(v) if v is not None else None


@router.get("/price-boxes")
def price_boxes(underlying: str | None = None, subaccount: int | None = None, bar: str = "auto",
                frm: str | None = None, till: str | None = None,
                user: CurrentUser = Depends(get_current_user)) -> dict:
    """Index candles with every box (strategy) drawn on them, for the user's accounts.

    Each leg = a line at its strike from open time to expiry (closed early: solid to the close, a
    dot, dashed to expiry); each box = the rectangle around its legs, labelled with their total size
    in coin; `exposure` = total open size over time (legs count until they are closed). Obeys the
    Dashboard filters (underlying, account) and its own reporting period (frm / till, 'YYYY-MM-DD',
    like the equity graph): only legs whose line overlaps the period are drawn and the boxes are
    built around those legs — the whole box (gold.box_shape) when there is no period.
    Without an underlying the one with the most legs that has price data is used."""
    subs = _subs(user, subaccount)
    now = datetime.now(timezone.utc)
    fd, td, lo, hi = _date_bounds(frm, till)
    with SessionLocal() as s:
        sub_cex = dict(s.execute(select(Subaccount.id, Subaccount.cex_code)
                                 .where(Subaccount.id.in_(subs))).all())
        cexes = sorted(set(sub_cex.values()))
        priced = set(s.execute(select(IndexCandle.cex_code, IndexCandle.inst_id)
                               .where(IndexCandle.cex_code.in_(cexes)).distinct()).all())
        priced_ul = sorted({inst for _cex, inst in priced})
        chosen = underlying
        if not chosen:
            best = s.execute(
                select(GoldPositionLeg.underlying, func.count())
                .where(GoldPositionLeg.subaccount_id.in_(subs),
                       GoldPositionLeg.underlying.in_(priced_ul))
                .group_by(GoldPositionLeg.underlying).order_by(func.count().desc())).first()
            chosen = best[0] if best else (priced_ul[0] if priced_ul else None)
        cex = next((c for c in cexes if (c, chosen) in priced), None)

        rows = s.execute(select(GoldPositionLeg).where(
            GoldPositionLeg.subaccount_id.in_(subs), GoldPositionLeg.underlying == chosen,
            GoldPositionLeg.strike.isnot(None)).order_by(GoldPositionLeg.pos_opened_at)
        ).scalars().all() if chosen else []
        pairs = []                                   # (gold row, domain leg) inside the period
        for lg in rows:
            dom = Leg(
                leg_id=lg.position_leg_id, subaccount_id=lg.subaccount_id,
                strategy_id=lg.strategy_id, underlying=lg.underlying, strike=_num(lg.strike),
                opened_at=lg.pos_opened_at, expires_at=lg.expires_at, status=lg.status,
                closed_at=lg.closed_at, last_seen_at=lg.last_seen_at, size=_num(lg.size),
                size_coin=_num(lg.size_coin), coin=lg.coin, close_type=lg.close_type,
                pnl_usd=_num(lg.realized_pnl_usd if lg.realized_pnl_usd is not None
                             else lg.upl_usd))
            if in_period(dom, lo, hi):
                pairs.append((lg, dom))
        if lo is None and hi is None:                # whole history: the gold rectangles
            boxes = s.execute(select(BoxShape).where(BoxShape.subaccount_id.in_(subs),
                                                     BoxShape.underlying == chosen)
                              .order_by(BoxShape.started_at)).scalars().all() if chosen else []
        else:                                        # a period: same rule over the shown legs
            boxes = build_boxes([dom for _lg, dom in pairs])
        meta = {sid: (name, color) for sid, name, color in s.execute(
            select(Strategy.id, Strategy.name, Strategy.color)
            .where(Strategy.id.in_({b.strategy_id for b in boxes if b.strategy_id})))}
        acct = account_labels(s, {b.subaccount_id for b in boxes})

        # visible time range: the period (till inclusive → next midnight) if given; otherwise all
        # boxes (first open → last expiry) and now; with no boxes, the last 30 days
        if boxes:
            t0 = min(b.started_at for b in boxes) - timedelta(days=1)
            t1 = max(max(b.ends_at for b in boxes), now) + timedelta(days=1)
        else:
            t0, t1 = now - timedelta(days=30), now + timedelta(days=1)
        if lo is not None:
            t0 = lo
        if td is not None:
            t1 = datetime.combine(td + timedelta(days=1), time.min, tzinfo=timezone.utc)
        bar_auto = bar not in BARS
        bar_used = auto_bar(t0, t1) if bar_auto else bar
        candles, last = [], None
        if cex:
            candles = s.execute(
                select(IndexCandle.ts, IndexCandle.open, IndexCandle.high, IndexCandle.low,
                       IndexCandle.close)
                .where(IndexCandle.cex_code == cex, IndexCandle.inst_id == chosen,
                       IndexCandle.bar == bar_used, IndexCandle.ts >= t0 - BAR_STEP[bar_used],
                       IndexCandle.ts < t1)
                .order_by(IndexCandle.ts)).all()
            last = s.execute(
                select(IndexCandle.ts, IndexCandle.close, IndexCandle.n_minutes)
                .where(IndexCandle.cex_code == cex, IndexCandle.inst_id == chosen,
                       IndexCandle.bar == "1h")
                .order_by(IndexCandle.ts.desc()).limit(1)).first()

    multi_acct = len({b.subaccount_id for b in boxes}) > 1
    box_key = {(b.subaccount_id, b.strategy_id): i for i, b in enumerate(boxes)}
    out_boxes = []
    for i, b in enumerate(boxes):
        name, color = meta.get(b.strategy_id, ("(no box)", None))
        size_coin, size_ct = _num(b.size_coin), _num(b.size_contracts)
        label = (f"{size_coin:g} {b.coin}" if size_coin is not None
                 else f"{size_ct:g} contracts" if size_ct is not None else "")
        out_boxes.append({
            "key": i, "strategy_id": b.strategy_id, "subaccount_id": b.subaccount_id,
            "name": name, "color": color, "is_unassigned": name == "unassigned",
            "legend": (f"{name} · {acct.get(b.subaccount_id, b.subaccount_id)}" if multi_acct
                       else name),
            "started_at": _iso(b.started_at), "ends_at": _iso(b.ends_at),
            "strike_lo": _num(b.strike_lo), "strike_hi": _num(b.strike_hi),
            "n_legs": b.n_legs, "n_open_legs": b.n_open_legs, "status": b.status,
            "size_coin": size_coin, "size_contracts": size_ct, "coin": b.coin, "label": label,
            "net_pnl_usd": _num(b.net_pnl_usd),
        })
    out_legs = [{
        "id": lg.position_leg_id, "box": box_key.get((lg.subaccount_id, lg.strategy_id)),
        "inst_id": lg.inst_id, "opt_type": lg.opt_type, "strike": _num(lg.strike),
        "side": lg.side, "size": _num(lg.size), "size_coin": _num(lg.size_coin),
        "coin": lg.coin, "status": lg.status, "close_type": lg.close_type,
        "closed_early": closed_early(dom),
        "opened_at": _iso(lg.pos_opened_at), "expires_at": _iso(lg.expires_at),
        "closed_at": _iso(lg.closed_at), "pnl_usd": dom.pnl_usd,
    } for lg, dom in pairs]
    exposure = [{"t0": _iso(sg.t0), "t1": _iso(sg.t1), "total": sg.total}
                for sg in exposure_segments([dom for _lg, dom in pairs], now)]
    coin = next((b.coin for b in boxes if b.coin), None) or (chosen or "").split("-")[0] or None
    return {
        "underlying": chosen, "underlyings": priced_ul, "cex_code": cex,
        "bar": bar_used, "bar_auto": bar_auto, "now": _iso(now), "coin": coin,
        "period": {"from": fd.isoformat() if fd else None, "till": td.isoformat() if td else None},
        "range": [_iso(t0), _iso(t1)],
        "candles": [{"t": _iso(ts), "o": _f(o), "h": _f(h), "l": _f(lo_), "c": _f(c)}
                    for ts, o, h, lo_, c in candles],
        "last": ({"t": _iso(last[0] + timedelta(minutes=int(last[2]) - 1)), "close": _f(last[1])}
                 if last else None),
        "boxes": out_boxes, "legs": out_legs, "exposure": exposure,
    }


# ② Equity curve & daily net P&L ------------------------------------------- #
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

        # ② is a USD report → return USD (fee-inclusive net); coin kept for reference/back-compat.
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


# ④ Strike × expiry position map ------------------------------------------- #
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


# ⑤ Greeks term structure -------------------------------------------------- #
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


# ⑥ Maturity ladder -------------------------------------------------------- #
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


# ③ Payoff / risk profile -------------------------------------------------- #
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
        # contract size (ctVal) per exchange from core.contract_size; the leg's exchange comes from
        # its subaccount. A missing row falls back to 1 contract = 1 coin (logged once).
        sizes = load_contract_sizes(s)
        sub_cex = dict(s.execute(select(Subaccount.id, Subaccount.cex_code)).all())

        def contract_size(sub_id: int, underlying: str | None) -> float:
            return sizes.ct_val_or_default(sub_cex.get(sub_id), underlying)

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
                cs = contract_size(p.subaccount_id, p.underlying)
                fees_usd += _f(p.fee) * (_f(p.idx_px) or marker_price)   # fee (coin) → USD
                norm.append((_f(p.strike), p.opt_type, _f(p.size) * cs,   # pos is signed (+long/−short)
                             _f(p.avg_px) * (_f(p.idx_px) or marker_price), _f(p.iv) or 0.5))
        elif closed_legs:                               # EXPIRED (settled): closed book → expiry price
            mode = "expired"
            marker_price = _settlement_price(s, subs, underlying, chosen) \
                or (sum(_f(c.strike) for c in closed_legs) / len(closed_legs))
            marker = {"value": marker_price, "label": "expiry", "time": None}
            include_t0 = False
            realized_usd = sum(_f(c.realized_pnl_usd) for c in closed_legs)  # real OKX figure (=②)
            for c in closed_legs:
                sign = -1.0 if (c.side or "").lower() == "short" else 1.0
                cs = contract_size(c.subaccount_id, c.underlying)
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
                cs = contract_size(p.subaccount_id, p.underlying)
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


# Ⓐ Analyze tab (live recompute from deal ledger) -------------------------- #
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
