"""Geometry of the dashboard chart "Price with strategy boxes". Pure, no I/O.

  * Each option leg is a LINE at its strike, from its open time to its expiry (08:00 UTC). Lines of
    one box can have different lengths. A leg CLOSED EARLY (traded close before expiry) is solid up
    to the close, marked with a dot there, and dashed from the close to the expiry.
  * A BOX (= strategy) is the rectangle that covers all its legs of one underlying: left = first
    leg opened, right = last line end, bottom / top = lowest / highest strike. The border sits
    exactly on the outer lines — no padding.
  * The box LABEL is the total size of its legs (Σ contracts × ct_val, in coin).
  * Under the candles, the TOTAL OPEN SIZE over time: a leg counts from its open time until it is
    closed (or until now while open), so early closes show up even though the line runs to expiry.
  * A REPORTING PERIOD keeps the legs whose line overlaps it; the boxes are then drawn around those
    legs only (same rule), so a box never covers strikes of legs that are not shown.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class Leg:
    leg_id: int                     # silver.position_leg.id
    subaccount_id: int
    strategy_id: int | None
    underlying: str | None
    strike: float | None
    opened_at: datetime
    expires_at: datetime | None     # expiry 08:00 UTC
    status: str                     # open | closed | stale
    closed_at: datetime | None = None
    last_seen_at: datetime | None = None
    size: float | None = None       # contracts
    size_coin: float | None = None  # contracts × ct_val (None when ct_val is unknown)
    coin: str | None = None
    pnl_usd: float | None = None    # realized (closed) or unrealized (open)
    close_type: str | None = None   # 'close' (traded) | 'expiry' — closed legs only


def line_end(leg: Leg) -> datetime:
    """Right end of the leg's line: its expiry; without one (not an option) the close, else the
    last snapshot, else the open time."""
    return leg.expires_at or leg.closed_at or leg.last_seen_at or leg.opened_at


def closed_early(leg: Leg) -> bool:
    """True for a leg closed by a trade before its expiry: drawn solid to the close, a dot at the
    close, dashed from there to the expiry."""
    return (leg.status == "closed" and leg.close_type != "expiry" and leg.closed_at is not None
            and leg.expires_at is not None and leg.closed_at < leg.expires_at)


def in_period(leg: Leg, lo: datetime | None, hi: datetime | None) -> bool:
    """Does the leg's line [open, line end] overlap the period [lo, hi]? Open bounds = unbounded."""
    return (hi is None or leg.opened_at <= hi) and (lo is None or line_end(leg) >= lo)


def open_until(leg: Leg, now: datetime) -> datetime:
    """Until when the leg counts in the total open size: the close time; `now` while open (but not
    past its expiry — an option can't be open after it expired, even if the close hasn't been
    collected yet); the last snapshot for a leg that vanished without a close yet (stale). Never
    later than now."""
    if leg.status == "closed" and leg.closed_at is not None:
        end = leg.closed_at
    elif leg.status == "open":
        end = min(now, leg.expires_at) if leg.expires_at else now
    else:
        end = leg.last_seen_at or leg.closed_at or leg.opened_at
    return min(end, now)


@dataclass
class Box:
    subaccount_id: int
    strategy_id: int | None
    underlying: str
    coin: str | None
    started_at: datetime
    ends_at: datetime
    strike_lo: float
    strike_hi: float
    n_legs: int
    n_open_legs: int
    size_contracts: float | None
    size_coin: float | None
    status: str                      # open | closed
    net_pnl_usd: float | None
    leg_ids: list[int] = field(default_factory=list)


def _sum_or_none(values: list[float | None]) -> float | None:
    """Σ of the values, or None if any is unknown (a partial total would mislabel the box)."""
    if not values or any(v is None for v in values):
        return None
    return float(sum(values))


def build_boxes(legs: list[Leg]) -> list[Box]:
    """One Box per (subaccount, strategy, underlying) over the legs that have a strike and an
    underlying (only those can be drawn)."""
    groups: dict[tuple, list[Leg]] = defaultdict(list)
    for lg in legs:
        if lg.strike is None or lg.underlying is None:
            continue
        groups[(lg.subaccount_id, lg.strategy_id, lg.underlying)].append(lg)

    out: list[Box] = []
    for (sub_id, strat_id, uly), grp in groups.items():
        grp.sort(key=lambda g: (g.opened_at, g.leg_id))
        strikes = [float(g.strike) for g in grp]
        pnls = [g.pnl_usd for g in grp if g.pnl_usd is not None]
        n_open = sum(1 for g in grp if g.status == "open")
        out.append(Box(
            subaccount_id=sub_id, strategy_id=strat_id, underlying=uly,
            coin=next((g.coin for g in grp if g.coin), None),
            started_at=min(g.opened_at for g in grp),
            ends_at=max(line_end(g) for g in grp),
            strike_lo=min(strikes), strike_hi=max(strikes),
            n_legs=len(grp), n_open_legs=n_open,
            size_contracts=_sum_or_none([g.size for g in grp]),
            size_coin=_sum_or_none([g.size_coin for g in grp]),
            status="open" if n_open else "closed",
            net_pnl_usd=float(sum(pnls)) if pnls else None,
            leg_ids=[g.leg_id for g in grp],
        ))
    out.sort(key=lambda b: (b.started_at, b.subaccount_id, b.strategy_id or 0))
    return out


@dataclass
class Segment:
    t0: datetime
    t1: datetime
    total: float                    # open size in coin


def exposure_segments(legs: list[Leg], now: datetime) -> list[Segment]:
    """Total open size (coin) between consecutive change points. Legs without a coin size are
    skipped; zero-total gaps are kept (they show as 0). Adjacent segments with the same total are
    merged, so every segment boundary is a real change."""
    events: dict[datetime, float] = defaultdict(float)
    for lg in legs:
        if lg.size_coin is None:
            continue
        end = open_until(lg, now)
        if end <= lg.opened_at:
            continue
        events[lg.opened_at] += float(lg.size_coin)
        events[end] -= float(lg.size_coin)
    if not events:
        return []
    times = sorted(events)
    segs: list[Segment] = []
    total = 0.0
    for t0, t1 in zip(times, times[1:], strict=False):
        total += events[t0]
        val = round(total, 10)
        if segs and abs(segs[-1].total - val) < 1e-12 and segs[-1].t1 == t0:
            segs[-1].t1 = t1
        else:
            segs.append(Segment(t0, t1, val))
    # trim leading/trailing zero segments (before the first open / after the last close)
    while segs and segs[0].total == 0:
        segs.pop(0)
    while segs and segs[-1].total == 0:
        segs.pop()
    return segs


BARS = ("1h", "4h", "1d")
BAR_STEP = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}


def auto_bar(t0: datetime, t1: datetime) -> str:
    """Candle size for a visible range: ≤ 14 days → 1h, ≤ 90 days → 4h, longer → 1d."""
    days = (t1 - t0).total_seconds() / 86400
    if days <= 14:
        return "1h"
    if days <= 90:
        return "4h"
    return "1d"
