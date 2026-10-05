"""Box builder — admin-only API for moving position legs between strategies.

The GUI calls a strategy a *box*; the API, DB and pipeline keep the name strategy (core.strategy).
The movable entity is a POSITION LEG (silver/gold.position_leg, key = OKX posId + cTime) — legs are
shown and moved one by one (no combo grouping). A move
writes an append-only row to core.strategy_link (action='pin'), which the bronze->silver pipeline
applies with precedence over every strategy_rule; 'Reset to rule' writes action='unpin'. Each
Apply / Reset / Undo is one changeset (uuid) and is also summarized in core.audit_log.

Boxes can be created, edited (name / color / description) and deleted. Delete is a soft delete
(core.strategy.deleted_at, 0018): every leg in the box is pinned to the account's `unassigned`
box as one changeset, the box's rules stop applying, and Undo of that changeset restores the box.

After a write the API queues a background recompute (silver + gold), serialized with the pm2
pipeline loop through the runner's advisory lock. Until it finishes, the board shows the affected
legs as "syncing" with their target strategy (current link vs. gold disagree).

Reads: gold.position_leg (board read model, USD) + core.strategy / strategy_rule / strategy_link,
silver.trade_fill / position_snapshot / closed_position (leg footprint in the detail drawer).
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models_core import (
    AuditLog,
    CexAccount,
    CoreUser,
    Strategy,
    StrategyLink,
    StrategyRule,
    Subaccount,
)
from app.db.models_gold import PositionLeg as GoldLeg
from app.db.models_silver import ClosedPosition, PositionLeg, PositionSnapshot, TradeFill
from app.web.accounts import account_options
from app.web.deps import CurrentUser, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/box-builder", tags=["admin", "box-builder"])

PIN, UNPIN = "pin", "unpin"
UNASSIGNED = "unassigned"          # the pipeline's fallback box, found by name — not editable
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _iso(v) -> str | None:
    return v.isoformat() if isinstance(v, (datetime, date)) else None


# --------------------------------------------------------------------------- #
# Background recompute (silver + gold) after a write
# --------------------------------------------------------------------------- #
_rc_lock = threading.Lock()
_rc_state: dict[str, Any] = {"running": False, "rerun": False, "last_started_at": None,
                             "last_finished_at": None, "last_error": None}


def _recompute_worker() -> None:
    from app.pipelines import runner

    while True:
        with _rc_lock:
            _rc_state["last_started_at"] = datetime.now(timezone.utc)
        err = None
        try:
            runner.run_stage("all")       # takes the pipeline advisory lock
        except Exception as e:  # noqa: BLE001 - surfaced through /recompute status
            err = str(e)
            logger.exception("box builder recompute failed")
        with _rc_lock:
            _rc_state["last_finished_at"] = datetime.now(timezone.utc)
            _rc_state["last_error"] = err
            if _rc_state["rerun"]:        # links changed while running → run once more
                _rc_state["rerun"] = False
                continue
            _rc_state["running"] = False
            return


def request_recompute() -> None:
    with _rc_lock:
        if _rc_state["running"]:
            _rc_state["rerun"] = True
            return
        _rc_state["running"] = True
    threading.Thread(target=_recompute_worker, name="box-builder-recompute", daemon=True).start()


def _rc_status() -> dict:
    with _rc_lock:
        st = dict(_rc_state)
    return {"running": st["running"] or st["rerun"], "last_started_at": _iso(st["last_started_at"]),
            "last_finished_at": _iso(st["last_finished_at"]), "last_error": st["last_error"]}


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #
def _subaccounts(s) -> list[dict]:
    return account_options(s)            # same account names as the Dashboard and Analyze tabs


def _check_sub(user: CurrentUser, subaccount_id: int) -> None:
    if subaccount_id not in user.subaccount_ids:
        raise HTTPException(404, "Unknown account")


def _strategies(s, sub_id: int, include_deleted: bool = False) -> list[Strategy]:
    q = select(Strategy).where(Strategy.subaccount_id == sub_id)
    if not include_deleted:
        q = q.where(Strategy.deleted_at.is_(None))
    return list(s.execute(q.order_by(Strategy.id)).scalars())


def _box_name(st: Strategy | None) -> str | None:
    if st is None:
        return None
    return st.name + (" (deleted)" if st.deleted_at is not None else "")


def _active_box(s, user: CurrentUser, strategy_id: int) -> Strategy:
    st = s.get(Strategy, strategy_id)
    if st is None or st.deleted_at is not None:
        raise HTTPException(404, "Unknown box")
    _check_sub(user, st.subaccount_id)
    return st


def _name_taken(s, sub_id: int, name: str, except_id: int | None = None) -> bool:
    q = select(Strategy.id).where(Strategy.subaccount_id == sub_id, Strategy.deleted_at.is_(None),
                                  func.lower(Strategy.name) == name.lower())
    if except_id is not None:
        q = q.where(Strategy.id != except_id)
    return s.execute(q).first() is not None


def _current_links(s, sub_id: int) -> dict[tuple, StrategyLink]:
    rows = s.execute(select(StrategyLink).where(StrategyLink.subaccount_id == sub_id,
                                                StrategyLink.superseded_at.is_(None))).scalars()
    return {(r.cex_code, r.subaccount_id, r.pos_id, r.pos_opened_at): r for r in rows}


def _user_names(s, ids) -> dict[int, str]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {uid: (dn or email) for uid, dn, email in s.execute(
        select(CoreUser.id, CoreUser.display_name, CoreUser.email).where(CoreUser.id.in_(ids)))}


def _leg_dict(g: GoldLeg, link: StrategyLink | None, names: dict[int, str],
              cex_code: str) -> dict:
    """Gold leg + its current manual link → effective board state (pending = not yet recomputed)."""
    eff_sid, eff_src, pending = g.strategy_id, g.strategy_source, False
    if link is not None and link.action == PIN:
        eff_src = "manual"
        if link.strategy_id != g.strategy_id or g.strategy_source != "manual":
            eff_sid, pending = link.strategy_id, True
    elif link is not None and link.action == UNPIN and g.strategy_source == "manual":
        eff_sid, eff_src, pending = g.rule_strategy_id, "rule", True
    closed = g.status == "closed"
    pnl = _f(g.realized_pnl_usd) if closed else _f(g.upl_usd)
    pin = None
    if link is not None and link.action == PIN:
        pin = {"id": link.id, "changeset_id": link.changeset_id, "reason": link.reason,
               "created_at": _iso(link.created_at), "created_by": names.get(link.created_by)}
    return {
        "id": g.position_leg_id, "cex_code": cex_code, "pos_id": g.pos_id,
        "pos_opened_at": _iso(g.pos_opened_at), "inst_id": g.inst_id, "underlying": g.underlying,
        "opt_type": g.opt_type, "strike": _f(g.strike), "expiry": _iso(g.expiry), "side": g.side,
        "status": g.status, "size": _f(g.size), "entry_px": _f(g.entry_px),
        "exit_px": _f(g.exit_px), "closed_at": _iso(g.closed_at), "close_type": g.close_type,
        "last_seen_at": _iso(g.last_seen_at), "pnl_usd": pnl,
        "realized_pnl_usd": _f(g.realized_pnl_usd), "upl_usd": _f(g.upl_usd),
        "n_fills": g.n_fills or 0, "n_snapshots": g.n_snapshots or 0,
        "strategy_id": eff_sid, "strategy_source": eff_src, "pending": pending,
        "computed_strategy_id": g.strategy_id, "rule_strategy_id": g.rule_strategy_id, "pin": pin,
    }


def _load_legs(s, sub_id: int) -> list[dict]:
    cex = s.execute(select(CexAccount.cex_code).join(
        Subaccount, Subaccount.cex_account_id == CexAccount.id).where(
        Subaccount.id == sub_id)).scalar_one_or_none() or ""
    links = _current_links(s, sub_id)
    names = _user_names(s, [lk.created_by for lk in links.values()])
    rows = s.execute(select(GoldLeg).where(GoldLeg.subaccount_id == sub_id)).scalars().all()
    return [_leg_dict(g, links.get((cex, sub_id, g.pos_id, g.pos_opened_at)), names, cex)
            for g in rows]


def _strategy_stats(legs: list[dict]) -> dict[int | None, dict]:
    st: dict[int | None, dict] = defaultdict(lambda: {"n_legs": 0, "n_open": 0, "n_deals": 0,
                                                      "wins": 0, "realized": 0.0, "upl": 0.0})
    for lg in legs:
        a = st[lg["strategy_id"]]
        a["n_legs"] += 1
        if lg["status"] == "closed":
            a["n_deals"] += 1
            r = lg["realized_pnl_usd"] or 0.0
            a["realized"] += r
            a["wins"] += 1 if r > 0 else 0
        else:
            a["n_open"] += 1 if lg["status"] == "open" else 0
            a["upl"] += lg["upl_usd"] or 0.0
    out = {}
    for sid, a in st.items():
        out[sid] = {"n_legs": a["n_legs"], "n_open": a["n_open"], "n_deals": a["n_deals"],
                    "net_pnl_usd": round(a["realized"] + a["upl"], 2),
                    "realized_pnl_usd": round(a["realized"], 2), "upl_usd": round(a["upl"], 2),
                    "win_rate": (a["wins"] / a["n_deals"]) if a["n_deals"] else None}
    return out


def _strategy_rows(s, sub_id: int, legs: list[dict]) -> list[dict]:
    stats = _strategy_stats(legs)
    rules = defaultdict(list)
    for r in s.execute(select(StrategyRule).where(StrategyRule.subaccount_id == sub_id)).scalars():
        rules[r.strategy_id].append({"id": r.id, "priority": r.priority, "match": r.match_json})
    empty = {"n_legs": 0, "n_open": 0, "n_deals": 0, "net_pnl_usd": 0.0, "realized_pnl_usd": 0.0,
             "upl_usd": 0.0, "win_rate": None}
    out = []
    for st in _strategies(s, sub_id):
        out.append({"id": st.id, "name": st.name, "color": st.color or "#888888",
                    "description": st.description, "is_unassigned": st.name == "unassigned",
                    "rules": rules.get(st.id, []), **stats.get(st.id, empty)})
    # unassigned last, others by id
    out.sort(key=lambda r: (r["is_unassigned"], r["id"]))
    return out


def _filter_legs(legs: list[dict], underlying: str | None, opened_from: str | None,
                 opened_to: str | None, status: str | None, source: str | None,
                 q: str | None) -> list[dict]:
    out = []
    ql = (q or "").strip().lower()
    for lg in legs:
        if underlying and lg["underlying"] != underlying:
            continue
        day = (lg["pos_opened_at"] or "")[:10]
        if opened_from and day < opened_from:
            continue
        if opened_to and day > opened_to:
            continue
        if status in ("open", "closed") and lg["status"] != status:
            continue
        if source in ("manual", "rule", "default") and lg["strategy_source"] != source:
            continue
        if ql and ql not in (lg["inst_id"] or "").lower() and ql not in (lg["pos_id"] or ""):
            continue
        out.append(lg)
    return out


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@router.get("/scope")
def scope(user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s:
        subs = [x for x in _subaccounts(s) if x["id"] in user.subaccount_ids]
        counts = dict(s.execute(select(GoldLeg.subaccount_id, func.count())
                                .group_by(GoldLeg.subaccount_id)).all())
        ulys = defaultdict(set)
        for sid, u in s.execute(select(GoldLeg.subaccount_id, GoldLeg.underlying).distinct()):
            if u:
                ulys[sid].add(u)
    for x in subs:
        x["n_legs"] = int(counts.get(x["id"], 0))
        x["underlyings"] = sorted(ulys.get(x["id"], set()))
    default = max(subs, key=lambda x: x["n_legs"])["id"] if subs else None
    return {"subaccounts": subs, "default": default}


@router.get("/board")
def board(subaccount: int, underlying: str | None = None, opened_from: str | None = None,
          opened_to: str | None = None, status: str | None = None, source: str | None = None,
          q: str | None = None, user: CurrentUser = Depends(require_admin)) -> dict:
    _check_sub(user, subaccount)
    with SessionLocal() as s:
        legs = _load_legs(s, subaccount)
        strategies = _strategy_rows(s, subaccount, legs)
        as_of = s.execute(select(func.max(PositionLeg.updated_at))
                          .where(PositionLeg.subaccount_id == subaccount)).scalar_one_or_none()
    # Leg filters (underlying, dates, status, source, find) narrow the Legs list only. The boxes
    # (strategy columns) always get every leg of the account, so `legs` is the full set and
    # `shown_leg_ids` is the filtered subset, both newest first.
    legs.sort(key=lambda lg: lg["pos_opened_at"] or "", reverse=True)
    shown = _filter_legs(legs, underlying, opened_from, opened_to, status, source, q)
    return {"subaccount": subaccount, "as_of": _iso(as_of), "strategies": strategies,
            "legs": legs, "shown_leg_ids": [lg["id"] for lg in shown],
            "counts": {"shown": len(shown), "all_legs": len(legs),
                       "pending": sum(1 for lg in legs if lg["pending"])},
            "recompute": _rc_status()}


@router.get("/leg/{leg_id}")
def leg_detail(leg_id: int, user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s:
        leg = s.get(PositionLeg, leg_id)
        if leg is None:
            raise HTTPException(404, "Unknown leg")
        _check_sub(user, leg.subaccount_id)
        legs = {lg["id"]: lg for lg in _load_legs(s, leg.subaccount_id)}
        cur = legs.get(leg_id)
        strat = {st.id: st for st in _strategies(s, leg.subaccount_id, include_deleted=True)}
        fills = s.execute(select(TradeFill).where(TradeFill.position_leg_id == leg_id)
                          .order_by(TradeFill.filled_at)).scalars().all()
        snap_n, snap_lo, snap_hi = s.execute(
            select(func.count(), func.min(PositionSnapshot.captured_at),
                   func.max(PositionSnapshot.captured_at))
            .where(PositionSnapshot.position_leg_id == leg_id)).one()
        closed = s.execute(select(ClosedPosition).where(
            ClosedPosition.position_leg_id == leg_id)).scalars().first()
        hist = s.execute(select(StrategyLink).where(
            StrategyLink.cex_code == leg.cex_code, StrategyLink.subaccount_id == leg.subaccount_id,
            StrategyLink.pos_id == leg.pos_id, StrategyLink.pos_opened_at == leg.pos_opened_at)
            .order_by(StrategyLink.id.desc())).scalars().all()
        names = _user_names(s, [h.created_by for h in hist])

    def sname(sid):
        st = strat.get(sid)
        return {"id": sid, "name": _box_name(st), "color": (st.color if st else None),
                "deleted": bool(st and st.deleted_at is not None)}

    history = [{"action": h.action, "strategy": sname(h.strategy_id) if h.strategy_id else None,
                "prev_strategy": sname(h.prev_strategy_id) if h.prev_strategy_id else None,
                "reason": h.reason, "changeset_id": h.changeset_id,
                "created_at": _iso(h.created_at), "created_by": names.get(h.created_by),
                "current": h.superseded_at is None} for h in hist]
    history.append({"action": "rule", "strategy": sname(leg.rule_strategy_id),
                    "created_at": _iso(leg.pos_opened_at), "created_by": "pipeline",
                    "reason": "rule (or unassigned) at ingestion", "current": False})
    return {
        "leg": cur,
        "strategy": sname(cur["strategy_id"]) if cur else None,
        "rule_strategy": sname(leg.rule_strategy_id),
        "footprint": {
            "trade_fill": {"n": len(fills), "rows": [
                {"trade_id": f.trade_id, "side": f.side, "size": _f(f.size), "price": _f(f.price),
                 "filled_at": _iso(f.filled_at)} for f in fills]},
            "position_snapshot": {"n": snap_n, "first": _iso(snap_lo), "last": _iso(snap_hi)},
            "closed_position": ({"n": 1, "closed_at": _iso(closed.closed_at),
                                 "realized_pnl": _f(closed.realized_pnl), "ext_id": closed.ext_id}
                                if closed else {"n": 0}),
        },
        "history": history,
    }


@router.get("/history")
def history(subaccount: int, user: CurrentUser = Depends(require_admin)) -> dict:
    _check_sub(user, subaccount)
    with SessionLocal() as s:
        rows = s.execute(select(StrategyLink).where(StrategyLink.subaccount_id == subaccount)
                         .order_by(StrategyLink.id.desc())).scalars().all()
        strat = {st.id: st for st in _strategies(s, subaccount, include_deleted=True)}
        deleted = [st for st in strat.values() if st.deleted_at is not None]
        names = _user_names(s, [r.created_by for r in rows] + [st.deleted_by for st in deleted])
    sets: dict[str, dict] = {}
    for r in rows:
        cs = sets.setdefault(r.changeset_id, {
            "changeset_id": r.changeset_id, "created_at": _iso(r.created_at),
            "created_by": names.get(r.created_by), "reason": r.reason, "n_rows": 0, "n_current": 0,
            "actions": defaultdict(int), "targets": defaultdict(int), "legs": []})
        cs["n_rows"] += 1
        cs["n_current"] += 1 if r.superseded_at is None else 0
        cs["actions"][r.action] += 1
        tgt = _box_name(strat[r.strategy_id]) if r.strategy_id in strat else "rules"
        cs["targets"][tgt] += 1
        if len(cs["legs"]) < 6:
            cs["legs"].append(r.inst_id)
    # "box deleted" changesets: Undo restores the box (also when it held no legs → no link rows)
    for st in deleted:
        if not st.deleted_changeset_id:
            continue
        cs = sets.setdefault(st.deleted_changeset_id, {
            "changeset_id": st.deleted_changeset_id, "created_at": _iso(st.deleted_at),
            "created_by": names.get(st.deleted_by), "reason": f"Box '{st.name}' deleted",
            "n_rows": 0, "n_current": 0, "actions": {}, "targets": {}, "legs": []})
        cs["deleted_box"] = {"id": st.id, "name": st.name, "color": st.color}
    out = []
    for cs in sets.values():
        cs["actions"], cs["targets"] = dict(cs["actions"]), dict(cs["targets"])
        cs["undoable"] = cs["n_current"] > 0 or "deleted_box" in cs
        out.append(cs)
    out.sort(key=lambda c: c["created_at"] or "", reverse=True)
    return {"changesets": out}


@router.get("/recompute")
def recompute_status(user: CurrentUser = Depends(require_admin)) -> dict:
    return _rc_status()


@router.post("/recompute")
def recompute_now(user: CurrentUser = Depends(require_admin)) -> dict:
    request_recompute()
    return _rc_status()


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #
class Move(BaseModel):
    leg_id: int
    to_strategy_id: int


class MovesIn(BaseModel):
    subaccount_id: int
    moves: list[Move] = Field(default_factory=list)
    reason: str | None = None


class ResetIn(BaseModel):
    subaccount_id: int
    leg_ids: list[int]
    reason: str


class UndoIn(BaseModel):
    changeset_id: str
    reason: str | None = None


class StrategyIn(BaseModel):
    subaccount_id: int
    name: str
    color: str | None = None
    description: str | None = None


class StrategyEditIn(BaseModel):
    name: str
    color: str | None = None
    description: str | None = None


def _valid_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(422, "Name is required (max 64 characters)")
    if name.lower() == UNASSIGNED:
        raise HTTPException(422, f"'{UNASSIGNED}' is reserved for the fallback box")
    return name


def _validate_moves(s, user: CurrentUser, body: MovesIn) -> tuple[dict, dict, list[Move]]:
    _check_sub(user, body.subaccount_id)
    if not body.moves:
        raise HTTPException(422, "No moves")
    strat = {st.id: st for st in _strategies(s, body.subaccount_id)}
    legs = {lg["id"]: lg for lg in _load_legs(s, body.subaccount_id)}
    seen: dict[int, Move] = {}
    for m in body.moves:
        if m.leg_id not in legs:
            raise HTTPException(422, f"Leg {m.leg_id} is not in this account")
        if m.to_strategy_id not in strat:
            raise HTTPException(422, f"Box {m.to_strategy_id} is not in this account")
        seen[m.leg_id] = m                       # last move of a leg wins
    return strat, legs, list(seen.values())


def _impact(strat: dict, legs: dict, moves: list[Move]) -> list[dict]:
    before = _strategy_stats(list(legs.values()))
    moved = {m.leg_id: m.to_strategy_id for m in moves}
    after = _strategy_stats([{**lg, "strategy_id": moved.get(lg["id"], lg["strategy_id"])}
                             for lg in legs.values()])
    touched = {legs[m.leg_id]["strategy_id"] for m in moves} | {m.to_strategy_id for m in moves}
    blank = {"net_pnl_usd": 0.0, "n_deals": 0, "win_rate": None, "n_legs": 0}
    out = []
    for sid in touched:
        b, a = before.get(sid, blank), after.get(sid, blank)
        st = strat.get(sid)
        out.append({"strategy_id": sid, "name": st.name if st else "?",
                    "color": (st.color if st else None) or "#888888",
                    "before": {k: b[k] for k in blank}, "after": {k: a[k] for k in blank},
                    "delta_pnl_usd": round(a["net_pnl_usd"] - b["net_pnl_usd"], 2)})
    out.sort(key=lambda r: r["delta_pnl_usd"])
    return out


@router.post("/preview")
def preview(body: MovesIn, user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s:
        strat, legs, moves = _validate_moves(s, user, body)
    rows, noop = [], 0
    for m in moves:
        lg = legs[m.leg_id]
        same = lg["strategy_id"] == m.to_strategy_id and lg["strategy_source"] == "manual"
        noop += 1 if same else 0
        rows.append({"leg_id": lg["id"], "inst_id": lg["inst_id"], "status": lg["status"],
                     "from": {"id": lg["strategy_id"],
                              "name": strat[lg["strategy_id"]].name
                              if lg["strategy_id"] in strat else None},
                     "to": {"id": m.to_strategy_id, "name": strat[m.to_strategy_id].name},
                     "pnl_usd": lg["pnl_usd"], "noop": same,
                     "rows": {"trade_fill": lg["n_fills"], "position_snapshot": lg["n_snapshots"],
                              "closed_position": 1 if lg["status"] == "closed" else 0}})
    return {"moves": rows, "impact": _impact(strat, legs, moves), "noop": noop,
            "n_links": len(moves) - noop}


def _leg_key(s, leg_id: int) -> tuple[PositionLeg, tuple]:
    leg = s.get(PositionLeg, leg_id)
    if leg is None:
        raise HTTPException(422, f"Unknown leg {leg_id}")
    return leg, (leg.cex_code, leg.subaccount_id, leg.pos_id, leg.pos_opened_at)


def _write_link(s, user_id: int, leg: PositionLeg, current: StrategyLink | None, action: str,
                strategy_id: int | None, prev_strategy_id: int | None, changeset_id: str,
                reason: str, now: datetime) -> bool:
    """Supersede the leg's current link (if any) and append the new one. False if a no-op."""
    if current is not None and current.action == action and current.strategy_id == strategy_id:
        return False
    if current is None and action == UNPIN:
        return False                                   # already on rules
    if current is not None:
        current.superseded_at = now
        s.flush()                                      # free the partial unique index first
    s.add(StrategyLink(
        subaccount_id=leg.subaccount_id, cex_code=leg.cex_code, pos_id=leg.pos_id,
        pos_opened_at=leg.pos_opened_at, inst_id=leg.inst_id, action=action,
        strategy_id=strategy_id, prev_strategy_id=prev_strategy_id, changeset_id=changeset_id,
        reason=reason, created_by=user_id, created_at=now))
    s.flush()
    return True


