"""Accounts in the UI — how an account is named, and which underlyings ("assets") it has.

The name is "<cex_account.label> · <subaccount.display_name>", e.g. "OKX_K · Straddle sell". When
there is no display name, the exchange sub-account name is used, and "main" when that is empty too.
The Box builder, the Dashboard and the Analyze tab all use this, so an account reads the same
everywhere.

`account_underlyings` (build 6) is the one list of underlyings the accounts actually have — used by
the Dashboard / Analyze Underlying selector and the Settings → Telegram notifications asset list.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select, union

from app.db.models_core import CexAccount, Subaccount
from app.db.models_gold import DealLedger, PositionCurrent
from app.db.models_gold import PositionLeg as GoldPositionLeg


def account_label(cex_label: str | None, display_name: str | None, subacct_name: str | None) -> str:
    name = display_name or subacct_name or "main"
    return f"{cex_label} · {name}" if cex_label else name


def account_options(s, ids: Iterable[int] | None = None) -> list[dict]:
    """[{id, label, cex_code}] ordered by id; only `ids` when given."""
    q = (select(Subaccount.id, Subaccount.display_name, Subaccount.subacct_name, CexAccount.label,
                CexAccount.cex_code)
         .join(CexAccount, CexAccount.id == Subaccount.cex_account_id).order_by(Subaccount.id))
    if ids is not None:
        q = q.where(Subaccount.id.in_(list(ids)))
    return [{"id": sid, "label": account_label(label, disp, sub_name), "cex_code": cex}
            for sid, disp, sub_name, label, cex in s.execute(q).all()]


def account_labels(s, ids: Iterable[int]) -> dict[int, str]:
    """{subaccount_id: label}."""
    return {a["id"]: a["label"] for a in account_options(s, ids)}


def account_underlyings(s, subaccount_ids: Iterable[int]) -> list[str]:
    """Underlyings the accounts actually have, sorted: at least one position with a non-zero size —
    open now (gold.position_current), traded (gold.deal_ledger) or a position leg, open or closed
    (gold.position_leg). Underlyings with only price data (index candles) and zero-size rows don't
    count."""
    ids = list(subaccount_ids)
    if not ids:
        return []
    q = union(*[select(t.underlying).where(t.subaccount_id.in_(ids), t.underlying.isnot(None),
                                           t.size.isnot(None), t.size != 0)
                for t in (PositionCurrent, DealLedger, GoldPositionLeg)])
    return sorted({u for (u,) in s.execute(q) if u})
