"""Position legs: fill→leg matching (posId isn't on fills)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.legs import LegWindow, match_fill_to_leg

T0 = datetime(2026, 8, 29, 8, 6, 35, tzinfo=timezone.utc)


def test_opening_fill_at_ctime_matches():
    w = [LegWindow(1, T0, T0 + timedelta(days=1))]
    assert match_fill_to_leg(T0, w) == 1
    assert match_fill_to_leg(T0 + timedelta(milliseconds=1), w) == 1   # observed OKX jitter


def test_fill_outside_every_window_is_unmatched():
    w = [LegWindow(1, T0, T0 + timedelta(hours=4))]
    assert match_fill_to_leg(T0 - timedelta(minutes=5), w) is None
    assert match_fill_to_leg(T0 + timedelta(hours=5), w) is None


def test_reused_posid_two_lifecycles_pick_by_time():
    # same instrument (and same OKX posId) closed early and reopened: two legs, two windows
    first = LegWindow(1, T0, T0 + timedelta(hours=4))
    second = LegWindow(2, T0 + timedelta(hours=6), None)              # still open
    assert match_fill_to_leg(T0 + timedelta(hours=4), [first, second]) == 1   # closing fill
    assert match_fill_to_leg(T0 + timedelta(hours=6), [first, second]) == 2   # re-opening fill
    assert match_fill_to_leg(T0 + timedelta(days=3), [first, second]) == 2


def test_flip_fill_goes_to_the_leg_it_opens():
    flip = T0 + timedelta(hours=2)
    w = [LegWindow(1, T0, flip), LegWindow(2, flip, None)]
    assert match_fill_to_leg(flip, w) == 2