def _audit(s, user_id: int, action: str, target: str) -> None:
    s.add(AuditLog(actor_user_id=user_id, action=action, target=target[:255]))


@router.post("/apply")
def apply(body: MovesIn, user: CurrentUser = Depends(require_admin)) -> dict:
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "A reason is required")
    changeset_id, now, written = str(uuid.uuid4()), datetime.now(timezone.utc), 0
    with SessionLocal() as s, s.begin():
        _strat, legs, moves = _validate_moves(s, user, body)
        links = _current_links(s, body.subaccount_id)
        for m in moves:
            leg, key = _leg_key(s, m.leg_id)
            written += _write_link(s, user.id, leg, links.get(key), PIN, m.to_strategy_id,
                                   legs[m.leg_id]["strategy_id"], changeset_id, reason, now)
        if written:
            _audit(s, user.id, "box_builder.apply",
                   f"changeset={changeset_id} subaccount={body.subaccount_id} pins={written}")
    if written:
        request_recompute()
    return {"changeset_id": changeset_id if written else None, "written": written,
            "recompute": _rc_status()}


@router.post("/reset")
def reset(body: ResetIn, user: CurrentUser = Depends(require_admin)) -> dict:
    """'Reset to rule': unpin legs so strategy_rule / unassigned decide again."""
    _check_sub(user, body.subaccount_id)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(422, "A reason is required")
    changeset_id, now, written = str(uuid.uuid4()), datetime.now(timezone.utc), 0
    with SessionLocal() as s, s.begin():
        legs = {lg["id"]: lg for lg in _load_legs(s, body.subaccount_id)}
        links = _current_links(s, body.subaccount_id)
        for leg_id in body.leg_ids:
            if leg_id not in legs:
                raise HTTPException(422, f"Leg {leg_id} is not in this account")
            leg, key = _leg_key(s, leg_id)
            written += _write_link(s, user.id, leg, links.get(key), UNPIN, None,
                                   legs[leg_id]["strategy_id"], changeset_id, reason, now)
        if written:
            _audit(s, user.id, "box_builder.reset",
                   f"changeset={changeset_id} subaccount={body.subaccount_id} unpins={written}")
    if written:
        request_recompute()
    return {"changeset_id": changeset_id if written else None, "written": written,
            "recompute": _rc_status()}


