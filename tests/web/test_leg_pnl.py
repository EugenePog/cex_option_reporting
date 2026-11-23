"""build 24 — expiry by time, and a leg's / box's P&L for graph ①'s hover: premium − fees (opening)
+ closing (settlement / buy-backs / value now), coin and USD."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.pipelines.silver_to_gold import _close_type
from app.web.reports import _leg_pnl, _pnl_sum

EXP = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)


def approx(x):
    return pytest.approx(x, abs=1e-9)


def test_expiry_is_a_close_within_5_minutes_of_the_expiry():
    d = date(2026, 11, 19)
    assert _close_type(EXP + timedelta(seconds=28.9), d) == "expiry"     # the 65000-P of 19 Nov
    assert _close_type(EXP - timedelta(minutes=5), d) == "expiry"
    assert _close_type(EXP + timedelta(minutes=5), d) == "expiry"
    assert _close_type(EXP + timedelta(minutes=5, seconds=1), d) == "close"
    assert _close_type(EXP - timedelta(hours=3), d) == "close"           # bought back that morning
    assert _close_type(None, d) == "close" and _close_type(EXP, None) == "close"


def _leg(**kw):
    base = dict(status="closed", close_type="expiry", side="short", size_coin=0.01,
                entry_px=0.0105, upl=None, premium_net_coin=0.000102, premium_net_usd=6.5,
                close_net_coin=-0.000224, close_net_usd=-14.0)
    return SimpleNamespace(**{**base, **kw})


def test_closed_leg_pnl_is_premium_plus_settlement():
    p = _leg_pnl(_leg(), 60_000)
    assert p["coin"] == approx(-0.000122) and p["usd"] == approx(-7.5)
    assert p["close_kind"] == "settlement"
    assert _leg_pnl(_leg(close_type="close"), None)["close_kind"] == "closing"


def test_open_leg_adds_its_value_now_at_the_current_price():
    # short 0.01 sold at avgPx 0.0105, mark 0.004 → upl = (0.0105 − 0.004) × 0.01 = 0.000065;
    # value now = −0.004 × 0.01 = −0.00004 BTC, × 60,000 = −2.4 USD
    p = _leg_pnl(_leg(status="open", close_type=None, upl=0.000065, premium_net_coin=0.000105,
                      premium_net_usd=6.6, close_net_coin=0.0, close_net_usd=0.0), 60_000)
    assert p["close_kind"] == "value now"
    assert p["close_coin"] == approx(-0.00004) and p["close_usd"] == approx(-2.4)
    assert p["coin"] == approx(0.000065) and p["usd"] == approx(4.2)
    assert _leg_pnl(_leg(status="open", upl=None), 60_000) is None
    assert _leg_pnl(_leg(status="stale"), 60_000) is None
    assert _leg_pnl(_leg(premium_net_coin=None), 60_000) is None


def test_box_pnl_sums_its_legs():
    a, b = _leg_pnl(_leg(), 1.0), _leg_pnl(_leg(close_type="close"), 1.0)
    s = _pnl_sum([a, b, None])
    assert s["coin"] == approx(-0.000244) and s["usd"] == approx(-15.0)
    assert s["premium_usd"] == approx(13.0) and s["n_missing"] == 1
    assert s["close_kind"] == "settlement + closing"
    assert _pnl_sum([a, a])["close_kind"] == "settlement"
    assert _pnl_sum([None]) is None
