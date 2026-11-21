"""Settings → Position hedged (build 16): input checks and the account × asset grid; build 18:
the future-PnL profile of graph ① — pure."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.domain.hedge import (
    NOTE_MAX,
    HedgeInputError,
    OptionLeg,
    clean_ccy,
    clean_note,
    clean_qty,
    combine_profiles,
    hedge_grid,
    hedge_profile,
    hedged_pnl,
    options_effect,
    payoff,
    profile_at,
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


# --------------------------------------------------------------------------- build 18: profile
Q, S0 = 10.0, 100.0
# the mock of 19 Nov: put spreads under the price (bought 95 / sold 90, bought 85 / sold 80), an
# in-the-money put spread above it (bought 115 / sold 110) and a sold call at the top (120)
BOOK = [OptionLeg(95, "P", 10), OptionLeg(90, "P", -10), OptionLeg(85, "P", 10),
        OptionLeg(80, "P", -10), OptionLeg(115, "P", 10), OptionLeg(110, "P", -10),
        OptionLeg(120, "C", -10)]


def test_zero_at_the_current_price_even_with_options_in_the_money():
    assert hedged_pnl(BOOK, Q, S0, S0) == 0 and options_effect(BOOK, S0, S0) == 0


def test_put_spread_freezes_the_loss_between_its_strikes():
    assert hedged_pnl(BOOK, Q, S0, 95) == -50
    assert hedged_pnl(BOOK, Q, S0, 92) == -50 == hedged_pnl(BOOK, Q, S0, 90)   # vertical part
    assert hedged_pnl(BOOK, Q, S0, 87) == -80                                   # falls again
    assert options_effect(BOOK, S0, 88) == 50              # options cover 50 of the −120 fall


def test_in_the_money_spread_above_and_the_sold_call_cap_the_rise():
    assert hedged_pnl(BOOK, Q, S0, 110) == 100
    assert hedged_pnl(BOOK, Q, S0, 113) == 100 == hedged_pnl(BOOK, Q, S0, 115)  # flat in spread
    assert hedged_pnl(BOOK, Q, S0, 120) == 150
    assert hedged_pnl(BOOK, Q, S0, 200) == 150                                  # capped above
    assert options_effect(BOOK, S0, 200) == -850


def test_profile_points_and_slopes_reproduce_the_formula_everywhere():
    pr = hedge_profile(BOOK, Q, S0)
    assert [p["px"] for p in pr["points"]] == [80, 85, 90, 95, 100, 110, 115, 120]
    assert pr["slope_below"] == {"hedged": 10.0, "btc_only": 10.0}   # spreads net out below 80
    assert pr["slope_above"] == {"hedged": 0.0, "btc_only": 10.0}    # capped by the sold call
    for s in [10, 79.5, 80, 83, 92.2, 100, 104, 112, 119, 120, 150, 1000]:
        v = profile_at(pr, s)
        assert abs(v["hedged"] - hedged_pnl(BOOK, Q, S0, s)) < 1e-9, s
        assert abs(v["btc_only"] - Q * (s - S0)) < 1e-9, s


def test_profile_without_options_or_without_q():
    pr = hedge_profile([], Q, S0)
    assert pr["points"] == [{"px": S0, "hedged": 0.0, "btc_only": 0.0}]
    assert profile_at(pr, 90) == {"hedged": -100.0, "btc_only": -100.0}
    pr = hedge_profile([OptionLeg(90, "P", 2)], 0.0, S0)           # Q not set: options only
    assert profile_at(pr, 80) == {"hedged": 20.0, "btc_only": 0.0}


def test_boxes_add_up_to_the_whole_book_and_a_hidden_box_drops_out():
    # 20 Nov: per-box options-only profiles + Q = the profile of all legs;
    # leaving a box out = leaving its legs out
    box_a, box_b = BOOK[:4], BOOK[4:]
    pa, pb = hedge_profile(box_a, 0.0, S0), hedge_profile(box_b, 0.0, S0)
    both, only_a = combine_profiles([pa, pb], Q, S0), combine_profiles([pa], Q, S0)
    for s in [10, 79, 80, 88, 95, 100, 111, 120, 400]:
        assert abs(profile_at(both, s)["hedged"] - hedged_pnl(BOOK, Q, S0, s)) < 1e-9, s
        assert abs(profile_at(only_a, s)["hedged"] - hedged_pnl(box_a, Q, S0, s)) < 1e-9, s
    assert combine_profiles([], Q, S0)["points"] == [{"px": S0, "hedged": 0.0, "btc_only": 0.0}]


def test_switches_shift_the_profile_by_a_constant():
    # build 21: from entry (premiums) = from now + Σ n·payoff(S0) + premium cash; realized adds on
    pa = hedge_profile(BOOK, 0.0, S0)
    entry = sum(lg.size * payoff(S0, lg) for lg in BOOK) + 37.5        # 37.5 = net premium & fees
    pr = combine_profiles([pa], Q, S0, shift=entry + 12.0)              # 12 = realized
    for s in [70, 95, 100, 130]:
        from_entry = Q * (s - S0) + sum(lg.size * payoff(s, lg) for lg in BOOK) + 37.5 + 12.0
        assert abs(profile_at(pr, s)["hedged"] - from_entry) < 1e-9, s
        assert profile_at(pr, s)["btc_only"] == Q * (s - S0)
