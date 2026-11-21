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

Index candles (market data) are copied set-based and incrementally: only bronze.raw_index_candle
rows with an id above the highest silver.index_candle.bronze_id (see _transform_index_candles).

Build 15 (speed; same results):
  * every bronze table is read ONCE, in id order, and only the payload fields the transform uses
    are sent from Postgres (payload->>'field'); greeks (raw_opt_summary) are loaded only for the
    snapshot runs that have positions;
  * rows are written in bulk instead of one statement per row: per table, all rows are streamed
    with COPY into a temporary staging table and merged with ONE INSERT … SELECT … ON CONFLICT
    (measured: ~10× faster than multi-row VALUES batches of 1,000 rows, which spent most of their
    time building the huge statements in Python). Duplicate keys inside a table are collapsed
    first: the newest bronze row (highest id) wins;
  * ON CONFLICT … DO UPDATE … WHERE (columns) IS DISTINCT FROM (new values): an unchanged row is
    not rewritten (no dead tuples, no index / WAL churn). position_leg is the exception — it is
    small and its updated_at is the Box builder's "as of" time, so it is refreshed every run;
  * source_fingerprint() lets the pipeline loop skip the whole stage when nothing it reads has
    changed (see app/pipelines/runner.py).

Build 17 (alerts): a snapshot run that is STILL BEING WRITTEN (bronze.ingest_run status RUNNING,
started less than IN_PROGRESS_MAX ago) is left out of raw_position and of "the newest snapshot
run". The collector writes positions, then balances, then margin, then greeks, each in its own
transaction; before, a pipeline run that came between them saw the new positions but the previous
balance run as the newest one, marked every leg "stale", the box looked closed to the alerts worker
for one pipeline cycle — its price alerts were not checked and got re-armed (repeated messages).
Now such a run is picked up by the next pipeline run, once it has finished (its finish changes the
source fingerprint). A run still RUNNING after IN_PROGRESS_MAX (a crashed collector) counts as
finished again, so it can never freeze the legs; a run that ended in ERROR counts, as before.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import cache
from typing import Any, cast

from sqlalchemy import Float, Numeric, Table, func, select, text

