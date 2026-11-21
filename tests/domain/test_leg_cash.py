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
