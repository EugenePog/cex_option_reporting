"""Settings tab (admin only) — /api/admin/settings/*.

The tab has a list of settings on the left; each opens an editor on the right. Built so far:

  Position hedged (build 16) — core.hedged_position (migration 0025): the hedged amount Q per
  account × asset, for the future-PnL profile of graph ①:
    GET    /hedged-positions                    one row per account × asset (+ saved rows)
    PUT    /hedged-positions/{subaccount}/{ccy} set Q (and the note) — adds or updates the row
    DELETE /hedged-positions/{subaccount}/{ccy} clear Q (deletes the row)

  Rules (app.domain.hedge + the table's constraints): Q is a number ≥ 0; the asset must be one the
  account has (or had) a balance in — gold.asset_balance_timeseries, the same rule as the asset
  selector of graph ②b — or already have a saved row; note ≤ 500 characters. The current balance
  is shown next to Q as a reference only (OKX `eq` includes the options' own PnL).

  Telegram notifications — core.notification_setting (migration 0022), read by the alerts worker on
  every check (no restart needed):
    GET    /notifications          rows + the allowed assets and codes
    POST   /notifications          add a row
    PUT    /notifications/{id}     edit a row
    DELETE /notifications/{id}     delete a row

  Rules (enforced here, and by the table's constraints):
    * code  — one of the codes the worker knows (app.db.models_core.NOTIFICATION_CODES);
    * asset — an underlying the accounts actually have (app.web.accounts.account_underlyings — the
              same list as the Dashboard's Underlying selector); an existing row keeps its asset
              even if that asset has no positions any more;
    * one row per asset + code (a second one would contradict the first) → 409;
    * threshold — 0 … 100 for every code (build 7): X / Y percent for a–d (top − X %, bottom + Y %),
              N days for e / f (last expiry − N days); 0 = exactly at the strike / expiry;
    * note — free text (≤ 500 characters).

  Every change is written to core.audit_log. Deleting a row keeps core.notification_state (keyed by
  box × code, not by row) — adding the row back continues from there.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select

from app.db.base import SessionLocal
from app.db.models_core import (
    NOTIFICATION_CODES,
    AuditLog,
    HedgedPosition,
    NotificationSetting,
    Subaccount,
)
from app.db.models_gold import AssetBalanceTimeseries
from app.domain.alerts import CODE_INFO, THRESHOLD_MAX, THRESHOLD_MIN
from app.domain.hedge import HedgeInputError, clean_ccy, clean_note, clean_qty, hedge_grid
from app.web.accounts import account_options, account_underlyings
from app.web.deps import CurrentUser, require_admin

router = APIRouter(prefix="/api/admin/settings", tags=["admin-settings"])

NOTE_MAX = 500


class NotificationIn(BaseModel):
    asset: str
    code: str
    threshold: float = 0
    is_enabled: bool = True
    is_critical: bool = False
    note: str | None = None


def _row(r: NotificationSetting) -> dict:
    return {"id": r.id, "asset": r.asset, "code": r.code, "threshold": float(r.threshold or 0),
            "is_enabled": bool(r.is_enabled), "is_critical": bool(r.is_critical), "note": r.note,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None}


def _assets(s) -> list[str]:
    """Allowed assets: the underlyings of all accounts (admin) with a non-zero position."""
    return account_underlyings(s, [i for (i,) in s.execute(select(Subaccount.id))])


def _code_label(code: str) -> str:
    return f"{CODE_INFO[code]['letter']} · {code}"


def _validate(s, body: NotificationIn, current: NotificationSetting | None) -> dict:
    """Checked, normalized column values — or HTTPException (422 / 409)."""
    code = (body.code or "").strip()
    if code not in NOTIFICATION_CODES:
        raise HTTPException(422, f"Unknown notification '{code}'. Allowed: "
                                 + ", ".join(NOTIFICATION_CODES))
    asset = (body.asset or "").strip().upper()
    keep_asset = current is not None and asset == current.asset
    if not keep_asset and asset not in _assets(s):
        raise HTTPException(422, f"'{asset}' is not an asset of the accounts (no positions in it)")
    unit = CODE_INFO[code]["unit"]
    threshold = float(body.threshold or 0)
    if not THRESHOLD_MIN <= threshold <= THRESHOLD_MAX:
        raise HTTPException(422, f"Threshold must be between {THRESHOLD_MIN} and {THRESHOLD_MAX}"
                                 f" ({unit})")
    note = (body.note or "").strip() or None
    if note and len(note) > NOTE_MAX:
        raise HTTPException(422, f"Note is too long (max {NOTE_MAX} characters)")
    clash = s.execute(select(NotificationSetting).where(
        NotificationSetting.asset == asset, NotificationSetting.code == code,
        NotificationSetting.id != (current.id if current else -1))).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(409, f"{asset} already has “{_code_label(code)}” (row #{clash.id}) — "
                                 "only one per asset and notification; edit that row instead")
    return {"asset": asset, "code": code, "threshold": threshold, "is_enabled": body.is_enabled,
            "is_critical": body.is_critical, "note": note}


def _audit(s, user_id: int, action: str, r: NotificationSetting) -> None:
    s.add(AuditLog(actor_user_id=user_id, action=action, target=(
        f"notification_setting={r.id} {r.asset} {r.code} threshold={float(r.threshold or 0):g} "
        f"enabled={r.is_enabled} critical={r.is_critical}")[:255]))


@router.get("/notifications")
def list_notifications(user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s:
        rows = s.execute(select(NotificationSetting)).scalars().all()
        assets = _assets(s)
    order = {c: i for i, c in enumerate(NOTIFICATION_CODES)}
    rows = sorted(rows, key=lambda r: (r.asset, order.get(r.code, 99), r.id))
    return {"rows": [_row(r) for r in rows], "assets": assets,
            "codes": [{"code": c, **CODE_INFO[c]} for c in NOTIFICATION_CODES],
            "threshold": {"min": THRESHOLD_MIN, "max": THRESHOLD_MAX}}


@router.post("/notifications", status_code=201)
def create_notification(body: NotificationIn, user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s, s.begin():
        r = NotificationSetting(**_validate(s, body, None))
        s.add(r)
        s.flush()
        s.refresh(r)
        _audit(s, user.id, "settings.notification.create", r)
        return _row(r)


@router.put("/notifications/{setting_id}")
def update_notification(setting_id: int, body: NotificationIn,
                        user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s, s.begin():
        r = s.get(NotificationSetting, setting_id)
        if r is None:
            raise HTTPException(404, "Unknown notification setting")
        for k, v in _validate(s, body, r).items():
            setattr(r, k, v)
        r.updated_at = func.now()
        s.flush()
        s.refresh(r)
        _audit(s, user.id, "settings.notification.update", r)
        return _row(r)


@router.delete("/notifications/{setting_id}")
def delete_notification(setting_id: int, user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s, s.begin():
        r = s.get(NotificationSetting, setting_id)
        if r is None:
            raise HTTPException(404, "Unknown notification setting")
        _audit(s, user.id, "settings.notification.delete", r)
        out = _row(r)
        s.delete(r)
        return out


# --------------------------------------------------------------------------- Position hedged
class HedgedIn(BaseModel):
    qty: Any = None                 # checked by clean_qty (a number ≥ 0)
    note: str | None = None


def _asset_balances(s, sub_id: int | None = None) -> dict[tuple[int, str], dict]:
    """{(subaccount_id, ccy): {balance, at, current}} — every asset the account has (or had) a
    balance in, with its amount in the account's latest snapshot (0 when it isn't in it)."""
    A = AssetBalanceTimeseries
    last_q = select(A.subaccount_id, func.max(A.captured_at)).group_by(A.subaccount_id)
    per_q = (select(A.subaccount_id, A.ccy, A.amount, A.captured_at)
             .where(A.ccy.isnot(None))
             .distinct(A.subaccount_id, A.ccy)
             .order_by(A.subaccount_id, A.ccy, A.captured_at.desc()))
    if sub_id is not None:
        last_q = last_q.where(A.subaccount_id == sub_id)
        per_q = per_q.where(A.subaccount_id == sub_id)
    last = dict(s.execute(last_q).all())
    out = {}
    for sid, ccy, amount, at in s.execute(per_q).all():
        current = at == last.get(sid)
        out[(sid, ccy.upper())] = {"balance": (float(amount) if amount is not None else None)
                                   if current else 0.0,
                                   "at": last.get(sid), "current": current}
    return out


def _saved(rows) -> dict[tuple[int, str], dict]:
    return {(r.subaccount_id, r.ccy): {"id": r.id, "qty": r.qty, "note": r.note,
                                       "updated_at": r.updated_at} for r in rows}


def _hedged_row(s, r: HedgedPosition | None, sub_id: int, ccy: str) -> dict:
    """The editor row of one account × asset, after a change."""
    rows = hedge_grid(account_options(s, [sub_id]), _asset_balances(s, sub_id),
                      _saved([r] if r is not None else []))
    return next((x for x in rows if x["ccy"] == ccy), {"subaccount_id": sub_id, "ccy": ccy,
                                                       "id": None, "qty": None})


def _hedged_audit(s, user_id: int, action: str, r: HedgedPosition) -> None:
    s.add(AuditLog(actor_user_id=user_id, action=action, target=(
        f"hedged_position={r.id} subaccount={r.subaccount_id} {r.ccy} "
        f"qty={float(r.qty):g}")[:255]))


def _hedged_key(s, subaccount_id: int, ccy: str) -> str:
    """Checked asset code of an existing account — or HTTPException (404 / 422)."""
    if s.get(Subaccount, subaccount_id) is None:
        raise HTTPException(404, "Unknown account")
    try:
        return clean_ccy(ccy)
    except HedgeInputError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/hedged-positions")
def list_hedged_positions(user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s:
        accounts = account_options(s)
        rows = hedge_grid(accounts, _asset_balances(s),
                          _saved(s.execute(select(HedgedPosition)).scalars().all()))
    return {"rows": rows, "accounts": [{"id": a["id"], "label": a["label"]} for a in accounts]}


@router.put("/hedged-positions/{subaccount_id}/{ccy}")
def set_hedged_position(subaccount_id: int, ccy: str, body: HedgedIn,
                        user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s, s.begin():
        ccy = _hedged_key(s, subaccount_id, ccy)
        try:
            qty, note = clean_qty(body.qty), clean_note(body.note)
        except HedgeInputError as e:
            raise HTTPException(422, str(e)) from None
        r = s.execute(select(HedgedPosition).where(
            HedgedPosition.subaccount_id == subaccount_id, HedgedPosition.ccy == ccy)
        ).scalar_one_or_none()
        if r is None:
            if (subaccount_id, ccy) not in _asset_balances(s, subaccount_id):
                raise HTTPException(422, f"The account has no {ccy} balance — pick one of its "
                                         "assets")
            r = HedgedPosition(subaccount_id=subaccount_id, ccy=ccy, qty=qty, note=note)
            s.add(r)
            action = "settings.hedged.create"
        else:
            r.qty, r.note, r.updated_at = qty, note, func.now()
            action = "settings.hedged.update"
        s.flush()
        s.refresh(r)
        _hedged_audit(s, user.id, action, r)
        return _hedged_row(s, r, subaccount_id, ccy)


@router.delete("/hedged-positions/{subaccount_id}/{ccy}")
def clear_hedged_position(subaccount_id: int, ccy: str,
                          user: CurrentUser = Depends(require_admin)) -> dict:
    with SessionLocal() as s, s.begin():
        ccy = _hedged_key(s, subaccount_id, ccy)
        r = s.execute(select(HedgedPosition).where(
            HedgedPosition.subaccount_id == subaccount_id, HedgedPosition.ccy == ccy)
        ).scalar_one_or_none()
        if r is None:
            raise HTTPException(404, f"No hedged {ccy} amount set for this account")
        _hedged_audit(s, user.id, "settings.hedged.delete", r)
        s.delete(r)
        s.flush()
        return _hedged_row(s, None, subaccount_id, ccy)
