"""Bronze -> Silver transform.

Reads raw bronze rows, resolves each to a core.subaccount (via cex_code + account_label +
subacct_name), parses inst_ids, joins greeks onto positions and upserts typed rows into the silver
tables. Idempotent: safe to re-run.

Strategy tagging is per POSITION LEG (one OKX position lifecycle, key = posId + cTime):
  1. legs are collected from bronze.raw_position (open side, latest snapshot) and
     bronze.raw_closed_position (closed side) and upserted into silver.position_leg;
  2. each leg's strategy is resolved ONCE — manual pin (core.strategy_link) > strategy_rule >
     unassigned (domain/strategy_rules.resolve_strategy);
  3. position_snapshot / closed_position rows inherit the leg's strategy_id + strategy_source;
     trade_fill rows only link to their leg (position_leg_id), matched by subaccount + inst_id +
     fill time inside the leg's [cTime, uTime] window (domain/legs.match_fill_to_leg).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import session_scope
from app.db.models import (
    RawBalance,
    RawBill,
    RawClosedPosition,
    RawMargin,
    RawOptSummary,
    RawPosition,
    RawTradeFill,
)
from app.db.models_core import CexAccount, Strategy, StrategyLink, StrategyRule, Subaccount
from app.db.models_silver import (
    BalanceSnapshot,
    Bill,
    ClosedPosition,
    MarginSnapshot,
    PositionLeg,
    PositionSnapshot,
    TradeFill,
)
from app.domain.instruments import parse_inst_id
from app.domain.legs import LegWindow, match_fill_to_leg
from app.domain.strategy_rules import Resolution, Rule, TagRecord, resolve_strategy

logger = logging.getLogger(__name__)

LegKey = tuple[str, int, str, datetime]   # (cex_code, subaccount_id, posId, cTime)


def _f(val: Any) -> float | None:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(ms: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
class _Lookups:
    """Resolves bronze rows to subaccounts, and legs to strategies (pins, rules, unassigned)."""

    def __init__(self, session) -> None:
        # (cex_code, account_label, subacct_name) -> subaccount_id
        self.subaccount: dict[tuple[str, str, str], int] = {}
        rows = session.execute(
            select(CexAccount.cex_code, CexAccount.label, Subaccount.subacct_name, Subaccount.id)
            .join(Subaccount, Subaccount.cex_account_id == CexAccount.id)
        ).all()
        for cex_code, label, subacct_name, sub_id in rows:
            self.subaccount[(cex_code, label, subacct_name or "")] = sub_id

        # subaccount_id -> [Rule], and subaccount_id -> unassigned strategy_id
        # rules of deleted boxes (core.strategy.deleted_at, 0018) no longer apply
        self.rules: dict[int, list[Rule]] = {}
        for r in session.execute(
            select(StrategyRule).join(Strategy, Strategy.id == StrategyRule.strategy_id)
            .where(Strategy.deleted_at.is_(None))
        ).scalars():
            self.rules.setdefault(r.subaccount_id, []).append(
                Rule(strategy_id=r.strategy_id, priority=r.priority, match_json=r.match_json or {})
            )
        self.unassigned: dict[int, int] = {}
        for s in session.execute(
            select(Strategy).where(Strategy.name == "unassigned", Strategy.deleted_at.is_(None))
        ).scalars():
            self.unassigned[s.subaccount_id] = s.id

        # current manual pins: leg key -> (strategy_link.id, strategy_id). Highest precedence.
        self.pins: dict[LegKey, tuple[int, int]] = {}
        # (a pin to a deleted box is ignored → rules decide; deleting a box re-pins its legs first)
        for link in session.execute(
            select(StrategyLink).join(Strategy, Strategy.id == StrategyLink.strategy_id)
            .where(StrategyLink.superseded_at.is_(None), StrategyLink.action == "pin",
                   Strategy.deleted_at.is_(None))
        ).scalars():
            key = (link.cex_code, link.subaccount_id, link.pos_id, link.pos_opened_at)
            self.pins[key] = (link.id, link.strategy_id)

    def resolve_subaccount(self, cex_code: str, account_label: str, subacct_name: str) -> int | None:
        return self.subaccount.get((cex_code, account_label, subacct_name or ""))

    def resolve_leg(self, key: LegKey, rec: TagRecord) -> tuple[Resolution, int | None]:
        """(Resolution, strategy_link_id) for a leg: pin > rule > unassigned."""
        sub_id = key[1]
        pin = self.pins.get(key)
        res = resolve_strategy(self.rules.get(sub_id, []), rec, self.unassigned.get(sub_id),
                               pinned_strategy_id=pin[1] if pin else None)
        return res, (pin[0] if pin else None)

    def resolve_unkeyed(self, subaccount_id: int, rec: TagRecord) -> Resolution:
        """Rows that can't be tied to a leg (no posId/cTime in the payload): rules only."""
        return resolve_strategy(self.rules.get(subaccount_id, []), rec,
                                self.unassigned.get(subaccount_id))