@router.post("/undo")
def undo(body: UndoIn, user: CurrentUser = Depends(require_admin)) -> dict:
    """Revert a changeset: each of its links that is still current goes back to the state before
    it (the previous pin, or the rules). Links already replaced by a later change are skipped."""
    changeset_id, now = str(uuid.uuid4()), datetime.now(timezone.utc)
    written, skipped, restored = 0, [], None
    with SessionLocal() as s, s.begin():
        rows = s.execute(select(StrategyLink).where(
            StrategyLink.changeset_id == body.changeset_id)).scalars().all()
        box = s.execute(select(Strategy).where(
            Strategy.deleted_changeset_id == body.changeset_id)).scalar_one_or_none()
        if not rows and box is None:
            raise HTTPException(404, "Unknown changeset")
        sub_id = rows[0].subaccount_id if rows else box.subaccount_id
        _check_sub(user, sub_id)
        reason = f"Undo of changeset {body.changeset_id[:8]}" + (
            f": {body.reason.strip()}" if body.reason and body.reason.strip() else "")
        if box is not None and box.deleted_at is not None:
            # undo of "box deleted": bring the box (and so its rules) back before re-pinning legs
            if _name_taken(s, sub_id, box.name):
                box.name = (box.name[:53] + " (restored)")
            box.deleted_at = box.deleted_by = box.deleted_changeset_id = None
            restored = box.name
            s.flush()
        deleted_ids = {st.id for st in _strategies(s, sub_id, include_deleted=True)
                       if st.deleted_at is not None}
        for r in rows:
            if r.superseded_at is not None:
                skipped.append(r.inst_id)
                continue
            prev = s.execute(select(StrategyLink).where(
                StrategyLink.cex_code == r.cex_code, StrategyLink.subaccount_id == r.subaccount_id,
                StrategyLink.pos_id == r.pos_id, StrategyLink.pos_opened_at == r.pos_opened_at,
                StrategyLink.id < r.id).order_by(StrategyLink.id.desc()).limit(1)
            ).scalar_one_or_none()
            leg = s.execute(select(PositionLeg).where(
                PositionLeg.cex_code == r.cex_code, PositionLeg.subaccount_id == r.subaccount_id,
                PositionLeg.pos_id == r.pos_id, PositionLeg.pos_opened_at == r.pos_opened_at)
            ).scalar_one_or_none()
            if leg is None:
                skipped.append(r.inst_id)
                continue
            if prev is not None and prev.action == PIN:
                action, sid = PIN, prev.strategy_id
                if sid in deleted_ids:              # can't pin back into a deleted box
                    skipped.append(r.inst_id)
                    continue
            else:
                action, sid = UNPIN, None
            cur_sid = r.strategy_id if r.action == PIN else leg.strategy_id
            # _write_link treats "same action + strategy" as a no-op; force the revert row here
            r.superseded_at = now
            s.flush()
            s.add(StrategyLink(
                subaccount_id=r.subaccount_id, cex_code=r.cex_code, pos_id=r.pos_id,
                pos_opened_at=r.pos_opened_at, inst_id=r.inst_id, action=action, strategy_id=sid,
                prev_strategy_id=cur_sid, changeset_id=changeset_id, reason=reason,
                created_by=user.id, created_at=now))
            s.flush()
            written += 1
        if written or restored:
            _audit(s, user.id, "box_builder.undo",
                   f"changeset={changeset_id} undoes={body.changeset_id} rows={written}"
                   + (f" restored_box={restored}" if restored else ""))
    if written or restored:
        request_recompute()
    return {"changeset_id": changeset_id if written else None, "written": written,
            "skipped": skipped, "restored_box": restored, "recompute": _rc_status()}


