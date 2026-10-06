"""The dashboard password policy, pinned.

Rule (the operator's words): the password is asked only at sign-in and only for
removals. `REAUTH_REQUIRED_ENDPOINTS` must therefore be exactly three delete
endpoints, every add / save / toggle / poll form must stay free, and no template
may keep its own inline password prompt.

These tests fail loudly if a future change quietly re-introduces a password
prompt for everyday work.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from dashboard.app import app, REAUTH_REQUIRED_ENDPOINTS
from influencer_hub import config, db

PASSWORD = "test-admin-password-123"
REMOVALS = {"delete_channel", "delete_influencer", "delete_deal_source"}
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "dashboard" / "templates"
APP_JS = REPO_ROOT / "dashboard" / "static" / "app.js"

REAUTH_TAG_RE = re.compile(
    r"<(?:form|button)\b[^>]*\bdata-require-reauth\b[^>]*>", re.I | re.S
)


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "rendered form is missing its CSRF token"
    return match.group(1)


# --------------------------------------------------------------------------
# The gate itself
# --------------------------------------------------------------------------
def test_the_gate_is_exactly_the_three_removals():
    assert set(REAUTH_REQUIRED_ENDPOINTS) == REMOVALS


def test_no_setup_endpoint_sneaks_back_into_the_gate():
    """Anything that is not a removal must be absent from the gate."""
    non_removals = {
        "seed_default_sources", "add_deal_source", "toggle_deal_source",
        "update_global_settings", "quick_add", "update_profile",
        "update_channel_route", "add_manual_channel", "bulk_import",
        "undo_channel_delete", "easy_setup", "toggle_influencer_active",
        "toggle_channel_status", "onboard", "set_flags", "onboard_tg",
        "toggle_money_switch", "onboard_wa", "create_tg", "pair_wa",
        "create_group", "wa_connect_chat", "create_newsletter",
        "send_test_message", "send_poll",
    }
    assert REAUTH_REQUIRED_ENDPOINTS.isdisjoint(non_removals)


# --------------------------------------------------------------------------
# Templates: only the removal forms carry the unlock dialog
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "template", sorted(TEMPLATES.glob("*.html")), ids=lambda path: path.name
)
def test_only_removal_forms_keep_the_unlock_dialog(template):
    text = template.read_text(encoding="utf-8")
    for match in REAUTH_TAG_RE.finditer(text):
        tag = match.group(0)
        assert re.search(
            r"url_for\('(?:delete_channel|delete_influencer|delete_deal_source)'",
            tag,
        ), f"{template.name} keeps data-require-reauth on a non-removal form: {tag[:120]}"


def test_the_unlock_dialog_is_wired_on_exactly_four_tags():
    total = sum(
        len(REAUTH_TAG_RE.findall(path.read_text(encoding="utf-8")))
        for path in TEMPLATES.glob("*.html")
    )
    assert total == 4  # delete_influencer + delete_channel + 2x delete_deal_source
    assert "[data-require-reauth]" in APP_JS.read_text(encoding="utf-8")


def test_delete_influencer_uses_the_shared_dialog_only():
    """No separate prompt()/admin_password field for one removal."""
    html = (TEMPLATES / "influencer.html").read_text(encoding="utf-8")
    assert "prompt(" not in html
    assert 'name="admin_password"' not in html
    assert "delete_influencer" in html


def test_policy_copy_says_removals_not_setup_changes():
    copy = "\n".join(
        path.read_text(encoding="utf-8")
        for path in list(TEMPLATES.glob("*.html")) + [APP_JS]
    )
    assert "Setup changes are locked" not in copy
    assert "unlock setup changes" not in copy
    assert "Removals locked" in copy
    assert "Removals unlocked" in copy


# --------------------------------------------------------------------------
# Behaviour: add / save / poll are free, removals ask once per window
# --------------------------------------------------------------------------
@pytest.fixture
def signed_in(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_SETUP_UNLOCK_SECONDS", 1800)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reauth-policy.sqlite3")
    db.init()

    client = app.test_client()
    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": PASSWORD, "_csrf_token": token}
    ).status_code == 302
    token = _csrf(client.get("/setup").get_data(as_text=True))
    return client, token


def _post(client, path, data, token):
    payload = dict(data, _csrf_token=token)
    return client.post(path, data=payload, headers={"Accept": "text/html"})


def test_adds_toggles_and_saves_never_ask_for_the_password(signed_in):
    client, token = signed_in

    added = _post(client, "/sources/add", {"name": "Policy source", "spec": "@policy"}, token)
    assert added.status_code == 302
    assert "reauth=1" not in added.headers["Location"]
    source = db.list_sources(active_only=False)[0]

    toggled = _post(client, f"/sources/{source['id']}/toggle", {}, token)
    assert toggled.status_code == 302
    assert "reauth=1" not in toggled.headers["Location"]

    settings = _post(client, "/global-settings/update", {"nav_button_enabled": "0"}, token)
    assert settings.status_code == 302
    assert "reauth=1" not in settings.headers["Location"]

    easy = _post(
        client,
        "/easy-setup",
        {
            "name": "Policy Creator",
            "amazon_tag": "policy-21",
            "approval_channel": "@policy_approval",
            "main_channel": "@policy_main",
            "allow_amazon": "1",
        },
        token,
    )
    assert easy.status_code == 302
    assert "reauth=1" not in easy.headers["Location"]
    assert client.get("/reauth/status").get_json()["unlocked"] is False


def test_removals_ask_once_then_pass(signed_in):
    client, token = signed_in
    source_id = db.add_source("Policy source", "@policy_remove")

    locked = _post(client, f"/sources/{source_id}/delete", {}, token)
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("reauth=1")
    assert len(db.list_sources(active_only=False)) == 1

    assert client.post(
        "/reauth", data={"password": PASSWORD, "_csrf_token": token}
    ).status_code == 200

    removed = _post(client, f"/sources/{source_id}/delete", {}, token)
    assert removed.status_code == 302
    assert db.list_sources(active_only=False) == []
