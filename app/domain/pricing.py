"""Minimal Black-76 option pricing for the payoff report's T+0 curve. Pure, testable.

Black-76 prices an option on a forward F. Inputs/outputs are in the same currency as F and K
(USD here). We ignore discounting (r≈0) — fine for the short tenors this book trades and for a
visual T+0 curve. `payoff_intrinsic` gives the at-expiry intrinsic value.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def payoff_intrinsic(spot: float, strike: float, opt_type: str) -> float:
    """At-expiry intrinsic value per unit underlying (USD), for a long option."""
    if opt_type.upper() == "C":
        return max(spot - strike, 0.0)
    return max(strike - spot, 0.0)


def black76_price(forward: float, strike: float, t_years: float, iv: float, opt_type: str) -> float:
    """Undiscounted Black-76 price per unit underlying (USD). `iv` is a decimal vol (e.g. 0.55)."""
    if t_years <= 0 or iv <= 0 or forward <= 0 or strike <= 0:
        return payoff_intrinsic(forward, strike, opt_type)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(forward / strike) + 0.5 * iv * iv * t_years) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    if opt_type.upper() == "C":
        return forward * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - forward * _norm_cdf(-d1)