@router.post("/strategies")
def create_strategy(body: StrategyIn, user: CurrentUser = Depends(require_admin)) -> dict:
    """Create a strategy ("box" in the GUI) in the account. Lives in the DB only — add it to
    seed/strategy.csv to keep it in git."""
    _check_sub(user, body.subaccount_id)
    name = _valid_name(body.name)
    color = body.color if body.color and _HEX.match(body.color) else "#9b7be0"
    with SessionLocal() as s, s.begin():
        if _name_taken(s, body.subaccount_id, name):
            raise HTTPException(409, f"Box '{name}' already exists in this account")
        st = Strategy(subaccount_id=body.subaccount_id, name=name, color=color,
                      description=(body.description or "").strip() or None)
        s.add(st)
        s.flush()
        _audit(s, user.id, "box_builder.create_strategy",
               f"strategy={st.id} subaccount={body.subaccount_id} name={name}")
        return {"id": st.id, "name": st.name, "color": st.color}


@router.put("/strategies/{strategy_id}")
def update_strategy(strategy_id: int, body: StrategyEditIn,
                    user: CurrentUser = Depends(require_admin)) -> dict:
    """Edit a box: name, color, description. Legs, pins and rules are untouched; reports read the
    name live from core.strategy, so no recompute is needed."""
    with SessionLocal() as s, s.begin():
        st = _active_box(s, user, strategy_id)
        if st.name == UNASSIGNED:
            raise HTTPException(422, f"The '{UNASSIGNED}' box can't be edited")
        name = _valid_name(body.name)
        if _name_taken(s, st.subaccount_id, name, except_id=st.id):
            raise HTTPException(409, f"Box '{name}' already exists in this account")
        old = st.name
        st.name = name
        if body.color and _HEX.match(body.color):
            st.color = body.color
        st.description = (body.description or "").strip() or None
        _audit(s, user.id, "box_builder.update_strategy",
               f"strategy={st.id} subaccount={st.subaccount_id} name={old}->{name}")
        return {"id": st.id, "name": st.name, "color": st.color, "description": st.description}


