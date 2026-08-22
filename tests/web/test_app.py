"""Web app smoke tests — boot the app and hit non-DB routes + auth gating (no DB needed)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.web.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200 and "Sign in" in r.text


def test_api_requires_auth():
    r = client.get("/api/equity")
    assert r.status_code == 401


def test_dashboard_redirects_when_anonymous():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_analyze_redirects_when_anonymous():
    r = client.get("/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
