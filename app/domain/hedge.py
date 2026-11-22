"""Hedged amount Q and the future-PnL profile of graph ① — pure, no I/O.

Q = the amount of a coin (e.g. 10 BTC) held besides the options and hedged by them. build 22: set
per BOX (core.strategy.hedged_qty / hedged_ccy) in the Box builder's New box / ✎ dialog ("Position
hedged" — clean_qty / clean_ccy check the input); the panel adds up the Q of the boxes it shows.
(build 16 had it per account × asset in Settings, core.hedged_position — dropped in 0028.) Q is
entered by hand: the balance OKX reports for an options account (`eq`) includes the options' own
PnL, and option cash (premiums, fees, realized PnL) is added by the panel itself.

build 18 — the future-PnL profile drawn right of graph ① (hedge_profile): for a price S at expiry,

    BTC only        P0(S) = Q · (S − S0)
    options         O(S)  = Σ n_i · [payoff_i(S) − payoff_i(S0)]     n_i = +size bought / −size sold
    with options    P(S)  = P0(S) + O(S)

S0 = the current price; payoff = intrinsic value at expiry (call max(S − K, 0), put max(K − S, 0)).
No premiums and no current option value (O(S0) = 0 by construction: the profile is "from here"),
every open leg settled at the same S whatever its expiry. P is piecewise linear with kinks only at
the strikes, so the profile is exact as its values at the strikes and S0 plus the slopes beyond
the outermost ones — the page interpolates between them (profile_at).

20 Nov: only the boxes switched on in graph ①'s legend count, so the server sends one options-only
profile per box (Q = 0) and the page adds Q·(S − S0) and the visible boxes (combine_profiles; the
page's JS port is pnlCombine — keep the two in step).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

QTY_MAX = 1e12          # sanity bound for a typed amount
_CCY = re.compile(r"^[A-Z0-9]{1,16}$")


class HedgeInputError(ValueError):
    """A value the editor can't store — the message is shown to the admin."""


def clean_ccy(raw: str | None) -> str:
    """'btc ' → 'BTC'; letters and digits, at most 16 (core.strategy.hedged_ccy)."""
    ccy = (raw or "").strip().upper()
    if not _CCY.match(ccy):
        raise HedgeInputError(f"'{raw}' is not an asset code (letters / digits, ≤ 16)")
    return ccy


def clean_qty(raw: Any) -> float:
    """The hedged amount: a finite number ≥ 0 (0 = nothing hedged, kept as a value)."""
    if raw is None or raw == "" or isinstance(raw, bool):
        raise HedgeInputError("Enter the hedged amount (a number ≥ 0)")
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        raise HedgeInputError(f"'{raw}' is not a number") from None
    if not math.isfinite(qty) or qty < 0:
        raise HedgeInputError("The hedged amount must be a number ≥ 0")
    if qty > QTY_MAX:
        raise HedgeInputError(f"The hedged amount is too large (max {QTY_MAX:g})")
    return qty


# --------------------------------------------------------------------------- future-PnL profile
@dataclass(frozen=True)
class OptionLeg:
    strike: float
    opt_type: str            # 'C' | 'P'
    size: float              # in coin, signed: + bought (long), − sold (short)


def payoff(s: float, leg: OptionLeg) -> float:
    """Intrinsic value at expiry per coin of a long option."""
    return max(s - leg.strike, 0.0) if leg.opt_type == "C" else max(leg.strike - s, 0.0)


def options_effect(legs: list[OptionLeg], s0: float, s: float) -> float:
    """O(S): what the options add to (+) or take from (−) the PnL if the price ends at S."""
    return sum(lg.size * (payoff(s, lg) - payoff(s0, lg)) for lg in legs)


def hedged_pnl(legs: list[OptionLeg], qty: float, s0: float, s: float) -> float:
    """P(S) = Q · (S − S0) + O(S)."""
    return qty * (s - s0) + options_effect(legs, s0, s)


def hedge_profile(legs: list[OptionLeg], qty: float, s0: float) -> dict:
    """The exact profile: `points` [{px, hedged, btc_only}] at the strikes and S0 (ascending), and
    the slopes (USD per 1 USD of price) of both lines below the lowest and above the highest one."""
    pxs = sorted({float(s0), *(float(lg.strike) for lg in legs)})
    points = [{"px": px, "hedged": hedged_pnl(legs, qty, s0, px), "btc_only": qty * (px - s0)}
              for px in pxs]
    below = qty - sum(lg.size for lg in legs if lg.opt_type == "P")   # puts pay 1:1 under K
    above = qty + sum(lg.size for lg in legs if lg.opt_type == "C")   # calls pay 1:1 over K
    return {"points": points,
            "slope_below": {"hedged": below, "btc_only": qty},
            "slope_above": {"hedged": above, "btc_only": qty}}


def profile_at(profile: dict, s: float) -> dict:
    """{hedged, btc_only} at price S from a hedge_profile — linear between the points, the end
    slopes beyond them (the page does the same in JS)."""
    pts = profile["points"]
    if s <= pts[0]["px"]:
        p, k = pts[0], profile["slope_below"]
        return {key: p[key] + k[key] * (s - p["px"]) for key in ("hedged", "btc_only")}
    if s >= pts[-1]["px"]:
        p, k = pts[-1], profile["slope_above"]
        return {key: p[key] + k[key] * (s - p["px"]) for key in ("hedged", "btc_only")}
    for a, b in zip(pts, pts[1:], strict=False):
        if a["px"] <= s <= b["px"]:
            f = (s - a["px"]) / (b["px"] - a["px"])
            return {key: a[key] + f * (b[key] - a[key]) for key in ("hedged", "btc_only")}
    raise AssertionError("unreachable")


def combine_profiles(box_profiles: list[dict], qty: float, s0: float, shift: float = 0.0) -> dict:
    """One profile from options-only box profiles (hedge_profile with Q = 0) + Q·(S − S0): the
    points are S0 and every box's points, the values and slopes the sums (O is additive).
    build 21: `shift` = the constant the switches add to the options (premiums & fees of the open
    options counted from entry, realized PnL) — it moves "with options" only, never "coin only"."""
    pxs = sorted({float(s0), *(p["px"] for pr in box_profiles for p in pr["points"])})
    points = []
    for px in pxs:
        opt = sum(profile_at(pr, px)["hedged"] for pr in box_profiles)
        points.append({"px": px, "hedged": qty * (px - s0) + opt + shift,
                       "btc_only": qty * (px - s0)})
    return {"points": points,
            "slope_below": {"hedged": qty + sum(pr["slope_below"]["hedged"] for pr in box_profiles),
                            "btc_only": qty},
            "slope_above": {"hedged": qty + sum(pr["slope_above"]["hedged"] for pr in box_profiles),
                            "btc_only": qty}}
