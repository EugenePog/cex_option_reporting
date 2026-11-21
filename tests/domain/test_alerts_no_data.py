"""Build 17 — no-data notifications g / h: "⚠ alerts can't check ASSET: no price for X min"."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.domain.alerts import (
    CODE_INFO,
    Feed,
    FeedState,
    Setting,
    evaluate_no_data,
    format_no_data,
    threshold_range,
)

T0 = datetime(2026, 11, 19, 18, 0, tzinfo=UTC)      # time of the last fresh price
WARN = Setting("BTC-USD", "no_data_warning", threshold=10, enabled=True, critical=False)
ERR = Setting("BTC-USD", "no_data_error", threshold=30, enabled=True, critical=True)


def settings(warn=WARN, err=ERR):
    return {(s.asset, s.code): s for s in (warn, err) if s is not None}


def feed(last_at=T0, source="candle", usable=False, n=3):
    return Feed("OKX", "BTC-USD", last_at, source, usable, n)


class Chat:
    """Runs checks like the worker: sends every message, stores the states (all accepted)."""

    def __init__(self, sets=None):
        self.sets = sets or settings()
        self.states: dict = {}
        self.sent: list[str] = []

    def check(self, now, f):
        ev = evaluate_no_data([f], self.sets, self.states, now)
        self.states.update(ev.updates)
        for m in ev.messages:
            if m.state_key is not None:
                self.states[m.state_key] = m.new_state
            self.sent.append(m.message)
        return [m.code if m.state_key else "back" for m in ev.messages]


def test_fresh_price_sends_nothing():
    c = Chat()
    for minute in range(0, 120, 1):
        live = feed(T0 + timedelta(minutes=minute), "index", True)
        assert c.check(T0 + timedelta(minutes=minute, seconds=5), live) == []
    assert c.sent == []


def test_warning_then_error_once_each_then_one_back_message():
    c = Chat()
    stale = feed()                                           # last price at T0, nothing since
    got = {m: c.check(T0 + timedelta(minutes=m), stale) for m in range(0, 45)}
    assert [m for m, codes in got.items() if codes] == [10, 30]
    assert got[10] == ["no_data_warning"] and got[30] == ["no_data_error"]
    assert c.states[("OKX", "BTC-USD", "no_data_warning")].armed is False
    # the price is back → both re-arm, ONE "again" message (for the h that went out)
    back = c.check(T0 + timedelta(minutes=46), feed(T0 + timedelta(minutes=46), "index", True))
    assert back == ["back"]
    assert "alerts can check BTC-USD again" in c.sent[-1] and "#no_data_ok" in c.sent[-1]
    assert all(st.armed and not st.sent for st in c.states.values())
    # a second outage alerts again
    assert c.check(T0 + timedelta(minutes=56), feed(T0 + timedelta(minutes=46))) == \
        ["no_data_warning"]


def test_late_start_sends_only_the_error():
    c = Chat()                                               # worker was down, price 40 min old
    assert c.check(T0 + timedelta(minutes=40), feed()) == ["no_data_error"]
    assert c.states[("OKX", "BTC-USD", "no_data_warning")].armed is False   # h covered g
    assert c.check(T0 + timedelta(minutes=41), feed()) == []
    # back: one message, for h
    assert c.check(T0 + timedelta(minutes=42), feed(T0 + timedelta(minutes=42), "index", True)) \
        == ["back"]


def test_no_price_at_all_is_due_at_once():
    c = Chat()
    codes = c.check(T0, feed(last_at=None, source=None))
    assert codes == ["no_data_error"]
    assert "no price at all" in c.sent[0] and "no stored candle" in c.sent[0]


def test_switched_off_code_is_not_sent_and_does_not_block_the_other():
    c = Chat(settings(warn=Setting("BTC-USD", "no_data_warning", 10, enabled=False)))
    got = [m for m in range(0, 40) if c.check(T0 + timedelta(minutes=m), feed())]
    assert got == [30]
    c = Chat(settings(err=None))                             # only g configured
    got = [m for m in range(0, 40) if c.check(T0 + timedelta(minutes=m), feed())]
    assert got == [10]


def test_short_gap_below_the_threshold_sends_nothing():
    c = Chat()
    for m in range(0, 9):
        assert c.check(T0 + timedelta(minutes=m), feed()) == []
    assert c.check(T0 + timedelta(minutes=9), feed(T0 + timedelta(minutes=9), "index", True)) == []
    assert c.sent == []


def test_message_text():
    now = T0 + timedelta(minutes=12)
    msg = format_no_data(feed(usable=False), WARN, now)
    assert msg.startswith("🟡 <b>⚠ alerts can't check BTC-USD: no price for 12 min</b>")
    assert "Live OKX index price failing · newest stored candle 2026-11-19 18:00 UTC" in msg
    assert "Price alerts of 3 alive boxes are NOT checked until a price is back" in msg
    assert msg.endswith("Threshold 10 min · 2026-11-19 18:12 UTC · #no_data_warning")
    crit = format_no_data(feed(usable=True, n=1), ERR, T0 + timedelta(minutes=95))
    assert crit.startswith("🔴 <b>CRITICAL · ⚠ alerts can't check BTC-USD: no price for 1 h 35 min")
    assert "Price alerts of 1 alive box run on that old price" in crit


def test_minute_codes_have_their_own_threshold_range():
    assert threshold_range("no_data_warning") == (1, 1440)
    assert threshold_range("no_data_error") == (1, 1440)
    assert threshold_range("bottom_strike") == (0, 100)
    assert CODE_INFO["no_data_error"]["critical"] and not CODE_INFO["no_data_warning"]["critical"]


def test_failed_send_is_retried():
    """A message Telegram refused is not stored — the next check sends it again."""
    states: dict = {}
    now = T0 + timedelta(minutes=11)
    ev = evaluate_no_data([feed()], settings(), states, now)
    assert [m.code for m in ev.messages] == ["no_data_warning"]
    # not stored (refused) → same result one minute later
    ev2 = evaluate_no_data([feed()], settings(), states, now + timedelta(minutes=1))
    assert [m.code for m in ev2.messages] == ["no_data_warning"]
    assert FeedState().armed
