"""Telegram alerts for graph ① boxes: alive boxes, the six codes, once per crossing, messages."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.alerts import (
    AlertBox,
    Price,
    Setting,
    State,
    evaluate,
    format_message,
    is_alive,
    leg_open_at,
)
from app.domain.boxes import Leg

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
EXP = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)          # last expiry of the box
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
REARM, GRACE = timedelta(minutes=60), timedelta(hours=12)
CODES = ("top_strike", "bottom_strike", "near_top_pct", "near_bottom_pct", "last_expiry",
         "last_expiry_days")
THRESH = {"near_top_pct": 2, "near_bottom_pct": 2, "last_expiry_days": 1}
CRIT = {"top_strike", "bottom_strike", "last_expiry"}


def leg(i, strike, status="open", opened=T0, expires=EXP, closed=None, seen=None):
    return Leg(leg_id=i, subaccount_id=2, strategy_id=3, underlying="BTC-USD", strike=strike,
               opened_at=opened, expires_at=expires, status=status, closed_at=closed,
               last_seen_at=seen)


def box(legs=None, lo=60000, hi=68000, ends=EXP, name="Hedge box"):
    return AlertBox(subaccount_id=2, strategy_id=3, underlying="BTC-USD", cex_code="OKX",
                    name=name, account="OKX_M_HEDGE · Hedge", strike_lo=lo, strike_hi=hi,
                    ends_at=ends, legs=legs if legs is not None else [leg(1, lo), leg(2, hi)])


def settings(off=(), **thr):
    return {("BTC-USD", c): Setting("BTC-USD", c, float(thr.get(c, THRESH.get(c, 0))),
                                    enabled=c not in off, critical=c in CRIT) for c in CODES}


def px(v, at=NOW):
    return {("OKX", "BTC-USD"): Price(v, at)}


def run(b, price, now=NOW, states=None, **kw):
    return evaluate([b], settings(**kw), price, states or {}, now, REARM, GRACE)


def codes(ev):
    return [a.setting.code for a in ev.alerts]


# --------------------------------------------------------------------------- alive
def test_leg_open_at_and_alive():
    assert leg_open_at(leg(1, 1), NOW)
    assert not leg_open_at(leg(1, 1), EXP)                       # expired, close not collected
    assert not leg_open_at(leg(1, 1, status="closed", closed=NOW - timedelta(hours=1)), NOW)
    assert leg_open_at(leg(1, 1, status="closed", closed=NOW + timedelta(hours=1)),
                       NOW)                                      # closed later than t
    assert leg_open_at(leg(1, 1, status="stale", seen=NOW + timedelta(minutes=5)), NOW)
    assert not leg_open_at(leg(1, 1, opened=NOW + timedelta(hours=1)), NOW)
    assert is_alive(box(), NOW)
    closed = [leg(1, 60000, "closed", closed=NOW - timedelta(days=1)),
              leg(2, 68000, "closed", closed=NOW - timedelta(days=1))]
    assert not is_alive(box(closed), NOW)


def test_box_that_is_not_alive_gets_no_alert_and_is_rearmed():
    closed = [leg(1, 60000, "closed", closed=NOW - timedelta(days=1))]
    disarmed = {(2, 3, "BTC-USD", "top_strike"): State(armed=False, last_sent_at=NOW)}
    ev = run(box(closed), px(70000), states=disarmed)
    assert ev.alerts == []
    assert ev.updates[(2, 3, "BTC-USD", "top_strike")].armed is True


# --------------------------------------------------------------------------- price codes a–d
def test_top_strike_reached_is_critical_and_covers_near_top():
    ev = run(box(), px(68000.0))
    assert codes(ev) == ["top_strike"]
    a = ev.alerts[0]
    assert a.message.startswith("🔴 <b>CRITICAL · BTC-USD reached the top strike</b>")
    assert "Price 68,000.0 ≥ top strike 68,000" in a.message and "#top_strike" in a.message
    assert a.new_state.armed is False and a.new_state.last_value == 68000.0
    assert ev.updates[(2, 3, "BTC-USD", "near_top_pct")].armed is False   # c marked done


def test_near_top_when_top_is_switched_off():
    ev = run(box(), px(69000), off=("top_strike",))
    assert codes(ev) == ["near_top_pct"]


def test_near_top_pct_and_near_bottom_pct_levels():
    # top 68,000 − 2 % = 66,640 ; bottom 60,000 + 2 % = 61,200
    assert codes(run(box(), px(66639))) == []
    ev = run(box(), px(66640))
    assert codes(ev) == ["near_top_pct"]
    assert ev.alerts[0].message.startswith("🟡 <b>BTC-USD within 2% of the top strike</b>")
    assert "Price 66,640.0 ≥ 66,640 = top strike 68,000 − 2%" in ev.alerts[0].message
    assert codes(run(box(), px(61200))) == ["near_bottom_pct"]
    assert codes(run(box(), px(61201))) == []
    assert codes(run(box(), px(5000), near_top_pct=50)) == ["bottom_strike"]   # d covered by b


def test_bottom_strike_reached():
    ev = run(box(), px(59999.5))
    assert codes(ev) == ["bottom_strike"]
    assert "Price 59,999.5 ≤ bottom strike 60,000" in ev.alerts[0].message


def test_once_per_crossing_with_rearm_pause():
    key = (2, 3, "BTC-USD", "top_strike")
    sent = State(armed=False, last_sent_at=NOW - timedelta(minutes=10))
    assert codes(run(box(), px(69000), states={key: sent})) == []          # still above: quiet
    back = run(box(), px(64000), states={key: sent})                        # back below: re-arm
    assert back.updates[key].armed is True
    rearmed = State(armed=True, last_sent_at=NOW - timedelta(minutes=10))
    assert codes(run(box(), px(69000), states={key: rearmed})) == []        # too soon after
    later = run(box(), px(69000), now=NOW + timedelta(minutes=55), states={key: rearmed})
    assert codes(later) == ["top_strike"]


def test_no_price_means_no_price_alert():
    assert codes(run(box(), {("OKX", "BTC-USD"): None})) == []
    assert codes(run(box(), {})) == []


def test_switched_off_and_unconfigured_assets():
    assert codes(run(box(), px(69000), off=CODES)) == []
    eth = AlertBox(2, 3, "ETH-USD", "OKX", "x", "y", 2000, 3000, EXP, [leg(1, 2000)])
    assert evaluate([eth], settings(), {("OKX", "ETH-USD"): Price(5000, NOW)}, {}, NOW,
                    REARM, GRACE).alerts == []


def test_straddle_box_of_zero_height():
    b = box([leg(1, 65000), leg(2, 65000)], lo=65000, hi=65000)
    assert codes(run(b, px(65000))) == ["top_strike", "bottom_strike"]


def test_stored_candle_price_is_named_in_the_message():
    ev = run(box(), {("OKX", "BTC-USD"): Price(69000, NOW - timedelta(minutes=30), "candle")})
    assert "(last candle 11:30 UTC)" in ev.alerts[0].message


# --------------------------------------------------------------------------- time codes e–f
def test_last_expiry_days_once_per_expiry():
    before = EXP - timedelta(hours=20)
    ev = run(box(), px(64000), now=before)
    assert codes(ev) == ["last_expiry_days"]
    msg = ev.alerts[0].message
    assert msg.startswith("🟡 <b>Last expiry within 1 day</b>") and "in 20 h" in msg
    done = {(2, 3, "BTC-USD", "last_expiry_days"): ev.alerts[0].new_state}
    assert codes(run(box(), px(64000), now=before, states=done)) == []
    later = EXP + timedelta(days=1)
    rolled = box(ends=later, legs=[leg(1, 60000, expires=later)])
    assert codes(run(rolled, px(64000), now=EXP + timedelta(hours=5), states=done)) == \
        ["last_expiry_days"]                                      # a later expiry: again
    assert codes(run(box(), px(64000), now=EXP - timedelta(days=2))) == []   # too early


def test_last_expiry_reached():
    at = EXP + timedelta(minutes=1)
    ev = run(box(), px(64000), now=at)
    assert codes(ev) == ["last_expiry"]
    assert ev.alerts[0].message.startswith("🔴 <b>CRITICAL · Last expiry reached</b>")
    assert ev.alerts[0].new_state.fired_for_ends_at == EXP
    assert codes(run(box(), px(64000), now=EXP + GRACE)) == []              # too late


def test_last_expiry_not_sent_when_every_leg_closed_early():
    early = [leg(1, 60000, "closed", closed=EXP - timedelta(days=3)),
             leg(2, 68000, "closed", closed=EXP - timedelta(days=3))]
    assert codes(run(box(early), px(64000), now=EXP + timedelta(minutes=1))) == []
    settled = [leg(1, 60000, "closed", closed=EXP + timedelta(seconds=30))]  # closed by expiry
    assert codes(run(box(settled), px(64000), now=EXP + timedelta(minutes=1))) == \
        ["last_expiry"]


# --------------------------------------------------------------------------- message
def test_message_escapes_names():
    b = box(name="<Iron> & co")
    msg = format_message(b, settings()[("BTC-USD", "top_strike")], NOW,
                         price=Price(69000, NOW), level=68000)
    assert "<b>&lt;Iron&gt; &amp; co</b> · OKX_M_HEDGE · Hedge · BTC-USD" in msg
    assert "Box 60,000 – 68,000 · 2 legs, 2 open · last expiry 2026-09-25 08:00 UTC" in msg
    assert msg.endswith("2026-09-20 12:00 UTC · #top_strike")
