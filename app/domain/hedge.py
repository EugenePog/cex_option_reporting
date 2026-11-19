"""Hedged positions (Settings → Position hedged, build 16) — pure, no I/O.

Q = the amount of an asset (e.g. 10 BTC) that an account's options hedge, set by an admin per
account × asset (core.hedged_position). The future-PnL profile of graph ① compares Q hedged by the
options with the same Q without them. Q is entered by hand: the balance OKX reports for an
options account (`eq`, gold.asset_balance_timeseries.amount) is equity and includes the options'
own PnL, so it is shown next to Q as a reference only.

The editor lists one row per account × asset — the assets an account has (or had) a balance in,
the same rule as the asset selector of graph ②b — plus any saved row whose asset is no longer
there, so a saved Q never disappears from view.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

QTY_MAX = 1e12          # sanity bound for a typed amount
NOTE_MAX = 500
_CCY = re.compile(r"^[A-Z0-9]{1,16}$")


class HedgeInputError(ValueError):
    """A value the editor can't store — the message is shown to the admin."""


def clean_ccy(raw: str | None) -> str:
    """'btc ' → 'BTC'; letters and digits, at most 16 (core.hedged_position.ccy)."""
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


def clean_note(raw: str | None) -> str | None:
    note = (raw or "").strip() or None
    if note and len(note) > NOTE_MAX:
        raise HedgeInputError(f"Note is too long (max {NOTE_MAX} characters)")
    return note


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def hedge_grid(accounts: list[dict], assets: dict[tuple[int, str], dict],
               saved: dict[tuple[int, str], dict]) -> list[dict]:
    """Rows of the editor, in the accounts' order, then by asset.

    accounts  [{id, label}] — the order rows are shown in; accounts not listed are left out
    assets    {(subaccount_id, ccy): {balance, at, current}} — the account's latest snapshot
              (`current` = the asset is in it; otherwise balance is 0 now)
    saved     {(subaccount_id, ccy): {id, qty, note, updated_at}} — core.hedged_position
    """
    order = {a["id"]: i for i, a in enumerate(accounts)}
    label = {a["id"]: a["label"] for a in accounts}
    keys = sorted((k for k in set(assets) | set(saved) if k[0] in order),
                  key=lambda k: (order[k[0]], k[1]))
    rows = []
    for sub_id, ccy in keys:
        a, s = assets.get((sub_id, ccy)), saved.get((sub_id, ccy))
        rows.append({
            "subaccount_id": sub_id, "account": label[sub_id], "ccy": ccy,
            "has_balance": a is not None,                 # asset known from the balances
            "balance": a.get("balance") if a else None,   # latest snapshot (0 when not in it)
            "balance_current": bool(a.get("current")) if a else False,   # in the latest snapshot
            "balance_at": _iso(a.get("at")) if a else None,
            "id": s["id"] if s else None,                 # None = not set
            "qty": float(s["qty"]) if s and s.get("qty") is not None else None,
            "note": s.get("note") if s else None,
            "updated_at": _iso(s.get("updated_at")) if s else None,
        })
    return rows
