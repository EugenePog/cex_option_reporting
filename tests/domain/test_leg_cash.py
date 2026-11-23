"""Option cash in USD at operation-time prices (build 21) — app.domain.leg_cash, pure."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.leg_cash import (
    CashEvent,
    Fill,
    LegInfo,
    Snap,
    apply,
    fill_events,
    fills_cover,
    leg_cash,
)

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
H = timedelta(hours=1)
NO_PX = lambda at: None          # noqa: E731 — no candle needed when fills carry fillIdxPx


def approx(x):
    return pytest.approx(x, abs=1e-6)


def test_short_partial_close_from_fills_average_cost():
    # sell 2 @ 0.01 at 100k (fee −0.0003), buy back 1 @ 0.004 at 90k (fee −0.0002); 1 still open
    leg = LegInfo(status="open", side="short", opened_at=T0, size=1, fills=[
        Fill(T0, "sell", 2, 0.01, -0.0003, "BTC", 100_000),
        Fill(T0 + 5 * H, "buy", 1, 0.004, -0.0002, "BTC", 90_000)])
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=NO_PX)
    assert c.source == "fills" and c.missing_px == 0
    # cost of the 2 opened: +2000 premium − 30 fee = 1970; half closed: 985 + (−360 − 18)
    assert c.open_usd == approx(985) and c.realized_usd == approx(607)


def test_expiry_settlement_after_fills():
    # long put 1 @ 0.02 at 50k (fee −0.0001); settles paying 0.05 BTC at 40k →
    # realizedPnl = 0.05 − 0.02 − 0.0001 = 0.0299
    leg = LegInfo(status="closed", side="long", opened_at=T0, closed_at=T0 + 48 * H,
                  close_type="expiry", size=1, realized_pnl=0.0299,
                  fills=[Fill(T0, "buy", 1, 0.02, -0.0001, "BTC", 50_000)])
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=NO_PX, settle_px=40_000)
    assert c.source == "fills" and c.open_usd == 0
    assert c.realized_usd == approx(-1000 - 5 + 2000)


def test_summary_for_a_closed_leg_whole_fee_at_the_closing_price():
    # long call, entry 0.01 at 100k; closed at 110k with realizedPnl 0.004, fee −0.0005
    leg = LegInfo(status="closed", side="long", opened_at=T0, closed_at=T0 + 24 * H,
                  close_type="close", size=1, entry_px=0.01, fee=-0.0005, realized_pnl=0.004)
    px = {T0: 100_000, T0 + 24 * H: 110_000}
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=px.get)
    # open −1000; close cash = pnl 0.0045 − (−0.01) = 0.0145 → 1595; fee −55, all at 110k
    assert c.source == "summary" and c.realized_usd == approx(540) and c.open_usd == 0


def test_snapshot_replay_add_then_partial_close():
    snaps = [Snap(T0 + H, 2, 0.01, -0.0003, 0.0, idx_px=100_000),
             Snap(T0 + 2 * H, 3, 0.012, -0.0005, 0.0, idx_px=98_000),       # +1 sold
             Snap(T0 + 3 * H, 1, 0.012, -0.0007, 0.004, idx_px=90_000)]     # 2 bought back
    leg = LegInfo(status="open", side="short", opened_at=T0, size=1, snaps=snaps)
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at={T0: 99_000}.get)
    assert c.source == "snapshots"
    basis = (0.02 * 99_000 - 0.0003 * 99_000) + (0.016 * 98_000 - 0.0002 * 98_000)
    closing = -0.020 * 90_000 - 0.0002 * 90_000          # bought back 2 @ 0.010, fee
    assert c.realized_usd == approx(basis * 2 / 3 + closing)
    assert c.open_usd == approx(basis / 3)


def test_fills_used_only_when_they_cover_the_whole_position():
    late = [Fill(T0 + H, "sell", 1, 0.01, 0, "BTC", 100_000)]       # opened before our fills
    leg = LegInfo(status="open", side="short", opened_at=T0, size=1, fills=late,
                  snaps=[Snap(T0 + H, 1, 0.01, 0, 0, idx_px=100_000)])
    ev = fill_events(late, "short", 1.0, NO_PX)
    assert not fills_cover(ev, leg, 1.0)
    assert leg_cash(leg, 1.0, 1.0, {T0: 95_000}.get).source == "snapshots"
    # newest trade not collected yet: fills say 1, the position is 2 → snapshots
    leg2 = LegInfo(status="open", side="short", opened_at=T0, size=2,
                   fills=[Fill(T0, "sell", 1, 0.01, 0, "BTC", 100_000)],
                   snaps=[Snap(T0 + H, 2, 0.01, 0, 0, idx_px=100_000)])
    assert leg_cash(leg2, 1.0, 2.0, NO_PX).source == "snapshots"


def test_a_plus_b_equals_all_cash_of_the_leg():
    leg = LegInfo(status="open", side="long", opened_at=T0, size=1, fills=[
        Fill(T0, "buy", 3, 0.02, -0.0003, "BTC", 60_000),
        Fill(T0 + H, "sell", 1, 0.03, -0.0001, "BTC", 65_000),
        Fill(T0 + 2 * H, "sell", 1, 0.01, -0.0001, "BTC", 55_000)])
    c = leg_cash(leg, 1.0, 1.0, NO_PX)
    total = sum(e.cash * e.px + e.fee * e.px for e in fill_events(leg.fills, "long", 1.0, NO_PX))
    assert c.open_usd + c.realized_usd == approx(total)


def test_stable_fee_and_missing_price_and_stale_leg():
    ev = fill_events([Fill(T0, "sell", 1, 0.01, -2.5, "USDT", 100_000)], "short", 1.0, NO_PX)
    assert ev[0].fee_usd == -2.5 and ev[0].fee == 0
    assert apply([CashEvent(T0, 1.0, -0.01, px=None)]) == (0.0, 0.0, 1)
    assert leg_cash(LegInfo(status="stale", side="long", opened_at=T0), 1.0, 1.0, NO_PX) is None
    assert leg_cash(LegInfo(status="open", side="long", opened_at=T0), None, None, NO_PX) is None


# --------------------------------------------------------------------------- build 23
def test_premium_is_the_opening_side_closing_the_rest():
    # build 24 — the partial close above: sold 2 @ 0.01 (+0.02) at 100k with fee −0.0003 →
    # premium − fees +0.0197 BTC / +1970 USD; bought back 1 @ 0.004 at 90k, fee −0.0002 →
    # closing −0.0042 BTC / −378 USD
    leg = LegInfo(status="open", side="short", opened_at=T0, size=1, fills=[
        Fill(T0, "sell", 2, 0.01, -0.0003, "BTC", 100_000),
        Fill(T0 + 5 * H, "buy", 1, 0.004, -0.0002, "BTC", 90_000)])
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=NO_PX)
    assert c.premium_coin == approx(0.0197) and c.premium_usd == approx(1970)
    assert c.close_coin == approx(-0.0042) and c.close_usd == approx(-378)


def test_expiry_settlement_is_closing_not_premium():
    # long put bought 0.02 at 50k, fee −0.0001: premium − fees = −0.0201 BTC / −1005 USD; the 0.05
    # BTC the put paid at expiry is closing, at the settlement price 40k = +2000 USD
    leg = LegInfo(status="closed", side="long", opened_at=T0, closed_at=T0 + 48 * H,
                  close_type="expiry", size=1, realized_pnl=0.0299,
                  fills=[Fill(T0, "buy", 1, 0.02, -0.0001, "BTC", 50_000)])
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=NO_PX, settle_px=40_000)
    assert c.premium_coin == approx(-0.0201) and c.premium_usd == approx(-1005)
    assert c.close_coin == approx(0.05) and c.close_usd == approx(2000)
    assert c.premium_coin + c.close_coin == approx(0.0299)             # = OKX realizedPnl
    # summary source, expired short call: entry 0.01 at 100k, fee −0.0005 → with the opening (no
    # closing trade at an expiry): premium − fees 0.0095 BTC / 950 USD; the settlement −0.004 BTC
    # at 105k = −420 USD
    leg = LegInfo(status="closed", side="short", opened_at=T0, closed_at=T0 + 24 * H,
                  close_type="expiry", size=1, entry_px=0.01, fee=-0.0005, realized_pnl=0.0055)
    px = {T0: 100_000, T0 + 24 * H: 105_000}
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=px.get, settle_px=105_000)
    assert c.source == "summary"
    assert c.premium_coin == approx(0.0095) and c.premium_usd == approx(950)
    assert c.close_coin == approx(-0.004) and c.close_usd == approx(-420)
    assert c.realized_usd == approx(530)


def test_premium_net_stable_fee_in_coin_at_the_trade_price():
    leg = LegInfo(status="open", side="short", opened_at=T0, size=1, fills=[
        Fill(T0, "sell", 1, 0.01, -5.0, "USDT", 100_000)])
    c = leg_cash(leg, ct_val=1.0, size_coin=1.0, px_at=NO_PX)
    assert c.premium_coin == approx(0.01 - 0.00005) and c.premium_usd == approx(995)


def test_premium_per_day_of_a_leg_and_a_box():
    from app.domain.leg_cash import PremiumSpan, leg_end, premium_per_day
    exp = T0 + 120 * H
    # open leg → its expiry; closed → its close; stale → last seen
    assert leg_end("open", exp, None) == exp
    assert leg_end("closed", exp, T0 + 30 * H) == T0 + 30 * H
    assert leg_end("stale", exp, None, T0 + 7 * H) == T0 + 7 * H
    a = PremiumSpan(T0, exp, 0.05, 5000.0)                       # 5 days
    p = premium_per_day([a])
    assert p["days"] == approx(5) and p["coin_per_day"] == approx(0.01)
    assert p["usd_per_day"] == approx(1000) and p["n_missing"] == 0
    # box: earliest opening (T0 − 1 day) → latest end (exp + 2 days) = 8 days; Σ = 0.05 − 0.01
    b = PremiumSpan(T0 - 24 * H, T0 + 30 * H, -0.01, -900.0)
    c = PremiumSpan(T0 + H, exp + 48 * H, None, None)              # no cash data: dates count
    p = premium_per_day([a, b, c])
    assert p["start"] == T0 - 24 * H and p["end"] == exp + 48 * H and p["days"] == approx(8)
    assert p["coin"] == approx(0.04) and p["usd"] == approx(4100)
    assert p["coin_per_day"] == approx(0.005) and p["usd_per_day"] == approx(512.5)
    assert p["n_missing"] == 1
    p = premium_per_day([c])
    assert p["coin"] is None and p["coin_per_day"] is None and p["days"] == approx(167 / 24)
