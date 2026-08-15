"""Shared FastAPI dependencies: current user, admin gate, and DB session with RLS binding.

These are stubs wiring the access-control model from ARCHITECTURE.md §5.5 / §6. The auth layer
(fastapi-users) will populate `CurrentUser`; here we only define the contract the routes rely on.
"""
from __future__ import annotations

from dataclasses import dataclass

# from fastapi import Depends, HTTPException, status
# from sqlalchemy import text


@dataclass
class CurrentUser:
    id: int
    email: str
    role: str  # "client" | "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_current_user() -> CurrentUser:
    """Resolve the authenticated user from the session/JWT (wired via fastapi-users).

    TODO: replace stub with real token decoding.
    """
    raise NotImplementedError("wire fastapi-users auth")


def require_admin(user: CurrentUser) -> CurrentUser:
    """Route gate for /admin/*. Rejects non-admins with 403.

    Usage (once auth is wired):
        @router.get("/admin/overview")
        def overview(user: CurrentUser = Depends(require_admin)): ...
    """
    if not user.is_admin:
        # raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
        raise PermissionError("Admin only")
    return user


def bind_rls(session, user: CurrentUser) -> None:
    """Set per-request Postgres session GUCs the RLS policies read (see ARCHITECTURE.md §6).

    Admins get app.is_admin='true' → policies allow cross-tenant SELECT; everyone is still
    constrained to their own subaccounts otherwise.
    """
    # session.execute(text("SELECT set_config('app.current_user_id', :uid, true)"),
    #                 {"uid": str(user.id)})
    # session.execute(text("SELECT set_config('app.is_admin', :adm, true)"),
    #                 {"adm": "true" if user.is_admin else "false"})
    ...
