"""Security regression tests for the administrator dashboard."""
from __future__ import annotations

import re
import time

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


def test_production_requires_stable_secret_and_strong_admin_password(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "production")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "strong-admin-password-123")
    # No env key and no persisted key file: sessions would not survive a
    # restart, so the dashboard stays locked.
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", "")
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY_FILE", str(tmp_path / "absent.key"))

    client = app.test_client()
    assert client.get("/login").status_code == 503

    # A weak password is never enough in production.
    durable_key = tmp_path / "dashboard.key"
    durable_key.write_text("persistent-secret-key", encoding="utf-8")
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY_FILE", str(durable_key))
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "short-password")
    assert client.get("/login").status_code == 503

    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "strong-admin-password-123")
    assert client.get("/login").status_code == 200

    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", "env-secret-key")
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY_FILE", str(tmp_path / "absent.key"))
    assert client.get("/login").status_code == 200


def test_workers_share_one_persisted_session_key(monkeypatch, tmp_path):
    """A session signed by worker A must be readable by worker B.

    Without a shared key, a login page rendered by one worker always fails the
    CSRF check on the next one (HTTP 400 on POST /login).
    """
    from dashboard import app as dashboard_module

    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(dashboard_module, "_running_under_pytest", lambda: False)
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", "")
    key_file = tmp_path / "shared.key"
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY_FILE", str(key_file))

    generated = dashboard_module._resolve_dashboard_secret_key()
    assert key_file.exists()
    assert oct(key_file.stat().st_mode)[-3:] == "600"
    # A second worker reading the same file gets the same key.
    assert dashboard_module._resolve_dashboard_secret_key() == generated
    # The dashboard now counts as production-ready with that durable key.
    monkeypatch.setattr(config, "HUB_ENV", "production")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "strong-admin-password-123")
    assert app.test_client().get("/login").status_code == 200


def test_sensitive_setup_changes_use_one_password_per_unlock_window(monkeypatch, tmp_path):
    """The password is asked once at the start of a work session, not per change."""
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "DASHBOARD_SETUP_UNLOCK_SECONDS", 1800)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reauth-dashboard.sqlite3")
    db.init()

    client = app.test_client()
    login_page = client.get("/login")
    token = _csrf(login_page.get_data(as_text=True))
    assert client.post(
        "/login",
        data={"password": "test-admin-password-123", "_csrf_token": token},
    ).status_code == 302

    setup_page = client.get("/setup")
    assert setup_page.status_code == 200
    token = _csrf(setup_page.get_data(as_text=True))
    change = {"name": "New source", "spec": "@new_source", "_csrf_token": token}
    # Locked at the start: the very first change asks for the password.
    assert client.post("/sources/add", data=change).status_code == 428
    rejected_password = client.post(
        "/reauth", data={"password": "wrong", "_csrf_token": token}
    )
    assert rejected_password.status_code == 401
    assert client.post("/sources/add", data=change).status_code == 428

    confirmed = client.post(
        "/reauth",
        data={"password": "test-admin-password-123", "_csrf_token": token},
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["ok"] is True
    assert confirmed.get_json()["unlocked"] is True
    assert client.post("/sources/add", data=change).status_code == 302

    # Once unlocked, further changes go through without asking again.
    other_change = {"name": "Second source", "spec": "@second_source", "_csrf_token": token}
    assert client.post("/sources/add", data=other_change).status_code == 302

    # An idle dashboard locks itself again.
    with client.session_transaction() as browser_session:
        browser_session["_setup_unlock_at"] = time.time() - 5000
    assert client.post("/sources/add", data=other_change).status_code == 428

    # …and one confirmation opens it back up.
    assert client.post(
        "/reauth",
        data={"password": "test-admin-password-123", "_csrf_token": token},
    ).status_code == 200
    assert client.post("/sources/add", data=other_change).status_code == 302

    # Locking manually takes effect immediately.
    assert client.post("/reauth/lock", data={"_csrf_token": token}).status_code == 200
    assert client.post("/sources/add", data=other_change).status_code == 428


def test_setup_unlock_status_reports_and_binds_to_the_client_ip(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "unlock-status.sqlite3")
    db.init()

    client = app.test_client()
    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": "test-admin-password-123", "_csrf_token": token}
    ).status_code == 302
    # Signing in rotates the session, so read the token the new session issued.
    token = _csrf(client.get("/setup").get_data(as_text=True))

    assert client.get("/reauth/status").get_json()["unlocked"] is False
    assert client.post(
        "/reauth", data={"password": "test-admin-password-123", "_csrf_token": token}
    ).status_code == 200
    status = client.get("/reauth/status").get_json()
    assert status["unlocked"] is True
    assert 0 < status["remaining_seconds"] <= status["window_seconds"]

    # A cookie replayed from another address does not stay unlocked.
    with client.session_transaction() as browser_session:
        browser_session["_setup_unlock_ip"] = "203.0.113.9"
    assert client.get("/reauth/status").get_json()["unlocked"] is False


def test_locked_browser_form_post_redirects_back_instead_of_json(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "locked-browser.sqlite3")
    db.init()

    client = app.test_client()
    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": "test-admin-password-123", "_csrf_token": token}
    ).status_code == 302
    token = _csrf(client.get("/setup").get_data(as_text=True))

    browser_headers = {"Accept": "text/html,application/xhtml+xml", "Referer": "http://localhost/setup"}
    locked = client.post(
        "/sources/add",
        data={"name": "Browser", "spec": "@browser", "_csrf_token": token},
        headers=browser_headers,
        follow_redirects=False,
    )
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("/setup?reauth=1")
    assert db.list_sources() == []


def test_manual_poll_sending_requires_fresh_password_confirmation(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "test-admin-password-123")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "poll-reauth.sqlite3")
    db.init()
    influencer_id = db.add_influencer("Poll Security", "poll-security-21")
    db.add_channel(influencer_id, "telegram", "@poll_security", status="ready")

    client = app.test_client()
    login_page = client.get("/login")
    token = _csrf(login_page.get_data(as_text=True))
    assert client.post(
        "/login",
        data={"password": "test-admin-password-123", "_csrf_token": token},
    ).status_code == 302

    profile_page = client.get(f"/influencer/{influencer_id}")
    token = _csrf(profile_page.get_data(as_text=True))
    response = client.post(
        f"/influencer/{influencer_id}/send-poll",
        data={
            "_csrf_token": token,
            "poll_question": "Which fresh deals do you want next?",
            "poll_options": "Fashion\nTech",
            "poll_platforms": "telegram",
        },
    )
    assert response.status_code == 428
    assert db.list_recent_polls(influencer_id) == []


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
    reauth = client.post(
        "/reauth",
        data={"_csrf_token": csrf_token, "password": "test-admin-password-123"},
    )
    assert reauth.status_code == 200
    safe = client.post(
        "/global-settings/update",
        data={"_csrf_token": csrf_token, "nav_button_enabled": "0"},
    )
    assert safe.status_code == 302
    assert safe.headers["Location"].endswith("/setup")
    assert safe.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in safe.headers["Content-Security-Policy"]
