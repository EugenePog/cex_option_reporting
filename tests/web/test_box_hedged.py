"""build 22 — Position hedged (Q) per box: the box dialog's input and the per-box Q that the
future-PnL panel of graph ① adds up for the boxes switched on in its legend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.web.admin.box_builder import _hedged
from app.web.reports import _hedge_block

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def test_hedged_input():
    assert _hedged(None, "BTC") == (None, None)
    assert _hedged("  ", "BTC") == (None, None)            # empty field = not set
    assert _hedged("1.5", "btc") == (1.5, "BTC")
    assert _hedged(0, "ETH") == (0.0, "ETH")                # 0 is a value (shown nowhere on the card)
    for bad in ("-1", "abc"):
        with pytest.raises(HTTPException) as e:
            _hedged(bad, "BTC")
        assert e.value.status_code == 422 and "Position hedged" in e.value.detail


def _leg(i, strike, ot, side, size, status="open"):
    return SimpleNamespace(position_leg_id=i, inst_id=f"BTC-USD-261225-{strike}-{ot}", strike=strike,
                           opt_type=ot, side=side, size_coin=size, status=status,
                           expires_at=NOW + timedelta(days=30), open_cash_usd=None,
                           realized_cash_usd=None, cash_source=None)


def test_hedge_block_carries_q_per_box():
    rows = [(_leg(1, 90000, "P", "long", 1), 0), (_leg(2, 110000, "C", "short", 1), 1)]
    out = _hedge_block(rows, NOW, 100000.0, "2026-11-22T11:59:00Z", {0: 2.0, 2: 0.5}, "BTC", "acct")
    assert "qty" not in out and "qty_set" not in out     # no account-level Q any more
    assert out["boxes"][0]["qty"] == 2.0
    assert out["boxes"][1]["qty"] is None                 # box without Position hedged
    assert out["boxes"][2]["qty"] == 0.5                  # Q but no open legs: still in the panel
    assert out["boxes"][2]["points"] == [{"px": 100000.0, "hedged": 0.0, "btc_only": 0.0}]
    assert len(out["legs"]) == 2


def test_account_assets_are_the_latest_non_zero_balances():
    """Settings' rule (build 16): balance now = the amount in the account's latest snapshot."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.db.models_gold import AssetBalanceTimeseries as A
    from app.web.accounts import account_assets

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        execution_options={"schema_translate_map": {"gold": None, "core": None}})
    A.__table__.create(eng)                 # ids given: SQLite has no bigint autoincrement
    t0, t1 = NOW - timedelta(hours=1), NOW
    with Session(eng) as s:
        s.add_all([A(id=1, subaccount_id=3, ccy="BTC", captured_at=t0, amount=5),
                   A(id=2, subaccount_id=3, ccy="ETH", captured_at=t0, amount=2),    # gone since
                   A(id=3, subaccount_id=3, ccy="btc", captured_at=t1, amount=12.5),
                   A(id=4, subaccount_id=3, ccy="USDT", captured_at=t1, amount=0),   # zero now
                   A(id=5, subaccount_id=3, ccy="USDC", captured_at=t1, amount=40),
                   A(id=6, subaccount_id=4, ccy="SOL", captured_at=t1, amount=1)])
        s.flush()
        assert account_assets(s, 3) == [{"ccy": "BTC", "balance": 12.5},
                                        {"ccy": "USDC", "balance": 40.0}]
        assert account_assets(s, 9) == []
