"""Black-76 pricer + intrinsic payoff."""
from __future__ import annotations

from app.domain.pricing import black76_price, payoff_intrinsic


def test_intrinsic():
    assert payoff_intrinsic(70000, 65000, "C") == 5000
    assert payoff_intrinsic(60000, 65000, "C") == 0
    assert payoff_intrinsic(60000, 65000, "P") == 5000
    assert payoff_intrinsic(70000, 65000, "P") == 0


def test_black76_atm_positive_and_put_call_parity():
    F, K, T, iv = 65000.0, 65000.0, 30 / 365, 0.6
    c = black76_price(F, K, T, iv, "C")
    p = black76_price(F, K, T, iv, "P")
    assert c > 0 and p > 0
    # undiscounted parity: C - P == F - K  (== 0 at the money)
    assert abs((c - p) - (F - K)) < 1e-6


def test_black76_degenerate_returns_intrinsic():
    # zero time or zero vol → intrinsic
    assert black76_price(70000, 65000, 0, 0.6, "C") == 5000
    assert black76_price(70000, 65000, 0.1, 0, "C") == 5000