def _upsert(session, model, values: dict, index_elements: list[str] | None = None,
            constraint: str | None = None, returning=None):
    stmt = pg_insert(model).values(**values)
    keys = set(index_elements or [])
    update = {c: stmt.excluded[c] for c in values if c not in keys and c != "id"}
    if constraint:
        stmt = stmt.on_conflict_do_update(constraint=constraint, set_=update)
    else:
        stmt = stmt.on_conflict_do_update(index_elements=index_elements, set_=update)
    if returning is not None:
        return session.execute(stmt.returning(returning)).scalar_one()
    session.execute(stmt)
    return None


# --------------------------------------------------------------------------- #
# Position legs (built first — every other position-type row hangs off a leg)
# --------------------------------------------------------------------------- #
@dataclass
class _Leg:
    key: LegKey
    inst_id: str
    side: str | None = None
    status: str = "stale"           # open | closed | stale (vanished from snapshots, no close yet)
    size: float | None = None
    entry_px: float | None = None
    exit_px: float | None = None
    closed_at: datetime | None = None
    close_type: str | None = None
    last_seen_at: datetime | None = None
    last_ingest_id: str | None = None
    realized_pnl: float | None = None
    upl: float | None = None
    fee: float | None = None
    ccy: str | None = None
    idx_px: float | None = None
    n_snapshots: int = 0
    n_fills: int = 0
    id: int | None = None
    res: Resolution | None = None
    link_id: int | None = None
    extra: dict = field(default_factory=dict)


def _pos_side(size: float | None) -> str:
    return "short" if (size or 0) < 0 else ("long" if (size or 0) > 0 else "flat")


def _latest_snapshot_runs(session, lk: _Lookups) -> dict[int, str]:
    """subaccount_id -> ingest_id of its most recent snapshot run (balances are written on every
    run, positions only when something is open — so the run is identified via raw_balance)."""
    latest: dict[int, tuple[datetime, str]] = {}
    rows = session.execute(
        select(RawBalance.cex_code, RawBalance.account_label, RawBalance.subacct_name,
               RawBalance.ingest_id, func.max(RawBalance.captured_at))
        .group_by(RawBalance.cex_code, RawBalance.account_label, RawBalance.subacct_name,
                  RawBalance.ingest_id)
    ).all()
    for cex, label, sub_name, ingest_id, cap in rows:
        sub_id = lk.resolve_subaccount(cex, label, sub_name)
        if sub_id is None or cap is None:
            continue
        if sub_id not in latest or cap > latest[sub_id][0]:
            latest[sub_id] = (cap, ingest_id)
    return {k: v[1] for k, v in latest.items()}