@router.delete("/strategies/{strategy_id}")
def delete_strategy(strategy_id: int, user: CurrentUser = Depends(require_admin)) -> dict:
    """Delete a box: pin every leg it holds to the account's `unassigned` box (one changeset), then
    soft-delete it (its rules stop applying). Undo of the changeset in History restores the box."""
    changeset_id, now, moved = str(uuid.uuid4()), datetime.now(timezone.utc), 0
    with SessionLocal() as s, s.begin():
        st = _active_box(s, user, strategy_id)
        if st.name == UNASSIGNED:
            raise HTTPException(422, f"The '{UNASSIGNED}' box can't be deleted")
        unassigned = s.execute(select(Strategy).where(
            Strategy.subaccount_id == st.subaccount_id, Strategy.name == UNASSIGNED,
            Strategy.deleted_at.is_(None))).scalar_one_or_none()
        if unassigned is None:
            raise HTTPException(409, f"This account has no '{UNASSIGNED}' box to move the legs to")
        legs = _load_legs(s, st.subaccount_id)
        links = _current_links(s, st.subaccount_id)
        reason = f"Box '{st.name}' deleted — its legs moved to {UNASSIGNED}"
        for lg in legs:
            if lg["strategy_id"] != st.id:             # effective box (pending pins included)
                continue
            leg, key = _leg_key(s, lg["id"])
            moved += _write_link(s, user.id, leg, links.get(key), PIN, unassigned.id, st.id,
                                 changeset_id, reason, now)
        n_rules = s.execute(select(func.count()).select_from(StrategyRule).where(
            StrategyRule.strategy_id == st.id)).scalar_one()
        st.deleted_at, st.deleted_by, st.deleted_changeset_id = now, user.id, changeset_id
        _audit(s, user.id, "box_builder.delete_strategy",
               f"strategy={st.id} name={st.name} changeset={changeset_id} legs={moved} "
               f"rules_disabled={n_rules}")
        name = st.name
    request_recompute()
    return {"changeset_id": changeset_id, "name": name, "moved": moved,
            "rules_disabled": n_rules, "recompute": _rc_status()}
