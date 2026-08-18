"""Bronze -> Silver transform.

Reads raw bronze rows, resolves each to a core.subaccount (via cex_code + account_label +
subacct_name), parses inst_ids, joins greeks onto positions, tags positions/fills/closed-positions
with a strategy, and upserts typed rows into the silver tables. Idempotent: safe to re-run.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import session_scope
from app.db.models import (
    RawBalance,
    RawClosedPosition,
    RawMargin,
    RawOptSummary,
    RawPosition,
    RawTradeFill,
)
from app.db.models_core import CexAccount, Strategy, StrategyRule, Subaccount
from app.db.models_silver import (
    BalanceSnapshot,
    ClosedPosition,
    MarginSnapshot,
    PositionSnapshot,
    TradeFill,
)
from app.domain.instruments import parse_inst_id
from app.domain.strategy_rules import Rule, TagRecord, assign_strategy

logger = logging.getLogger(__name__)


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
    """Resolves bronze rows to subaccounts and strategies."""

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
        self.rules: dict[int, list[Rule]] = {}
        for r in session.execute(select(StrategyRule)).scalars():
            self.rules.setdefault(r.subaccount_id, []).append(
                Rule(strategy_id=r.strategy_id, priority=r.priority, match_json=r.match_json or {})
            )
        self.unassigned: dict[int, int] = {}
        for s in session.execute(
            select(Strategy).where(Strategy.name == "unassigned")
        ).scalars():
            self.unassigned[s.subaccount_id] = s.id

    def resolve_subaccount(self, cex_code: str, account_label: str, subacct_name: str) -> int | None:
        return self.subaccount.get((cex_code, account_label, subacct_name or ""))

    def tag(self, subaccount_id: int, rec: TagRecord) -> int | None:
        return assign_strategy(
            self.rules.get(subaccount_id, []), rec, self.unassigned.get(subaccount_id)
        )


def _upsert(session, model, values: dict, index_elements: list[str]) -> None:
    stmt = pg_insert(model).values(**values)
    update = {c: stmt.excluded[c] for c in values if c not in index_elements and c != "id"}
    session.execute(stmt.on_conflict_do_update(index_elements=index_elements, set_=update))


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


def _transform_positions(session, lk: _Lookups) -> tuple[int, int]:
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
        side = "short" if (size or 0) < 0 else ("long" if (size or 0) > 0 else "flat")
        g = greeks.get((pos.ingest_id, inst_id), {})
        strategy_id = lk.tag(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type, side=side,
        ))
        _upsert(session, PositionSnapshot, {
            "cex_code": pos.cex_code, "subaccount_id": sub_id, "strategy_id": strategy_id,
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


def _transform_fills(session, lk: _Lookups) -> tuple[int, int]:
    written = skipped = 0
    for f in session.execute(select(RawTradeFill)).scalars():
        sub_id = lk.resolve_subaccount(f.cex_code, f.account_label, f.subacct_name)
        if sub_id is None:
            skipped += 1
            continue
        p = f.payload
        inst_id = p.get("instId", "")
        parsed = parse_inst_id(inst_id)
        okx_side = (p.get("side") or "").lower()          # buy | sell
        direction = "long" if okx_side == "buy" else ("short" if okx_side == "sell" else None)
        filled_at = _ts(p.get("ts") or p.get("fillTime"))
        strategy_id = lk.tag(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=direction, opened_at=filled_at,
        ))
        _upsert(session, TradeFill, {
            "cex_code": f.cex_code, "subaccount_id": sub_id, "strategy_id": strategy_id,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "side": okx_side,
            "size": _f(p.get("fillSz")), "price": _f(p.get("fillPx")),
            "fee": _f(p.get("fee")), "fee_ccy": p.get("feeCcy"),
            "realized_pnl": _f(p.get("fillPnl")), "trade_id": f.trade_id,
            "filled_at": filled_at, "ingest_id": f.ingest_id,
        }, ["cex_code", "trade_id"])
        written += 1
    return written, skipped


def _transform_closed(session, lk: _Lookups) -> tuple[int, int]:
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
        opened_at = _ts(p.get("cTime"))
        closed_at = _ts(p.get("uTime")) or c.captured_at
        strategy_id = lk.tag(sub_id, TagRecord(
            inst_id=inst_id, underlying=parsed.underlying, opt_type=parsed.opt_type,
            side=side, opened_at=opened_at,
        ))
        _upsert(session, ClosedPosition, {
            "cex_code": c.cex_code, "subaccount_id": sub_id, "strategy_id": strategy_id,
            "inst_id": inst_id, "underlying": parsed.underlying, "opt_type": parsed.opt_type,
            "strike": parsed.strike, "expiry": parsed.expiry, "close_type": p.get("type"),
            "side": side, "open_avg_px": _f(p.get("openAvgPx")),
            "close_avg_px": _f(p.get("closeAvgPx")), "realized_pnl": _f(p.get("realizedPnl")),
            "pnl": _f(p.get("pnl")), "fee": _f(p.get("fee")), "ccy": p.get("ccy"),
            "opened_at": opened_at, "closed_at": closed_at, "ext_id": c.ext_id,
            "ingest_id": c.ingest_id,
        }, ["cex_code", "ext_id"])
        written += 1
    return written, skipped


def run() -> dict[str, tuple[int, int]]:
    """Run all bronze->silver transforms. Returns {table: (written, skipped_unresolved)}."""
    with session_scope() as s:
        lk = _Lookups(s)
        if not lk.subaccount:
            logger.warning("no subaccounts found in core — seed core data first (make seed); "
                           "all bronze rows will be skipped")
        results = {
            "balance_snapshot": _transform_balances(s, lk),
            "margin_snapshot": _transform_margin(s, lk),
            "position_snapshot": _transform_positions(s, lk),
            "trade_fill": _transform_fills(s, lk),
            "closed_position": _transform_closed(s, lk),
        }
    for table, (written, skipped) in results.items():
        logger.info("silver %s: %d written, %d skipped (unresolved subaccount)",
                    table, written, skipped)
    return results