def _collect_legs(session, lk: _Lookups) -> dict[LegKey, _Leg]:
    legs: dict[LegKey, _Leg] = {}

    # open side: every snapshot row contributes; the latest one sets the current state
    for pos in session.execute(select(RawPosition)).scalars():
        sub_id = lk.resolve_subaccount(pos.cex_code, pos.account_label, pos.subacct_name)
        p = pos.payload
        pos_id, c_time = p.get("posId") or "", _ts(p.get("cTime"))
        if sub_id is None or not pos_id or c_time is None:
            continue
        key = (pos.cex_code, sub_id, str(pos_id), c_time)
        leg = legs.get(key) or legs.setdefault(key, _Leg(key=key, inst_id=p.get("instId", "")))
        leg.n_snapshots += 1
        if leg.last_seen_at is None or pos.captured_at >= leg.last_seen_at:
            size = _f(p.get("pos"))
            leg.last_seen_at, leg.last_ingest_id = pos.captured_at, pos.ingest_id
            leg.upl, leg.fee, leg.idx_px = _f(p.get("upl")), _f(p.get("fee")), _f(p.get("idxPx"))
            leg.side = _pos_side(size)
            leg.size = abs(size) if size is not None else None
            leg.entry_px = _f(p.get("avgPx"))
            leg.ccy = p.get("ccy") or leg.ccy

    latest_run = _latest_snapshot_runs(session, lk)
    for leg in legs.values():
        if leg.last_ingest_id is not None and latest_run.get(leg.key[1]) == leg.last_ingest_id:
            leg.status = "open"

    # closed side: positions-history rows close (and fully describe) their leg
    for c in session.execute(select(RawClosedPosition)).scalars():
        sub_id = lk.resolve_subaccount(c.cex_code, c.account_label, c.subacct_name)
        p = c.payload
        c_time = c.pos_opened_at or _ts(p.get("cTime"))
        if sub_id is None or not c.ext_id or c_time is None:
            continue
        key = (c.cex_code, sub_id, c.ext_id, c_time)
        leg = legs.get(key) or legs.setdefault(key, _Leg(key=key, inst_id=p.get("instId", "")))
        leg.status = "closed"
        leg.side = (p.get("direction") or "").lower() or leg.side
        leg.size = _f(p.get("openMaxPos")) or _f(p.get("closeTotalPos")) or leg.size
        leg.entry_px = _f(p.get("openAvgPx"))
        leg.exit_px = _f(p.get("closeAvgPx"))
        leg.closed_at = _ts(p.get("uTime")) or c.captured_at
        leg.close_type = p.get("type")
        leg.realized_pnl = _f(p.get("realizedPnl"))
        leg.fee = _f(p.get("fee"))
        leg.ccy = p.get("ccy") or leg.ccy
        leg.upl = None
    return legs


def _upsert_legs(session, lk: _Lookups, legs: dict[LegKey, _Leg], fill_counts: dict) -> None:
    now = datetime.now(timezone.utc)
    for leg in legs.values():
        cex, sub_id, pos_id, c_time = leg.key
        parsed = parse_inst_id(leg.inst_id)
        leg.res, leg.link_id = lk.resolve_leg(leg.key, TagRecord(
            inst_id=leg.inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=leg.side, opened_at=c_time,
        ))
        leg.n_fills = fill_counts.get(leg.key, 0)
        leg.id = _upsert(session, PositionLeg, {
            "cex_code": cex, "subaccount_id": sub_id, "pos_id": pos_id, "pos_opened_at": c_time,
            "inst_id": leg.inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": leg.side,
            "status": leg.status, "size": leg.size, "entry_px": leg.entry_px,
            "exit_px": leg.exit_px, "closed_at": leg.closed_at, "close_type": leg.close_type,
            "last_seen_at": leg.last_seen_at, "realized_pnl": leg.realized_pnl, "upl": leg.upl,
            "fee": leg.fee, "ccy": leg.ccy, "idx_px": leg.idx_px, "n_fills": leg.n_fills,
            "n_snapshots": leg.n_snapshots, "strategy_id": leg.res.strategy_id,
            "strategy_source": leg.res.source, "rule_strategy_id": leg.res.rule_strategy_id,
            "strategy_link_id": leg.link_id, "updated_at": now,
        }, index_elements=["cex_code", "subaccount_id", "pos_id", "pos_opened_at"],
            returning=PositionLeg.id)


