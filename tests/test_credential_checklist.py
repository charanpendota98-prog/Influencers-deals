import re

from dashboard.app import _credential_setup_snapshot, app
from influencer_hub import config


def test_credential_snapshot_reports_configuration_without_exposing_secret_values(monkeypatch, tmp_path):
    admin = "Admin-Password-with-unique-characters-2026!"
    session_key = "Random-session-signing-secret-which-is-48-chars-long"
    delete = "Delete-Confirmation-secret-2026!Different"
    team = "Team-Only-Password-with-unique-characters-2026!"
    hub_token = "WhatsApp-Hub-token-that-is-long-and-private-2026-abcdef"
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", admin)
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY", session_key)
    monkeypatch.setattr(config, "DASHBOARD_SECRET_KEY_FILE", tmp_path / "absent.key")
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", delete)
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", team)
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    monkeypatch.setattr(config, "HUB_ENV", "production")
    monkeypatch.setattr(config, "WA_HUB_URL", "http://127.0.0.1:8088")
    monkeypatch.setattr(config, "WA_HUB_TOKEN", hub_token)
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "telegram-private-hash")
    monkeypatch.setattr(config, "BOT_TOKEN", "telegram-bot-private-token")
    monkeypatch.setattr(config, "AMAZON_CREATORS_API_CLIENT_SECRET", "amazon-private-secret")
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "affiliate-private-key")

    snapshot = _credential_setup_snapshot()
    by_name = {item["name"]: item for item in snapshot}
    assert by_name["DASHBOARD_ADMIN_PASSWORD"]["state"] == "ready"
    assert by_name["DASHBOARD_SECRET_KEY"]["state"] == "ready"
    assert by_name["ADMIN_DELETE_PASSWORD"]["state"] == "ready"
    assert by_name["ADMIN_TOTP_SECRET"]["state"] == "ready"
    assert by_name["DASHBOARD_TEAM_PASSWORD"]["state"] == "ready"
    assert by_name["WA_HUB_TOKEN"]["state"] == "ready"

    rendered_snapshot = repr(snapshot)
    for secret in (admin, session_key, delete, team, hub_token,
                   "telegram-private-hash", "telegram-bot-private-token",
                   "amazon-private-secret", "affiliate-private-key"):
        assert secret not in rendered_snapshot


def test_setup_page_shows_variable_names_but_never_secret_values(monkeypatch):
    admin = "Owner-Only-Password-with-many-unique-symbols-2026!"
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", admin)
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", "workers-only-secret")

    client = app.test_client()
    login_page = client.get("/login").get_data(as_text=True)
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', login_page).group(1)
    assert client.post("/login", data={"password": admin, "_csrf_token": csrf}).status_code == 302
    page = client.get("/setup")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "DASHBOARD_ADMIN_PASSWORD" in body
    assert "DASHBOARD_TEAM_PASSWORD" in body
    assert "WA_HUB_TOKEN" in body
    assert admin not in body
    assert "workers-only-secret" not in body
