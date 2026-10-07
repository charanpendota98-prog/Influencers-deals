"""End-to-end setup readiness and safe source onboarding checks."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, puller, worker


@pytest.fixture
def setup_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "setup-assistant.sqlite3")
    monkeypatch.setattr(config, "SHARED_SOURCES", [])
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "")
    monkeypatch.setattr(config, "TELEGRAM_SESSION", str(tmp_path / "missing-session"))
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def test_setup_page_has_source_to_channel_readiness_and_safe_live_check(setup_client):
    response = setup_client.get("/setup")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Launch readiness" in page
    assert "Source pool" in page
    assert "Run live checks" in page
    assert "never checks invite links or joins channels" in page
    assert "Telegram account" in page
    assert "Output channels" in page


def test_setup_source_pool_can_add_pause_and_remove_without_joining(setup_client):
    added = setup_client.post(
        "/sources/add",
        data={
            "from_setup": "1",
            "name": "Joined source",
            "spec": "@joined_source",
            "kind": "production",
        },
    )
    assert added.status_code == 302
    assert "source_saved=1" in added.headers["Location"]
    source = db.list_sources(active_only=False)[0]
    assert source["spec"] == "@joined_source"
    assert source["active"] == 1

    paused = setup_client.post(
        f"/sources/{source['id']}/toggle", data={"from_setup": "1"}
    )
    assert paused.status_code == 302
    assert "source_updated=1" in paused.headers["Location"]
    assert db.list_sources(active_only=False)[0]["active"] == 0

    removed = setup_client.post(
        f"/sources/{source['id']}/delete", data={"from_setup": "1"}
    )
    assert removed.status_code == 302
    assert "source_deleted=1" in removed.headers["Location"]
    assert db.list_sources(active_only=False) == []


def test_private_source_label_is_optional_and_never_exposes_invite_as_label(setup_client):
    response = setup_client.post(
        "/sources/add",
        data={"from_setup": "1", "spec": "https://t.me/+privatehash123"},
    )

    assert response.status_code == 302
    assert "source_saved=1" in response.headers["Location"]
    source = db.list_sources(active_only=False)[0]
    assert source["name"] == "Private Telegram source"
    assert source["spec"] == "https://t.me/+privatehash123"


def test_live_checks_report_telegram_sources_and_hub_without_sending(setup_client, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(
        puller,
        "inspect_source_selection",
        AsyncMock(return_value={
            "ok": True,
            "authorized": True,
            "configured_sources": 3,
            "joined_group_channels": 7,
            "selected_sources": 2,
            "unresolved_private_invites": 0,
        }),
    )
    monkeypatch.setattr(
        dashboard_module.whatsapp_client,
        "health_snapshot",
        AsyncMock(return_value={
            "ok": True,
            "session_count": 2,
            "connected_count": 1,
            "qr_pending_count": 1,
            "offline_count": 0,
        }),
    )
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = setup_client.post("/api/setup/live-checks")
    assert response.status_code == 200
    checks = response.get_json()["checks"]
    assert checks["telegram"]["state"] == "connected"
    assert checks["sources"]["state"] == "matched"
    assert "2 joined dialog(s) selected" in checks["sources"]["message"]
    assert checks["whatsapp_hub"]["state"] == "reachable"
    assert "No message was sent" in checks["whatsapp_hub"]["message"]


def test_live_checks_explain_named_private_invite_fallback(setup_client, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(
        puller,
        "inspect_source_selection",
        AsyncMock(return_value={
            "ok": True,
            "authorized": True,
            "configured_sources": 3,
            "matched_source_selectors": 0,
            "joined_group_channels": 236,
            "eligible_joined_dialogs": 236,
            "selected_sources": 236,
            "unresolved_private_invites": 3,
            "selection_mode": "joined_dialog_fallback",
            "fallback_reason": "private_invite_label_unmatched",
        }),
    )
    monkeypatch.setattr(
        dashboard_module.whatsapp_client,
        "health_snapshot",
        AsyncMock(return_value={
            "ok": True,
            "session_count": 0,
            "connected_count": 0,
            "qr_pending_count": 0,
            "offline_count": 0,
        }),
    )
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = setup_client.post("/api/setup/live-checks")

    assert response.status_code == 200
    sources = response.get_json()["checks"]["sources"]
    assert sources["state"] == "fallback"
    assert "236 joined dialog(s) selected" in sources["message"]
    assert "3 private invite label(s)" in sources["message"]
    assert "already-joined eligible dialog allowlist" in sources["message"]
    assert "No invite was checked or joined" in sources["message"]


def test_live_source_audit_uses_only_already_joined_dialogs(setup_client, monkeypatch):
    db.add_source("Already joined", "@already_joined", kind="production")

    entity = SimpleNamespace(
        id=456,
        username="already_joined",
        title="Already Joined",
        megagroup=True,
        broadcast=False,
        creator=False,
    )
    dialog = SimpleNamespace(
        id=-100456,
        entity=entity,
        is_user=False,
        is_bot=False,
        is_group=True,
        is_channel=False,
        username="already_joined",
        name="Already Joined",
    )

    class JoinedDialogOnlyClient:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def iter_dialogs(self):
            yield dialog

    monkeypatch.setattr(puller.telegram_ops, "_client", lambda: JoinedDialogOnlyClient())
    report = asyncio.run(puller.inspect_source_selection())

    assert report["authorized"] is True
    assert report["configured_sources"] == 1
    assert report["joined_group_channels"] == 1
    assert report["selected_sources"] == 1
    assert report["selection_mode"] == "configured_selectors"


def test_quick_add_stores_a_whatsapp_channel_jid_as_pending(setup_client):
    response = setup_client.post(
        "/quick-add",
        data={
            "name": "Channel Creator",
            "tag": "channelcreator-21",
            "allowed_sources": "joined_source",
            "whatsapp_id": "120363012345678901@newsletter",
        },
    )

    assert response.status_code == 302
    influencer = db.list_influencers()[0]
    destination = db.list_channels(influencer["id"])[0]
    assert destination["platform"] == "whatsapp_channel"
    assert destination["identifier"] == "120363012345678901@newsletter"
    assert destination["status"] == "pending"
    assert destination["allowed_sources"] == "joined_source"


def test_quick_add_never_saves_an_unresolved_channel_invite_as_a_group(setup_client, monkeypatch):
    def resolution_unavailable(coro):
        coro.close()
        raise RuntimeError("account not paired")

    monkeypatch.setattr(dashboard_module, "_run", resolution_unavailable)
    response = setup_client.post(
        "/quick-add",
        data={
            "name": "Needs Pairing",
            "tag": "needspairing-21",
            "whatsapp_id": "https://whatsapp.com/channel/ABCDEF_123456",
        },
    )

    assert response.status_code == 302
    assert "wa_link_status=failed" in response.headers["Location"]
    influencer = db.list_influencers()[0]
    assert influencer["name"] == "Needs Pairing"
    assert db.list_channels(influencer["id"]) == []


def test_worker_heartbeat_tracks_start_poll_and_stop(setup_client, monkeypatch):
    stop_event = asyncio.Event()

    async def one_poll(*, limit=None):
        stop_event.set()
        return {"pulled": 0, "handled": 0, "retry_sources": 0}

    monkeypatch.setattr(worker, "process_pending_batch", one_poll)
    monkeypatch.setattr(worker.scheduler, "start_scheduler", lambda: None)
    monkeypatch.setattr(worker.scheduler, "stop_scheduler", lambda: None)
    monkeypatch.setattr(worker.pipeline, "close", AsyncMock())

    asyncio.run(worker.run_forever(stop_event))
    heartbeat = db.get_worker_heartbeat()

    assert heartbeat["state"] == "stopped"
    assert heartbeat["pid"] > 0
    assert heartbeat["started_at"] > 0
    assert heartbeat["last_poll_at"] > 0
    assert heartbeat["heartbeat_at"] > 0