from app.db.base import session_scope
from app.db.models import (
    IngestRun,
    RawBalance,
    RawBill,
    RawClosedPosition,
    RawIndexCandle,
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
from app.domain.instruments import ParsedInstrument, parse_inst_id
from app.domain.legs import LegWindow, match_fill_to_leg
from app.domain.strategy_rules import Resolution, Rule, TagRecord, resolve_strategy

logger = logging.getLogger(__name__)

LegKey = tuple[str, int, str, datetime]   # (cex_code, subaccount_id, posId, cTime)

# A RUNNING snapshot run younger than this is "still being written" and left out (build 17). A run
# takes seconds; one still RUNNING after this is a crashed collector and counts as finished.
IN_PROGRESS_MAX_MINUTES = 10
_IN_PROGRESS_SQL = (f"SELECT ingest_id FROM bronze.ingest_run WHERE status = 'RUNNING' "
                    f"AND started_at > now() - interval '{IN_PROGRESS_MAX_MINUTES} minutes'")



def _q(name: str) -> str:
    """Quote an identifier (silver column / constraint names are plain lower-case words)."""
    return '"' + name.replace('"', '""') + '"'


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


def _or(val: Any, default: Any) -> Any:
    """`payload.get(key, default)` for a field read with ->> (a missing key arrives as None)."""
    return default if val is None else val


@cache
def _parse(inst_id: str) -> ParsedInstrument:
    """parse_inst_id, cached — the same few instruments repeat in every snapshot."""
    return parse_inst_id(inst_id)


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


# --------------------------------------------------------------------------- #
# Reading bronze (build 15: each table once, only the payload fields used)
# --------------------------------------------------------------------------- #
def _read(session, model, fields: Sequence[str] = (), *, json_fields: Sequence[str] = (),
          extra: Sequence = (), where=None) -> list:
    """Rows of a bronze table in id order: the common columns, `extra` columns, and each payload
    field as text (`p_<field>`, = payload->>'<field>') or as JSON (`j_<field>`)."""
    cols = [model.id, model.cex_code, model.account_label, model.subacct_name, model.ingest_id,
            model.captured_at, *extra]
    cols += [model.payload[f].astext.label(f"p_{f}") for f in fields]
    cols += [model.payload[f].label(f"j_{f}") for f in json_fields]
    q = select(*cols).order_by(model.id)
    if where is not None:
        q = q.where(where)
    return session.execute(q).all()


_POS_FIELDS = ("posId", "cTime", "instId", "pos", "upl", "fee", "idxPx", "avgPx", "ccy", "markPx",
               "notionalUsd", "optVal", "deltaBS", "gammaBS", "thetaBS", "vegaBS")
_GREEK_FIELDS = ("instId", "markPx", "fwdPx", "delta", "gamma", "theta", "vega", "markVol")
_CLOSED_FIELDS = ("cTime", "instId", "direction", "openMaxPos", "closeTotalPos", "openAvgPx",
                  "closeAvgPx", "uTime", "type", "realizedPnl", "pnl", "fee", "ccy")
_FILL_FIELDS = ("ts", "fillTime", "instId", "side", "fillSz", "fillPx", "fee", "feeCcy", "fillPnl")
_BILL_FIELDS = ("instId", "type", "subType", "px", "pnl", "fee", "ccy", "ts")
_BAL_FIELDS = ("ccy", "eq", "availEq", "availBal", "eqUsd")
_MARGIN_FIELDS = ("imr", "mmr", "mgnRatio", "totalEq", "ccy", "eqUsd")


@dataclass
class _Bronze:
    positions: list
    closed: list
    fills: list
    in_progress: frozenset[str] = frozenset()     # ingest_ids of runs still being written


def _in_progress_runs(session) -> frozenset[str]:
    return frozenset(session.execute(text(_IN_PROGRESS_SQL)).scalars())


def _read_bronze(session) -> _Bronze:
    running = _in_progress_runs(session)     # same snapshot as the reads below (run())
    return _Bronze(
        positions=[r for r in _read(session, RawPosition, _POS_FIELDS)
                   if r.ingest_id not in running],
        in_progress=running,
        closed=_read(session, RawClosedPosition, _CLOSED_FIELDS,
                     extra=(RawClosedPosition.ext_id, RawClosedPosition.pos_opened_at)),
        fills=_read(session, RawTradeFill, _FILL_FIELDS,
                    extra=(RawTradeFill.inst_id, RawTradeFill.trade_id)),
    )


# --------------------------------------------------------------------------- #
# Writing silver (build 15: bulk COPY + one merge per table, unchanged rows untouched)
# --------------------------------------------------------------------------- #
def _dedupe(rows: list[dict], key: Sequence[str], nulls_conflict: bool = False) -> list[dict]:
    """Collapse rows sharing a conflict key; the LAST one wins (Postgres refuses to update one row
    twice in a statement). Rows come in bronze-id order, so the newest bronze row wins — before
    build 15 rows were upserted one by one in the table's physical order, which is the id order in
    practice but not guaranteed. A key holding NULL never conflicts in Postgres unless the
    constraint is NULLS NOT DISTINCT (`nulls_conflict`), so such rows are all kept."""
    uniq: dict[tuple, dict] = {}
    loose: list[dict] = []
    for r in rows:
        k = tuple(r[c] for c in key)
        if not nulls_conflict and any(v is None for v in k):
            loose.append(r)
        else:
            uniq.pop(k, None)          # re-insert so the dict order follows the last occurrence
            uniq[k] = r
    return loose + list(uniq.values())


@dataclass
class _MergeSql:
    stage: str          # temp staging table
    create: str         # CREATE TEMP TABLE … (same columns; numeric staged as double precision)
    copy: str           # COPY … FROM STDIN
    merge: str          # INSERT … SELECT … ON CONFLICT … DO UPDATE … [WHERE … IS DISTINCT FROM …]


def _merge_sql(table, cols: Sequence[str], key: Sequence[str], constraint: str | None = None,
               compare: bool = True) -> _MergeSql:
    """SQL for one bulk upsert of `cols` into `table` through a staging table."""
    upd = [c for c in cols if c not in key and c != "id"]
    # Numeric columns are staged as double precision: the values are Python floats, and float8 →
    # numeric is exactly the conversion the row-by-row upserts (float parameters) went through.
    as_float = {c.name for c in table.columns
                if isinstance(c.type, Numeric) and not isinstance(c.type, Float)}
    stage = f"_stage_{table.name}"
    col_list = ", ".join(_q(c) for c in cols)
    stage_cols = ", ".join(f"{_q(c)}::double precision AS {_q(c)}" if c in as_float else _q(c)
                           for c in cols)
    target = f"ON CONSTRAINT {_q(constraint)}" if constraint else f"({', '.join(map(_q, key))})"
    merge = (f"INSERT INTO {table.fullname} AS t ({col_list}) SELECT {col_list} FROM {stage} "
             f"ON CONFLICT {target} DO UPDATE SET "
             + ", ".join(f"{_q(c)} = EXCLUDED.{_q(c)}" for c in upd))
    if compare and upd:
        merge += (f" WHERE ({', '.join('t.' + _q(c) for c in upd)}) IS DISTINCT FROM "
                  f"({', '.join('EXCLUDED.' + _q(c) for c in upd)})")
    return _MergeSql(
        stage=stage,
        create=(f"CREATE TEMP TABLE {stage} ON COMMIT DROP AS "
                f"SELECT {stage_cols} FROM {table.fullname} WITH NO DATA"),
        copy=f"COPY {stage} ({col_list}) FROM STDIN",
        merge=merge,
    )


def _upsert_many(session, model, rows: list[dict], key: Sequence[str], *,
                 constraint: str | None = None, nulls_conflict: bool = False,
                 compare: bool = True) -> int:
    """Bulk INSERT … ON CONFLICT DO UPDATE; returns the rows inserted or changed.

    `key` = the conflict columns (duplicates collapsed by _dedupe). All rows are streamed with COPY
    into a temporary staging table and merged with one statement. With `compare`, a row whose
    values are all unchanged is not rewritten (and not counted)."""
    if not rows:
        return 0
    rows = _dedupe(rows, key, nulls_conflict)
    cols = list(rows[0])
    sql = _merge_sql(model.__table__, cols, key, constraint, compare)
    # psycopg's own connection — the session's, so this is the same transaction
    raw = session.connection().connection.driver_connection
    with raw.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {sql.stage}")
        cur.execute(sql.create)
        with cur.copy(sql.copy) as copy:
            for r in rows:
                copy.write_row([r[c] for c in cols])
        cur.execute(sql.merge)
        changed = cur.rowcount
        cur.execute(f"DROP TABLE {sql.stage}")
    return max(changed, 0)


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


def _latest_snapshot_runs(session, lk: _Lookups,
                          in_progress: frozenset[str] = frozenset()) -> dict[int, str]:
    """subaccount_id -> ingest_id of its most recent snapshot run (balances are written on every
    run, positions only when something is open — so the run is identified via raw_balance).
    Runs still being written (build 17) don't count."""
    latest: dict[int, tuple[datetime, str]] = {}
    rows = session.execute(
        select(RawBalance.cex_code, RawBalance.account_label, RawBalance.subacct_name,
               RawBalance.ingest_id, func.max(RawBalance.captured_at))
        .group_by(RawBalance.cex_code, RawBalance.account_label, RawBalance.subacct_name,
                  RawBalance.ingest_id)
    ).all()
    for cex, label, sub_name, ingest_id, cap in rows:
        sub_id = lk.resolve_subaccount(cex, label, sub_name)
        if sub_id is None or cap is None or ingest_id in in_progress:
            continue
        if sub_id not in latest or cap > latest[sub_id][0]:
            latest[sub_id] = (cap, ingest_id)
    return {k: v[1] for k, v in latest.items()}


def _collect_legs(session, lk: _Lookups, br: _Bronze) -> dict[LegKey, _Leg]:
    legs: dict[LegKey, _Leg] = {}

    # open side: every snapshot row contributes; the latest one sets the current state
    for pos in br.positions:
        sub_id = lk.resolve_subaccount(pos.cex_code, pos.account_label, pos.subacct_name)
        pos_id, c_time = pos.p_posId or "", _ts(pos.p_cTime)
        if sub_id is None or not pos_id or c_time is None:
            continue
        key = (pos.cex_code, sub_id, str(pos_id), c_time)
        leg = legs.get(key) or legs.setdefault(key, _Leg(key=key, inst_id=_or(pos.p_instId, "")))
        leg.n_snapshots += 1
        if leg.last_seen_at is None or pos.captured_at >= leg.last_seen_at:
            size = _f(pos.p_pos)
            leg.last_seen_at, leg.last_ingest_id = pos.captured_at, pos.ingest_id
            leg.upl, leg.fee, leg.idx_px = _f(pos.p_upl), _f(pos.p_fee), _f(pos.p_idxPx)
            leg.side = _pos_side(size)
            leg.size = abs(size) if size is not None else None
            leg.entry_px = _f(pos.p_avgPx)
            leg.ccy = pos.p_ccy or leg.ccy

    latest_run = _latest_snapshot_runs(session, lk, br.in_progress)
    for leg in legs.values():
        if leg.last_ingest_id is not None and latest_run.get(leg.key[1]) == leg.last_ingest_id:
            leg.status = "open"

    # closed side: positions-history rows close (and fully describe) their leg
    for c in br.closed:
        sub_id = lk.resolve_subaccount(c.cex_code, c.account_label, c.subacct_name)
        c_time = c.pos_opened_at or _ts(c.p_cTime)
        if sub_id is None or not c.ext_id or c_time is None:
            continue
        key = (c.cex_code, sub_id, c.ext_id, c_time)
        leg = legs.get(key) or legs.setdefault(key, _Leg(key=key, inst_id=_or(c.p_instId, "")))
        leg.status = "closed"
        leg.side = (c.p_direction or "").lower() or leg.side
        leg.size = _f(c.p_openMaxPos) or _f(c.p_closeTotalPos) or leg.size
        leg.entry_px = _f(c.p_openAvgPx)
        leg.exit_px = _f(c.p_closeAvgPx)
        leg.closed_at = _ts(c.p_uTime) or c.captured_at
        leg.close_type = c.p_type
        leg.realized_pnl = _f(c.p_realizedPnl)
        leg.fee = _f(c.p_fee)
        leg.ccy = c.p_ccy or leg.ccy
        leg.upl = None
    return legs


def _upsert_legs(session, lk: _Lookups, legs: dict[LegKey, _Leg], fill_counts: dict) -> int:
    now = datetime.now(timezone.utc)
    rows = []
    for leg in legs.values():
        cex, sub_id, pos_id, c_time = leg.key
        parsed = _parse(leg.inst_id)
        leg.res, leg.link_id = lk.resolve_leg(leg.key, TagRecord(
            inst_id=leg.inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=leg.side, opened_at=c_time,
        ))
        leg.n_fills = fill_counts.get(leg.key, 0)
        rows.append({
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
        })
    # few rows, and updated_at is the Box builder's "as of" → always refreshed (compare=False)
    changed = _upsert_many(session, PositionLeg, rows,
                           ["cex_code", "subaccount_id", "pos_id", "pos_opened_at"], compare=False)
    ids = {(r.cex_code, r.subaccount_id, r.pos_id, r.pos_opened_at): r.id for r in session.execute(
        select(PositionLeg.id, PositionLeg.cex_code, PositionLeg.subaccount_id, PositionLeg.pos_id,
               PositionLeg.pos_opened_at))}
    for leg in legs.values():
        leg.id = ids.get(leg.key)
    return changed


def _fill_leg_index(
        legs: dict[LegKey, _Leg]) -> dict[tuple[str, int, str], list[tuple[LegKey, LegWindow]]]:
    """(cex_code, subaccount_id, inst_id) -> [(leg key, its time window)]."""
    idx: dict[tuple[str, int, str], list[tuple[LegKey, LegWindow]]] = defaultdict(list)
    for i, leg in enumerate(legs.values()):
        cex, sub_id, _pos, c_time = leg.key
        idx[(cex, sub_id, leg.inst_id)].append(
            (leg.key, LegWindow(leg_id=i, opened_at=c_time, closed_at=leg.closed_at)))
    return idx


def _match_fills(lk: _Lookups, legs: dict[LegKey, _Leg], br: _Bronze) -> dict[int, LegKey | None]:
    """raw_trade_fill.id -> leg key (or None when no leg window contains the fill)."""
    idx = _fill_leg_index(legs)
    out: dict[int, LegKey | None] = {}
    for f in br.fills:
        sub_id = lk.resolve_subaccount(f.cex_code, f.account_label, f.subacct_name)
        if sub_id is None:
            continue
        filled_at = _ts(f.p_ts or f.p_fillTime)
        cands = idx.get((f.cex_code, sub_id, f.inst_id or _or(f.p_instId, "")), [])
        hit = match_fill_to_leg(filled_at, [w for _k, w in cands]) if filled_at else None
        out[f.id] = (next((k for k, w in cands if w.leg_id == hit), None)
                     if hit is not None else None)
    return out


# --------------------------------------------------------------------------- #
# Transforms — each returns (rows written, rows skipped, rows inserted or changed)
# --------------------------------------------------------------------------- #
def _transform_balances(session, lk: _Lookups) -> tuple[int, int, int]:
    rows, skipped = [], 0
    for b in _read(session, RawBalance, _BAL_FIELDS):
        sub_id = lk.resolve_subaccount(b.cex_code, b.account_label, b.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        rows.append({
            "cex_code": b.cex_code, "subaccount_id": sub_id,
            "ccy": b.p_ccy, "total": _f(b.p_eq),
            "available": _f(b.p_availEq or b.p_availBal),
            "usd_value": _f(b.p_eqUsd),
            "captured_at": b.captured_at, "ingest_id": b.ingest_id,
        })
    changed = _upsert_many(session, BalanceSnapshot, rows, ["subaccount_id", "ccy", "captured_at"])
    return len(rows), skipped, changed


def _transform_margin(session, lk: _Lookups) -> tuple[int, int, int]:
    rows, skipped = [], 0
    for m in _read(session, RawMargin, _MARGIN_FIELDS, json_fields=("details",)):
        sub_id = lk.resolve_subaccount(m.cex_code, m.account_label, m.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        if m.p_imr not in (None, "") and m.p_mgnRatio not in (None, ""):
            details = m.j_details or []
            eq_usd = (sum((_f(d.get("eqUsd")) or 0) for d in details) if details
                      else _f(m.p_totalEq))
            scope, imr, mmr, ratio = "ACCOUNT", _f(m.p_imr), _f(m.p_mmr), _f(m.p_mgnRatio)
        else:
            scope = m.p_ccy or "ACCOUNT"
            eq_usd, imr, mmr, ratio = (_f(m.p_eqUsd), _f(m.p_imr), _f(m.p_mmr), _f(m.p_mgnRatio))
        rows.append({
            "cex_code": m.cex_code, "subaccount_id": sub_id, "scope": scope,
            "eq_usd": eq_usd, "imr_usd": imr, "mmr_usd": mmr, "margin_ratio": ratio,
            "captured_at": m.captured_at, "ingest_id": m.ingest_id,
        })
    changed = _upsert_many(session, MarginSnapshot, rows, ["subaccount_id", "scope", "captured_at"])
    return len(rows), skipped, changed


def _load_greeks(session) -> dict[tuple[str, str | None], Any]:
    """Greeks by (ingest_id, instId) — only for snapshot runs that have positions."""
    runs = select(RawPosition.ingest_id).distinct()
    return {(o.ingest_id, o.p_instId): o for o in _read(
        session, RawOptSummary, _GREEK_FIELDS, where=RawOptSummary.ingest_id.in_(runs))}


def _transform_positions(session, lk: _Lookups, legs: dict[LegKey, _Leg],
                         br: _Bronze) -> tuple[int, int, int]:
    greeks = _load_greeks(session)
    rows, skipped = [], 0
    for pos in br.positions:
        sub_id = lk.resolve_subaccount(pos.cex_code, pos.account_label, pos.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        inst_id = _or(pos.p_instId, "")
        parsed = _parse(inst_id)
        size = _f(pos.p_pos)
        side = _pos_side(size)
        g = greeks.get((pos.ingest_id, inst_id))
        pos_id, c_time = (pos.p_posId or None), _ts(pos.p_cTime)
        leg = legs.get((pos.cex_code, sub_id, str(pos_id), c_time)) if pos_id and c_time else None
        res = leg.res if leg is not None else lk.resolve_unkeyed(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type, side=side))
        rows.append({
            "cex_code": pos.cex_code, "subaccount_id": sub_id, "strategy_id": res.strategy_id,
            "strategy_source": res.source, "pos_id": pos_id, "pos_opened_at": c_time,
            "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": side, "size": size,
            "avg_px": _f(pos.p_avgPx),
            "mark_px": _f(pos.p_markPx or (g.p_markPx if g is not None else None)),
            "idx_px": _f(pos.p_idxPx), "fwd_px": _f(g.p_fwdPx if g is not None else None),
            "upl": _f(pos.p_upl), "fee": _f(pos.p_fee),
            "notional_usd": _f(pos.p_notionalUsd), "opt_val": _f(pos.p_optVal),
            # coin greeks (opt-summary) + Black-Scholes dollar greeks (positions)
            "delta": _f(g.p_delta if g is not None else None),
            "gamma": _f(g.p_gamma if g is not None else None),
            "theta": _f(g.p_theta if g is not None else None),
            "vega": _f(g.p_vega if g is not None else None),
            "iv": _f(g.p_markVol if g is not None else None),
            "delta_bs": _f(pos.p_deltaBS), "gamma_bs": _f(pos.p_gammaBS),
            "theta_bs": _f(pos.p_thetaBS), "vega_bs": _f(pos.p_vegaBS),
            "captured_at": pos.captured_at, "ingest_id": pos.ingest_id,
        })
    changed = _upsert_many(session, PositionSnapshot, rows,
                           ["subaccount_id", "inst_id", "side", "captured_at"])
    return len(rows), skipped, changed


def _transform_fills(session, lk: _Lookups, legs: dict[LegKey, _Leg],
                     fill_legs: dict[int, LegKey | None], br: _Bronze) -> tuple[int, int, int]:
    """Fills carry no strategy of their own — only the link to their leg (position_leg_id)."""
    rows, skipped = [], 0
    for f in br.fills:
        sub_id = lk.resolve_subaccount(f.cex_code, f.account_label, f.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        inst_id = f.inst_id or _or(f.p_instId, "")
        parsed = _parse(inst_id)
        okx_side = (f.p_side or "").lower()          # buy | sell
        leg_key = fill_legs.get(f.id)
        leg = legs.get(leg_key) if leg_key else None
        rows.append({
            "cex_code": f.cex_code, "subaccount_id": sub_id,
            "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": okx_side,
            "size": _f(f.p_fillSz), "price": _f(f.p_fillPx),
            "fee": _f(f.p_fee), "fee_ccy": f.p_feeCcy,
            "realized_pnl": _f(f.p_fillPnl), "trade_id": f.trade_id,
            "filled_at": _ts(f.p_ts or f.p_fillTime), "ingest_id": f.ingest_id,
        })
    changed = _upsert_many(session, TradeFill, rows, ["cex_code", "inst_id", "trade_id"])
    return len(rows), skipped, changed


def _transform_closed(session, lk: _Lookups, legs: dict[LegKey, _Leg],
                      br: _Bronze) -> tuple[int, int, int]:
    rows, skipped = [], 0
    for c in br.closed:
        sub_id = lk.resolve_subaccount(c.cex_code, c.account_label, c.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        inst_id = _or(c.p_instId, "")
        parsed = _parse(inst_id)
        side = (c.p_direction or "").lower() or None
        opened_at = c.pos_opened_at or _ts(c.p_cTime)
        closed_at = _ts(c.p_uTime) or c.captured_at
        leg = legs.get((c.cex_code, sub_id, c.ext_id, opened_at)) if opened_at else None
        res = leg.res if leg is not None else lk.resolve_unkeyed(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=side, opened_at=opened_at))
        rows.append({
            "cex_code": c.cex_code, "subaccount_id": sub_id, "strategy_id": res.strategy_id,
            "strategy_source": res.source, "position_leg_id": leg.id if leg is not None else None,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "close_type": c.p_type,
            "side": side, "size": _f(c.p_closeTotalPos), "open_avg_px": _f(c.p_openAvgPx),
            "close_avg_px": _f(c.p_closeAvgPx), "realized_pnl": _f(c.p_realizedPnl),
            "pnl": _f(c.p_pnl), "fee": _f(c.p_fee), "ccy": c.p_ccy,
            "opened_at": opened_at, "closed_at": closed_at, "ext_id": c.ext_id,
            "ingest_id": c.ingest_id,
        })
    # uq_silver_closed_position is NULLS NOT DISTINCT → rows with a NULL in the key do conflict
    changed = _upsert_many(session, ClosedPosition, rows, ["cex_code", "ext_id", "opened_at"],
                           constraint="uq_silver_closed_position", nulls_conflict=True)
    return len(rows), skipped, changed


def _transform_bills(s, lk: _Lookups) -> tuple[int, int, int]:
    rows, skipped = [], 0
    for b in _read(s, RawBill, _BILL_FIELDS, extra=(RawBill.bill_id,)):
        sub_id = lk.resolve_subaccount(b.cex_code, b.account_label, b.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        inst_id = b.p_instId or None
        parsed = _parse(inst_id or "")
        rows.append({
            "cex_code": b.cex_code, "subaccount_id": sub_id, "bill_id": b.bill_id,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry,
            "bill_type": b.p_type, "sub_type": b.p_subType,
            "px": _f(b.p_px), "pnl": _f(b.p_pnl), "fee": _f(b.p_fee),
            "ccy": b.p_ccy, "billed_at": _ts(b.p_ts), "ingest_id": b.ingest_id,
        })
    changed = _upsert_many(s, Bill, rows, ["cex_code", "bill_id"])
    return len(rows), skipped, changed


_NUM = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"   # a plain decimal string, as OKX sends prices


def _transform_index_candles(s) -> tuple[int, int, int]:
    """bronze.raw_index_candle → silver.index_candle (typed OHLC), new bronze rows only.

    Watermark = max(silver.index_candle.bronze_id): bronze ids only grow, so a minute the collector
    back-fills later (a hole) arrives with a higher id and is still picked up. One INSERT … SELECT —
    no rows go through Python. Rows whose o/h/l/c are not numbers are skipped (counted)."""
    wm = s.execute(text("SELECT COALESCE(MAX(bronze_id), 0) FROM silver.index_candle")).scalar_one()
    pending = s.execute(text(
        "SELECT count(*) FROM bronze.raw_index_candle WHERE id > :wm"), {"wm": wm}).scalar_one()
    if not pending:
        return 0, 0, 0
    written = s.execute(text("""
        INSERT INTO silver.index_candle
               (cex_code, inst_id, bar, ts, open, high, low, close, bronze_id, ingest_id)
        SELECT b.cex_code, b.inst_id, b.bar, b.ts,
               (b.payload->>1)::numeric, (b.payload->>2)::numeric,
               (b.payload->>3)::numeric, (b.payload->>4)::numeric, b.id, b.ingest_id
          FROM bronze.raw_index_candle b
         WHERE b.id > :wm
           AND jsonb_typeof(b.payload) = 'array'
           AND b.payload->>1 ~ :num AND b.payload->>2 ~ :num
           AND b.payload->>3 ~ :num AND b.payload->>4 ~ :num
        ON CONFLICT ON CONSTRAINT uq_silver_index_candle DO UPDATE
           SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
               close = EXCLUDED.close, bronze_id = EXCLUDED.bronze_id,
               ingest_id = EXCLUDED.ingest_id
    """), {"wm": wm, "num": _NUM}).rowcount
    return written, pending - written, written


# --------------------------------------------------------------------------- #
# What silver is built from — lets the pipeline loop skip a run that would change nothing
# --------------------------------------------------------------------------- #
_BRONZE_TABLES = (RawPosition, RawBalance, RawMargin, RawOptSummary, RawTradeFill,
                  RawClosedPosition, RawBill, RawIndexCandle)
_CORE_TABLES = (CexAccount, Subaccount, Strategy, StrategyRule, StrategyLink)


def source_fingerprint() -> tuple:
    """Everything bronze → silver reads, in one cheap query: the newest id of every bronze table
    (bronze is append-only), the ingest runs (count + latest finish, so a run that commits its rows
    late still counts) and a hash of the core rows used for subaccounts and strategy tagging
    (accounts, sub-accounts, boxes, rules, Box builder pins). Same fingerprint ⇒ a silver run would
    write exactly the same rows."""
    def name(model) -> str:
        return cast(Table, model.__table__).fullname

    parts = [f"(SELECT max(id) FROM {name(m)})" for m in _BRONZE_TABLES]
    run = name(IngestRun)
    parts.append(f"(SELECT count(*) || '/' || coalesce(max(finished_at)::text, '') FROM {run})")
    # build 17: the runs left out as "still being written" — changes when one finishes or when a
    # stuck one passes IN_PROGRESS_MAX_MINUTES and counts again
    parts.append(f"(SELECT coalesce(string_agg(ingest_id, ',' ORDER BY ingest_id), '') "
                 f"FROM ({_IN_PROGRESS_SQL}) r)")
    parts += [f"(SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
              f"FROM {name(m)} t)" for m in _CORE_TABLES]
    with session_scope() as s:
        return tuple(s.execute(text("SELECT " + ", ".join(parts))).one())


# --------------------------------------------------------------------------- #
def run() -> dict[str, tuple[int, int]]:
    """Run all bronze->silver transforms. Returns {table: (written, skipped_unresolved)}."""
    t0 = time.perf_counter()
    changed: dict[str, int] = {}
    with session_scope() as s:
        # build 17: one snapshot of the database for every read of this run — the in-progress
        # runs, raw_position and raw_balance (the newest run) are then consistent with each other
        # even while a collector commits. Only the pipeline writes silver (under its lock), so
        # REPEATABLE READ can't hit a write conflict here.
        s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        lk = _Lookups(s)
        if not lk.subaccount:
            logger.warning("no subaccounts found in core — seed core data first (make seed); "
                           "all bronze rows will be skipped")
        br = _read_bronze(s)        # raw_position / raw_closed_position / raw_trade_fill, once
        # legs first: every position-type row (snapshot / closed / fill) hangs off a leg
        legs = _collect_legs(s, lk, br)
        fill_legs = _match_fills(lk, legs, br)
        fill_counts: dict[LegKey, int] = defaultdict(int)
        for k in fill_legs.values():
            if k is not None:
                fill_counts[k] += 1
        changed["position_leg"] = _upsert_legs(s, lk, legs, fill_counts)
        steps = {
            "balance_snapshot": lambda: _transform_balances(s, lk),
            "margin_snapshot": lambda: _transform_margin(s, lk),
            "position_leg": lambda: (len(legs), 0, changed["position_leg"]),
            "position_snapshot": lambda: _transform_positions(s, lk, legs, br),
            "trade_fill": lambda: _transform_fills(s, lk, legs, fill_legs, br),
            "closed_position": lambda: _transform_closed(s, lk, legs, br),
            "bill": lambda: _transform_bills(s, lk),
            "index_candle": lambda: _transform_index_candles(s),
        }
        results: dict[str, tuple[int, int]] = {}
        for table, step in steps.items():
            written, skipped, changed[table] = step()
            results[table] = (written, skipped)
        unlinked = sum(1 for k in fill_legs.values() if k is None)
        if unlinked:
            logger.info("silver trade_fill: %d fill(s) not matched to a position leg yet", unlinked)
        pinned = sum(1 for lg in legs.values() if lg.link_id is not None)
        logger.info("silver position_leg: %d legs (%d pinned manually)", len(legs), pinned)
    for table, (written, skipped) in results.items():
        logger.info("silver %s: %d written (%d new or changed), %d skipped (%s)", table, written,
                    changed[table], skipped,
                    "not numeric" if table == "index_candle" else "unresolved subaccount")
    logger.info("silver done in %.1f s", time.perf_counter() - t0)
    return results
