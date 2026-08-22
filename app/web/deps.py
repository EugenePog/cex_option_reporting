"""Request dependencies: current user, admin gate, and subaccount scoping.

Auth uses a signed session cookie (Starlette SessionMiddleware). A client sees only the subaccounts
belonging to their own cex_accounts; an admin sees every subaccount.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models_core import CexAccount, CoreUser, Subaccount


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str                       # "client" | "admin"
    display_name: str | None = None
    subaccount_ids: list[int] = field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _load_user(uid: int) -> CurrentUser | None:
    with SessionLocal() as s:
        u = s.get(CoreUser, uid)
        if u is None or not u.is_active:
            return None
        if u.role == "admin":
            sub_ids = list(s.execute(select(Subaccount.id)).scalars())
        else:
            sub_ids = list(s.execute(
                select(Subaccount.id)
                .join(CexAccount, CexAccount.id == Subaccount.cex_account_id)
                .where(CexAccount.user_id == u.id)
            ).scalars())
        return CurrentUser(id=u.id, email=u.email, role=u.role,
                           display_name=u.display_name, subaccount_ids=sub_ids)


def get_current_user(request: Request) -> CurrentUser:
    """Resolve the logged-in user from the session, or 401 (redirect handled by the page layer)."""
    uid = request.session.get("uid")
    if uid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = _load_user(uid)
    if user is None:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user


def get_optional_user(request: Request) -> CurrentUser | None:
    uid = request.session.get("uid")
    return _load_user(uid) if uid is not None else None


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
