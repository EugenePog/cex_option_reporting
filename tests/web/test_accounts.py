"""Account names in the UI: "<cex_account.label> · <display_name>" on every tab."""
from __future__ import annotations

from app.web.accounts import account_label


def test_label_uses_the_display_name():
    assert account_label("OKX_K", "Straddle sell", "") == "OKX_K · Straddle sell"


def test_label_falls_back_to_the_subaccount_name_then_main():
    assert account_label("OKX_K", None, "sub-7") == "OKX_K · sub-7"
    assert account_label("OKX_K", "", "") == "OKX_K · main"


def test_label_without_an_account_label():
    assert account_label(None, "Straddle sell", "") == "Straddle sell"
