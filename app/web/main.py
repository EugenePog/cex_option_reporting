"""FastAPI app: authenticated dashboard (reports ①–⑤) + Analyze tab (⑥).

Server-rendered pages (Jinja2, dark theme) that fetch JSON from /api/* and draw Plotly charts.
Auth is a signed session cookie; pages redirect to /login when not authenticated.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.db.base import SessionLocal
from app.db.models_core import CoreUser
from app.web.deps import CurrentUser, get_current_user, get_optional_user
from app.web.reports import router as reports_router
from app.web.security import verify_password
from config.settings import get_settings

_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

app = FastAPI(title="CEX Option Reporting")
app.add_middleware(SessionMiddleware, secret_key=get_settings().app_secret_key or "dev-insecure-key")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
app.include_router(reports_router)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# --- auth ----------------------------------------------------------------- #
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if get_optional_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    with SessionLocal() as s:
        user = s.execute(select(CoreUser).where(CoreUser.email == email)).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid email or password."}, status_code=401)
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- pages ---------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_optional_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "dashboard.html", {"user": user})


@app.get("/analyze", response_class=HTMLResponse)
def analyze_page(request: Request):
    user = get_optional_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "analyze.html", {"user": user})


# JSON identity for the frontend nav
@app.get("/api/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role,
            "display_name": user.display_name, "is_admin": user.is_admin}