def _fill_leg_index(
        legs: dict[LegKey, _Leg]) -> dict[tuple[str, int, str], list[tuple[LegKey, LegWindow]]]:
    """(cex_code, subaccount_id, inst_id) -> [(leg key, its time window)]."""
    idx: dict[tuple[str, int, str], list[tuple[LegKey, LegWindow]]] = defaultdict(list)
    for i, leg in enumerate(legs.values()):
        cex, sub_id, _pos, c_time = leg.key
        idx[(cex, sub_id, leg.inst_id)].append(
            (leg.key, LegWindow(leg_id=i, opened_at=c_time, closed_at=leg.closed_at)))
    return idx


def _match_fills(session, lk: _Lookups, legs: dict[LegKey, _Leg]) -> dict[int, LegKey | None]:
    """raw_trade_fill.id -> leg key (or None when no leg window contains the fill)."""
    idx = _fill_leg_index(legs)
    out: dict[int, LegKey | None] = {}
    for f in session.execute(select(RawTradeFill)).scalars():
        sub_id = lk.resolve_subaccount(f.cex_code, f.account_label, f.subacct_name)
        if sub_id is None:
            continue
        p = f.payload
        filled_at = _ts(p.get("ts") or p.get("fillTime"))
        cands = idx.get((f.cex_code, sub_id, f.inst_id or p.get("instId", "")), [])
        hit = match_fill_to_leg(filled_at, [w for _k, w in cands]) if filled_at else None
        out[f.id] = (next((k for k, w in cands if w.leg_id == hit), None)
                     if hit is not None else None)
    return out


