"""Geometry of "Price with strategy boxes": leg lines, boxes, total open size, candle size."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.boxes import Leg, auto_bar, build_boxes, exposure_segments, line_end, open_until

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def h(n: float) -> datetime:
    return T0 + timedelta(hours=n)


def leg(i, strike, opened, expires, status="closed", closed=None, strat=1, sub=1, size=1.0,
        coin_size=0.01, uly="BTC-USD", seen=None, pnl=None) -> Leg:
    return Leg(leg_id=i, subaccount_id=sub, strategy_id=strat, underlying=uly, strike=strike,
               opened_at=opened, expires_at=expires, status=status, closed_at=closed,
               last_seen_at=seen, size=size, size_coin=coin_size, coin="BTC", pnl_usd=pnl)


def test_line_runs_from_open_to_expiry_even_when_closed_early():
    lg = leg(1, 60000, h(0), h(240), closed=h(10))
    assert line_end(lg) == h(240)                      # D1: the line ends at the expiry
    no_exp = leg(2, 60000, h(0), None, closed=h(10))
    assert line_end(no_exp) == h(10)                   # not an option: falls back to the close


def test_box_covers_all_legs_lines_of_different_length():
    legs = [leg(1, 60000, h(0), h(240)), leg(2, 68000, h(0), h(240)),
            leg(3, 65000, h(30), h(100), status="open")]          # shorter inner line
    (b,) = build_boxes(legs)
    assert (b.started_at, b.ends_at) == (h(0), h(240))
    assert (b.strike_lo, b.strike_hi) == (60000, 68000)            # border on the outer lines
    assert b.n_legs == 3 and b.n_open_legs == 1 and b.status == "open"
    assert b.leg_ids == [1, 2, 3]


def test_box_label_is_the_sum_of_all_legs():
    legs = [leg(1, 65000, h(0), h(24), size=1, coin_size=0.01),
            leg(2, 65000, h(0), h(24), size=2, coin_size=0.02)]     # straddle, 1 + 2 lots
    (b,) = build_boxes(legs)
    assert b.size_contracts == 3 and abs(b.size_coin - 0.03) < 1e-12   # D2: sum, not max
    assert b.strike_lo == b.strike_hi == 65000          # D5: zero height, no special case


def test_unknown_contract_size_gives_no_coin_total():
    legs = [leg(1, 65000, h(0), h(24)), leg(2, 66000, h(0), h(24), coin_size=None)]
    (b,) = build_boxes(legs)
    assert b.size_coin is None and b.size_contracts == 2


def test_boxes_are_per_account_strategy_and_underlying():
    legs = [leg(1, 65000, h(0), h(24), strat=1), leg(2, 65000, h(0), h(24), strat=2),
            leg(3, 65000, h(0), h(24), strat=1, sub=2), leg(4, 3000, h(0), h(24), uly="ETH-USD"),
            leg(5, None, h(0), h(24))]                               # no strike: not drawable
    keys = sorted((b.subaccount_id, b.strategy_id, b.underlying) for b in build_boxes(legs))
    assert keys == [(1, 1, "BTC-USD"), (1, 1, "ETH-USD"), (1, 2, "BTC-USD"), (2, 1, "BTC-USD")]


def test_box_pnl_sums_known_legs():
    (b,) = build_boxes([leg(1, 1, h(0), h(1), pnl=10.0), leg(2, 2, h(0), h(1), pnl=-4.0),
                        leg(3, 3, h(0), h(1))])
    assert b.net_pnl_usd == 6.0


def test_open_until():
    assert open_until(leg(1, 1, h(0), h(240), closed=h(10)), NOW) == h(10)
    assert open_until(leg(1, 1, h(0), h(24 * 30), status="open"), NOW) == NOW
    # an "open" leg past its expiry stops counting at the expiry (close not collected yet)
    assert open_until(leg(1, 1, h(0), h(24), status="open"), NOW) == h(24)
    assert open_until(leg(1, 1, h(0), h(240), status="stale", seen=h(50)), NOW) == h(50)


def test_exposure_segments_overlap_merge_and_gaps():
    legs = [leg(1, 1, h(0), h(24), closed=h(24)),                    # 0.01 for a day
            leg(2, 1, h(12), h(36), closed=h(36)),                   # overlaps 12 h → 0.02
            leg(3, 1, h(48), h(72), closed=h(72))]                   # after a 12 h gap
    segs = [(s.t0, s.t1, round(s.total, 6)) for s in exposure_segments(legs, NOW)]
    assert segs == [(h(0), h(12), 0.01), (h(12), h(24), 0.02), (h(24), h(36), 0.01),
                    (h(36), h(48), 0.0), (h(48), h(72), 0.01)]


def test_exposure_merges_equal_neighbours_and_skips_unsized():
    legs = [leg(1, 1, h(0), h(24), closed=h(24)), leg(2, 1, h(24), h(48), closed=h(48)),
            leg(3, 1, h(0), h(48), closed=h(48), coin_size=None)]
    segs = exposure_segments(legs, NOW)
    # the roll at 24 h is no change
    assert [(s.t0, s.t1, s.total) for s in segs] == [(h(0), h(48), 0.01)]


def test_auto_bar():
    assert auto_bar(T0, T0 + timedelta(days=7)) == "1h"
    assert auto_bar(T0, T0 + timedelta(days=60)) == "4h"
    assert auto_bar(T0, T0 + timedelta(days=200)) == "1d"


def test_closed_early():
    from app.domain.boxes import closed_early
    early = leg(1, 1, h(0), h(24), closed=h(5))
    early.close_type = "close"
    assert closed_early(early)
    at_expiry = leg(2, 1, h(0), h(24), closed=h(24.01))            # traded/settled after expiry
    at_expiry.close_type = "close"
    assert not closed_early(at_expiry)
    expired = leg(3, 1, h(0), h(24), closed=h(5))
    expired.close_type = "expiry"
    assert not closed_early(expired)
    assert not closed_early(leg(4, 1, h(0), h(24), status="open"))


def test_in_period_overlap():
    from app.domain.boxes import in_period
    lg = leg(1, 1, h(10), h(34))                                    # line 10 h → 34 h
    assert in_period(lg, None, None)
    assert in_period(lg, h(30), h(40)) and in_period(lg, h(0), h(12))
    assert in_period(lg, h(34), None) and in_period(lg, None, h(10))  # touching ends count
    assert not in_period(lg, h(35), None) and not in_period(lg, None, h(9))


def test_boxes_from_a_period_cover_only_its_legs():
    from app.domain.boxes import in_period
    legs = [leg(1, 60000, h(0), h(24)), leg(2, 70000, h(100), h(124)),
            leg(3, 65000, h(110), h(130))]
    (b,) = build_boxes([g for g in legs if in_period(g, h(105), None)])
    assert (b.strike_lo, b.strike_hi, b.n_legs) == (65000, 70000, 2)
    assert abs(b.size_coin - 0.02) < 1e-12
