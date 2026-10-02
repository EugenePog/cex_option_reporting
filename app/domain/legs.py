"""Position legs: fill→leg matching (pure functions, no DB).

A *leg* is one OKX position lifecycle, keyed by (cex_code, subaccount_id, posId, cTime). Fills carry
no posId, but in OKX net mode an instrument has at most one open position at a time, so a fill
belongs to the leg of the same (subaccount, inst_id) whose [cTime, uTime] window contains the fill
time. The leg is the only unit the Box builder shows and moves (no combo grouping).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

# OKX reports the opening fill at cTime (observed: same millisecond, at most +1 ms) and the closing
# fill at uTime; allow a little slack on both ends.
FILL_WINDOW_TOLERANCE = timedelta(seconds=1)


@dataclass(frozen=True)
class LegWindow:
    leg_id: int
    opened_at: datetime            # cTime
    closed_at: datetime | None     # uTime of the closed lifecycle; None while open


def match_fill_to_leg(filled_at: datetime, windows: Iterable[LegWindow],
                      tolerance: timedelta = FILL_WINDOW_TOLERANCE) -> int | None:
    """Return the id of the leg whose [opened_at, closed_at] window contains `filled_at`.

    `windows` must be the legs of the fill's own (subaccount, inst_id). If two windows match — a
    single fill that flips the position through zero closes one leg and opens the next at the same
    instant — the most recently opened leg wins (the fill opens it). None if nothing matches (e.g.
    the leg's lifecycle is not in bronze yet).
    """
    hits = [w for w in windows
            if w.opened_at - tolerance <= filled_at
            and (w.closed_at is None or filled_at <= w.closed_at + tolerance)]
    if not hits:
        return None
    return max(hits, key=lambda w: w.opened_at).leg_id