# --------------------------------------------------------------------------- #
# Transforms
# --------------------------------------------------------------------------- #
def _transform_balances(session, lk: _Lookups) -> tuple[int, int]:
    written = skipped = 0
    for b in session.execute(select(RawBalance)).scalars():
        sub_id = lk.resolve_subaccount(b.cex_code, b.account_label, b.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = b.payload
        _upsert(session, BalanceSnapshot, {
            "cex_code": b.cex_code, "subaccount_id": sub_id,
            "ccy": p.get("ccy"), "total": _f(p.get("eq")),
            "available": _f(p.get("availEq") or p.get("availBal")),
            "usd_value": _f(p.get("eqUsd")),
            "captured_at": b.captured_at, "ingest_id": b.ingest_id,
        }, ["subaccount_id", "ccy", "captured_at"])
        written += 1
    return written, skipped


def _transform_margin(session, lk: _Lookups) -> tuple[int, int]:
    written = skipped = 0
    for m in session.execute(select(RawMargin)).scalars():
        sub_id = lk.resolve_subaccount(m.cex_code, m.account_label, m.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = m.payload
        if p.get("imr") not in (None, "") and p.get("mgnRatio") not in (None, ""):
            details = p.get("details") or []
            eq_usd = (sum((_f(d.get("eqUsd")) or 0) for d in details) if details
                      else _f(p.get("totalEq")))
            scope, imr, mmr, ratio = "ACCOUNT", _f(p.get("imr")), _f(p.get("mmr")), _f(p.get("mgnRatio"))
        else:
            scope = p.get("ccy") or "ACCOUNT"
            eq_usd, imr, mmr, ratio = (_f(p.get("eqUsd")), _f(p.get("imr")),
                                       _f(p.get("mmr")), _f(p.get("mgnRatio")))
        _upsert(session, MarginSnapshot, {
            "cex_code": m.cex_code, "subaccount_id": sub_id, "scope": scope,
            "eq_usd": eq_usd, "imr_usd": imr, "mmr_usd": mmr, "margin_ratio": ratio,
            "captured_at": m.captured_at, "ingest_id": m.ingest_id,
        }, ["subaccount_id", "scope", "captured_at"])
        written += 1
    return written, skipped


def _transform_positions(session, lk: _Lookups, legs: dict[LegKey, _Leg]) -> tuple[int, int]:
    # Preload greeks by (ingest_id, inst_id).
    greeks: dict[tuple[str, str], dict] = {}
    for o in session.execute(select(RawOptSummary)).scalars():
        greeks[(o.ingest_id, o.payload.get("instId"))] = o.payload

    written = skipped = 0
    for pos in session.execute(select(RawPosition)).scalars():
        sub_id = lk.resolve_subaccount(pos.cex_code, pos.account_label, pos.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = pos.payload
        inst_id = p.get("instId", "")
        parsed = parse_inst_id(inst_id)
        size = _f(p.get("pos"))
        side = _pos_side(size)
        g = greeks.get((pos.ingest_id, inst_id), {})
        pos_id, c_time = (p.get("posId") or None), _ts(p.get("cTime"))
        leg = legs.get((pos.cex_code, sub_id, str(pos_id), c_time)) if pos_id and c_time else None
        res = leg.res if leg is not None else lk.resolve_unkeyed(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type, side=side))
        _upsert(session, PositionSnapshot, {
            "cex_code": pos.cex_code, "subaccount_id": sub_id, "strategy_id": res.strategy_id,
            "strategy_source": res.source, "pos_id": pos_id, "pos_opened_at": c_time,
            "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": side, "size": size,
            "avg_px": _f(p.get("avgPx")), "mark_px": _f(p.get("markPx") or g.get("markPx")),
            "idx_px": _f(p.get("idxPx")), "fwd_px": _f(g.get("fwdPx")),
            "upl": _f(p.get("upl")), "fee": _f(p.get("fee")),
            "notional_usd": _f(p.get("notionalUsd")), "opt_val": _f(p.get("optVal")),
            # coin greeks (opt-summary) + Black-Scholes dollar greeks (positions)
            "delta": _f(g.get("delta")), "gamma": _f(g.get("gamma")),
            "theta": _f(g.get("theta")), "vega": _f(g.get("vega")), "iv": _f(g.get("markVol")),
            "delta_bs": _f(p.get("deltaBS")), "gamma_bs": _f(p.get("gammaBS")),
            "theta_bs": _f(p.get("thetaBS")), "vega_bs": _f(p.get("vegaBS")),
            "captured_at": pos.captured_at, "ingest_id": pos.ingest_id,
        }, ["subaccount_id", "inst_id", "side", "captured_at"])
        written += 1
    return written, skipped


def _transform_fills(session, lk: _Lookups, legs: dict[LegKey, _Leg],
                     fill_legs: dict[int, LegKey | None]) -> tuple[int, int]:
    """Fills carry no strategy of their own — only the link to their leg (position_leg_id)."""
    written = skipped = 0
    for f in session.execute(select(RawTradeFill)).scalars():
        sub_id = lk.resolve_subaccount(f.cex_code, f.account_label, f.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = f.payload
        inst_id = f.inst_id or p.get("instId", "")
        parsed = parse_inst_id(inst_id)
        okx_side = (p.get("side") or "").lower()          # buy | sell
        leg_key = fill_legs.get(f.id)
        leg = legs.get(leg_key) if leg_key else None
        _upsert(session, TradeFill, {
            "cex_code": f.cex_code, "subaccount_id": sub_id,
            "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": okx_side,
            "size": _f(p.get("fillSz")), "price": _f(p.get("fillPx")),
            "fee": _f(p.get("fee")), "fee_ccy": p.get("feeCcy"),
            "realized_pnl": _f(p.get("fillPnl")), "trade_id": f.trade_id,
            "filled_at": _ts(p.get("ts") or p.get("fillTime")), "ingest_id": f.ingest_id,
        }, ["cex_code", "inst_id", "trade_id"])
        written += 1
    return written, skipped


def _transform_closed(session, lk: _Lookups, legs: dict[LegKey, _Leg]) -> tuple[int, int]:
    written = skipped = 0
    for c in session.execute(select(RawClosedPosition)).scalars():
        sub_id = lk.resolve_subaccount(c.cex_code, c.account_label, c.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = c.payload
        inst_id = p.get("instId", "")
        parsed = parse_inst_id(inst_id)
        side = (p.get("direction") or "").lower() or None
        opened_at = c.pos_opened_at or _ts(p.get("cTime"))
        closed_at = _ts(p.get("uTime")) or c.captured_at
        leg = legs.get((c.cex_code, sub_id, c.ext_id, opened_at)) if opened_at else None
        res = leg.res if leg is not None else lk.resolve_unkeyed(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=side, opened_at=opened_at))
        _upsert(session, ClosedPosition, {
            "cex_code": c.cex_code, "subaccount_id": sub_id, "strategy_id": res.strategy_id,
            "strategy_source": res.source, "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "close_type": p.get("type"),
            "side": side, "size": _f(p.get("closeTotalPos")), "open_avg_px": _f(p.get("openAvgPx")),
            "close_avg_px": _f(p.get("closeAvgPx")), "realized_pnl": _f(p.get("realizedPnl")),
            "pnl": _f(p.get("pnl")), "fee": _f(p.get("fee")), "ccy": p.get("ccy"),
            "opened_at": opened_at, "closed_at": closed_at, "ext_id": c.ext_id,
            "ingest_id": c.ingest_id,
        }, constraint="uq_silver_closed_position",
            index_elements=["cex_code", "ext_id", "opened_at"])
        written += 1
    return written, skipped


def _transform_bills(s, lk: _Lookups) -> tuple[int, int]:
    written = skipped = 0
    for b in s.execute(select(RawBill)).scalars():
        sub_id = lk.resolve_subaccount(b.cex_code, b.account_label, b.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = b.payload
        inst_id = p.get("instId") or None
        parsed = parse_inst_id(inst_id or "")
        _upsert(s, Bill, {
            "cex_code": b.cex_code, "subaccount_id": sub_id, "bill_id": b.bill_id,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry,
            "bill_type": p.get("type"), "sub_type": p.get("subType"),
            "px": _f(p.get("px")), "pnl": _f(p.get("pnl")), "fee": _f(p.get("fee")),
            "ccy": p.get("ccy"), "billed_at": _ts(p.get("ts")), "ingest_id": b.ingest_id,
        }, ["cex_code", "bill_id"])
        written += 1
    return written, skipped


def run() -> dict[str, tuple[int, int]]:
    """Run all bronze->silver transforms. Returns {table: (written, skipped_unresolved)}."""
    with session_scope() as s:
        lk = _Lookups(s)
        if not lk.subaccount:
            logger.warning("no subaccounts found in core — seed core data first (make seed); "
                           "all bronze rows will be skipped")
        # legs first: every position-type row (snapshot / closed / fill) hangs off a leg
        legs = _collect_legs(s, lk)
        fill_legs = _match_fills(s, lk, legs)
        fill_counts: dict[LegKey, int] = defaultdict(int)
        for k in fill_legs.values():
            if k is not None:
                fill_counts[k] += 1
        _upsert_legs(s, lk, legs, fill_counts)
        results = {
            "balance_snapshot": _transform_balances(s, lk),
            "margin_snapshot": _transform_margin(s, lk),
            "position_leg": (len(legs), 0),
            "position_snapshot": _transform_positions(s, lk, legs),
            "trade_fill": _transform_fills(s, lk, legs, fill_legs),
            "closed_position": _transform_closed(s, lk, legs),
            "bill": _transform_bills(s, lk),
        }
        unlinked = sum(1 for k in fill_legs.values() if k is None)
        if unlinked:
            logger.info("silver trade_fill: %d fill(s) not matched to a position leg yet", unlinked)
        pinned = sum(1 for lg in legs.values() if lg.link_id is not None)
        logger.info("silver position_leg: %d legs (%d pinned manually)", len(legs), pinned)
    for table, (written, skipped) in results.items():
        logger.info("silver %s: %d written, %d skipped (unresolved subaccount)",
                    table, written, skipped)
    return results
