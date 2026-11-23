"""build 25 — graph ② / ②b / Analyze in USD at operation-time prices (as graph ①) and in coin."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.domain.leg_cash import LegCash
from app.pipelines.silver_to_gold import _deal_usd, _upl_usd
from app.web.reports import _coin_metrics, _coin_prices

D = date(2026, 11, 19)


class Rates:                                   # the old rule: coin × the day's rate
    def rate(self, sub, ccy, d):
        return 60_000.0


def approx(x):
    return pytest.approx(x, abs=1e-9)


def test_deal_usd_uses_the_legs_trade_prices():
    cp = NS(position_leg_id=7, subaccount_id=1, ccy="BTC", closed_at=datetime(2026, 11, 19, 8),
            realized_pnl=-0.000122, fee=-0.000003)
    cash = {7: LegCash(0.0, -7.7, "summary", premium_coin=0.000102, premium_usd=6.5,
                       close_coin=-0.000224, close_usd=-14.2, fee_usd=-0.19)}
    assert _deal_usd(cp, cash, Rates()) == (approx(-7.7), approx(-0.19), "trades")
    # no cash data, or an operation without a price → the day rate
    assert _deal_usd(cp, {}, Rates()) == (approx(-7.32), approx(-0.18), "day rate")
    bad = {7: LegCash(0.0, 0.0, "summary", missing_px=1, premium_usd=1.0, close_usd=1.0)}
    assert _deal_usd(cp, bad, Rates())[2] == "day rate"


def test_unrealized_at_the_snapshots_own_price():
    p = NS(upl=0.001, idx_px=63_000.0, subaccount_id=1, underlying="BTC-USD",
           captured_at=datetime(2026, 11, 19, 9))
    assert _upl_usd(p, Rates()) == approx(63.0)
    assert _upl_usd(NS(**{**vars(p), "idx_px": None}), Rates()) == approx(60.0)


def test_coin_metrics_only_for_one_coin():
    ds = [NS(underlying="BTC-USD", realized_pnl=0.002), NS(underlying="BTC-USD", realized_pnl=-0.001)]
    m = _coin_metrics(ds)
    assert m["coin"] == "BTC" and m["net_pnl_coin"] == approx(0.001)
    assert m["avg_win_coin"] == approx(0.002) and m["avg_loss_coin"] == approx(-0.001)
    m = _coin_metrics(ds + [NS(underlying="ETH-USD", realized_pnl=0.1)])
    assert m["coin"] is None and m["net_pnl_coin"] is None and m["mixed_coins"]


def test_coin_prices_balance_then_position_then_candle():
    from app.db.models_gold import AssetBalanceTimeseries as A
    from app.db.models_gold import IndexCandle as C
    from app.db.models_gold import UnderlyingPrice as U
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        execution_options={"schema_translate_map": {"gold": None, "core": None}})
    for m in (A, U, C):
        m.__table__.create(eng)
    t1, t2, t3 = (datetime(2026, 11, 19, h, 5) for h in (9, 10, 11))
    with Session(eng) as s:                       # ids given: SQLite has no bigint autoincrement
        s.add_all([A(id=1, subaccount_id=1, ccy="BTC", captured_at=t1, amount=2, usd_value=126_000),
                   A(id=2, subaccount_id=1, ccy="BTC", captured_at=t2, amount=0, usd_value=0),
                   U(id=1, subaccount_id=1, underlying="BTC-USD", captured_at=t2, idx_px=62_500),
                   C(id=1, cex_code="OKX", inst_id="BTC-USD", bar="1h", ts=t3 - timedelta(minutes=5),
                     open=1, high=1, low=1, close=61_000, n_minutes=60, src_max_id=1,
                     updated_at=t3)])
        s.flush()
        px = _coin_prices(s, [1], "BTC-USD", [t1, t2, t3])
    assert px == {t1: approx(63_000), t2: approx(62_500), t3: approx(61_000)}
