"""How an account (a core.subaccount) is named in the UI — one format on every tab.

The name is "<cex_account.label> · <subaccount.display_name>", e.g. "OKX_K · Straddle sell". When
there is no display name, the exchange sub-account name is used, and "main" when that is empty too.
The Box builder, the Dashboard and the Analyze tab all use this, so an account reads the same
everywhere.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from app.db.models_core import CexAccount, Subaccount


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
