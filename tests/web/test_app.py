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


def test_box_builder_redirects_when_anonymous():
    r = client.get("/box-builder", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_box_builder_api_requires_auth():
    assert client.get("/api/admin/box-builder/scope").status_code == 401
    r = client.post("/api/admin/box-builder/apply",
                    json={"subaccount_id": 1, "moves": [], "reason": "x"})
    assert r.status_code == 401


def test_box_edit_and_delete_require_auth():
    assert client.put("/api/admin/box-builder/strategies/1", json={"name": "x"}).status_code == 401
    assert client.delete("/api/admin/box-builder/strategies/1").status_code == 401
