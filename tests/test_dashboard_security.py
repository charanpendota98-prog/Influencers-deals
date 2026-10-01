"""Security regression tests for the administrator dashboard."""
from __future__ import annotations

import re

from dashboard.app import app
from influencer_hub import config, db


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "rendered form is missing its CSRF token"
    return match.group(1)


def test_dashboard_fails_closed_without_an_admin_password(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "locked-dashboard.sqlite3")

    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 503
    assert b"Dashboard is locked" in response.data
    assert client.get("/healthz").get_json() == {"ok": True}


def test_production_requires_stable_secret_and_strong_admin_password(monkeypatch):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "production")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "strong-admin-password-123")
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", "")

    client = app.test_client()
    assert client.get("/login").status_code == 503

    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", "persistent-secret-key")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "short-password")
    assert client.get("/login").status_code == 503

    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "strong-admin-password-123")
    assert client.get("/login").status_code == 200


def test_login_requires_csrf_and_authenticates_with_safe_redirect(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "secure-dashboard.sqlite3")
    db.init()

    client = app.test_client()
    assert client.get("/").status_code == 302
    assert client.get("/api/vm").status_code == 401

    login_page = client.get("/login?next=%2Fvm")
    assert login_page.status_code == 200
    token = _csrf(login_page.get_data(as_text=True))

    no_csrf = client.post(
        "/login", data={"password": "test-admin-password-123", "next": "/vm"}
    )
    assert no_csrf.status_code == 400

    bad_password = client.post(
        "/login",
        data={"password": "wrong", "next": "/vm", "_csrf_token": token},
    )
    assert bad_password.status_code == 401

    login_page = client.get("/login?next=%2F%2Fevil.example")
    token = _csrf(login_page.get_data(as_text=True))
    logged_in = client.post(
        "/login",
        data={
            "password": "test-admin-password-123",
            "next": "//evil.example",
            "_csrf_token": token,
        },
    )
    assert logged_in.status_code == 302
    assert logged_in.headers["Location"].endswith("/")
    cookie = ";".join(logged_in.headers.getlist("Set-Cookie")).lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    backslash_redirect = client.get("/login?next=%2F%5C%5Cevil.example")
    assert backslash_redirect.status_code == 302
    assert backslash_redirect.headers["Location"].endswith("/")

    dashboard = client.get("/vm")
    assert dashboard.status_code == 200
    csrf_token = _csrf(dashboard.get_data(as_text=True))
    unsafe = client.post("/global-settings/update", data={})
    assert unsafe.status_code == 400
    safe = client.post(
        "/global-settings/update",
        data={"_csrf_token": csrf_token, "nav_button_enabled": "0"},
    )
    assert safe.status_code == 302
    assert safe.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in safe.headers["Content-Security-Policy"]
