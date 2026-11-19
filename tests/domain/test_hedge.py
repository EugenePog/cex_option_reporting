"""Settings → Position hedged (build 16): input checks and the account × asset grid — pure."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.hedge import (
    NOTE_MAX,
    HedgeInputError,
    clean_ccy,
    clean_note,
    clean_qty,
    hedge_grid,
)

AT = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
ACCOUNTS = [{"id": 3, "label": "OKX_K · Straddle sell"}, {"id": 1, "label": "OKX_M_BOX · Box test"}]


def test_ccy_is_upper_cased_and_checked():
    assert clean_ccy(" btc ") == "BTC"
    for bad in ("", None, "BTC-USD", "B T C", "X" * 17):
        with pytest.raises(HedgeInputError):
            clean_ccy(bad)


@pytest.mark.parametrize("raw, qty", [(10, 10.0), ("2.5", 2.5), (0, 0.0), ("0", 0.0)])
def test_qty_accepts_numbers_from_zero(raw, qty):
    assert clean_qty(raw) == qty


@pytest.mark.parametrize("raw", [None, "", True, "abc", -1, "-0.1", float("nan"), float("inf"),
                                 1e13])
def test_qty_refuses_empty_negative_and_odd_values(raw):
    with pytest.raises(HedgeInputError):
        clean_qty(raw)


def test_note_trimmed_empty_is_none_and_length_checked():
    assert clean_note("  ") is None and clean_note(None) is None
    assert clean_note(" spot held ") == "spot held"
    with pytest.raises(HedgeInputError):
        clean_note("x" * (NOTE_MAX + 1))


def test_grid_one_row_per_account_and_asset_in_account_order():
    assets = {(1, "USDT"): {"balance": 500.0, "at": AT, "current": True},
              (3, "USDT"): {"balance": 0.0, "at": AT, "current": False},
              (3, "BTC"): {"balance": 12.5, "at": AT, "current": True}}
    saved = {(3, "BTC"): {"id": 7, "qty": 10, "note": "spot", "updated_at": AT}}
    rows = hedge_grid(ACCOUNTS, assets, saved)
    assert [(r["subaccount_id"], r["ccy"]) for r in rows] == [(3, "BTC"), (3, "USDT"), (1, "USDT")]
    btc = rows[0]
    assert (btc["id"], btc["qty"], btc["note"], btc["balance"]) == (7, 10.0, "spot", 12.5)
    assert btc["balance_current"] and btc["account"] == "OKX_K · Straddle sell"
    assert btc["updated_at"] == AT.isoformat() and btc["balance_at"] == AT.isoformat()
    usdt = rows[1]                                    # not in the latest snapshot → 0 now, not set
    assert usdt["balance"] == 0.0 and not usdt["balance_current"]
    assert usdt["id"] is None and usdt["qty"] is None


def test_grid_keeps_a_saved_row_whose_asset_has_no_balance():
    saved = {(1, "ETH"): {"id": 2, "qty": 0, "note": None, "updated_at": AT}}
    (row,) = hedge_grid(ACCOUNTS, {}, saved)
    assert row["ccy"] == "ETH" and row["qty"] == 0.0 and not row["has_balance"]
    assert row["balance"] is None and row["balance_at"] is None


def test_grid_leaves_out_accounts_not_listed():
    assets = {(99, "BTC"): {"balance": 1.0, "at": AT, "current": True}}
    assert hedge_grid(ACCOUNTS, assets, {}) == []
