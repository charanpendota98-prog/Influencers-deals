"""WhatsApp Channel link resolution, pending activation, and affiliate dispatch tests."""
from __future__ import annotations

import asyncio
import json

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, pipeline, whatsapp_client


@pytest.fixture
def whatsapp_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "whatsapp-onboarding.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def test_qr_and_status_polling_are_read_only_gets(whatsapp_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Read Only Creator", "readonlycreator-21")
    session_key = f"inf-{influencer_id}-wa"
    db.upsert_wa_session(influencer_id, session_key)
    db.set_wa_session_status(session_key, "qr")
    channel_id = db.add_channel(
        influencer_id,
        "whatsapp_channel",
        "120363012345678901@newsletter",
        status="pending",
        role="whatsapp",
        wa_session_key=session_key,
    )
    sessions_before = db.list_wa_sessions(influencer_id)
    channels_before = db.list_channels(influencer_id)
    requests = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/qr"):
            return FakeResponse({"ok": True, "qr": "data:image/png;base64,QR", "status": "qr"})
        return FakeResponse({
            "ok": True,
            "status": "connected",
            "phone": "919876543210",
            "hasQR": False,
        })

    monkeypatch.setattr(dashboard_module.urllib.request, "urlopen", fake_urlopen)
    qr_response = whatsapp_dashboard.get(f"/api/wa/{session_key}/qr")
    status_response = whatsapp_dashboard.get(f"/api/wa/{session_key}/status")

    assert qr_response.status_code == 200
    assert qr_response.get_json()["status"] == "qr"
    assert status_response.status_code == 200
    assert status_response.get_json()["status"] == "connected"
    assert [request.get_method() for request, _timeout in requests] == ["GET", "GET"]
    assert [request.full_url.rsplit("/", 1)[-1] for request, _timeout in requests] == ["qr", session_key]
    assert all(timeout == 10 for _request, timeout in requests)

    # Polling a connected phone must not persist a side effect or approve a destination.
    assert db.list_wa_sessions(influencer_id) == sessions_before
    assert db.list_channels(influencer_id) == channels_before
    destination = next(c for c in db.list_channels(influencer_id) if c["id"] == channel_id)
    assert destination["status"] == "pending"


def test_channel_invite_resolves_to_real_jid_and_stays_pending(whatsapp_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Channel Creator", "channelcreator-21")
    seen = []

    async def fake_resolve(session_key, invite):
        seen.append((session_key, invite))
        return {"ok": True, "jid": "120363012345678901@newsletter", "name": "Creator Deals"}

    monkeypatch.setattr(whatsapp_client, "resolve_newsletter", fake_resolve)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = whatsapp_dashboard.post(
        f"/influencer/{influencer_id}/wa-connect-chat",
        data={"invite_link": "https://whatsapp.com/channel/ABCDEF_123456", "role": "whatsapp"},
    )

    assert response.status_code == 302
    assert "wa_link_status=pending" in response.headers["Location"]
    assert seen == [(f"inf-{influencer_id}-wa", "https://whatsapp.com/channel/ABCDEF_123456")]
    channels = db.list_channels(influencer_id)
    assert len(channels) == 1
    assert channels[0]["platform"] == "whatsapp_channel"
    assert channels[0]["identifier"] == "120363012345678901@newsletter"
    assert channels[0]["invite_link"] == "https://whatsapp.com/channel/ABCDEF_123456"
    assert channels[0]["status"] == "pending"
    assert channels[0]["role"] == "whatsapp"
    assert channels[0]["wa_session_key"] == f"inf-{influencer_id}-wa"


def test_unresolved_channel_link_is_not_saved_as_a_ready_destination(whatsapp_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Invalid Link Creator", "invalidlink-21")

    async def fake_resolve(_session_key, _invite):
        return {"ok": False, "error": "not a channel"}

    monkeypatch.setattr(whatsapp_client, "resolve_newsletter", fake_resolve)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = whatsapp_dashboard.post(
        f"/influencer/{influencer_id}/wa-connect-chat",
        data={"invite_link": "https://whatsapp.com/channel/ABCDEF_123456"},
    )

    assert response.status_code == 302
    assert "wa_link_status=failed" in response.headers["Location"]
    assert db.list_channels(influencer_id) == []


def test_pending_whatsapp_channel_cannot_be_activated_from_the_status_controls(whatsapp_dashboard):
    influencer_id = db.add_influencer("Guarded Creator", "guardedcreator-21")
    channel_id = db.add_channel(
        influencer_id,
        "whatsapp_channel",
        "120363012345678901@newsletter",
        status="pending",
        role="whatsapp",
    )

    update = whatsapp_dashboard.post(
        f"/channel/{channel_id}/update",
        data={
            "inf_id": str(influencer_id),
            "identifier": "120363012345678901@newsletter",
            "role": "whatsapp",
            "status": "ready",
        },
    )
    toggle = whatsapp_dashboard.post(
        f"/channel/{channel_id}/toggle-status",
        data={"inf_id": str(influencer_id)},
    )

    assert update.status_code == 302
    assert "wa_link_status=pending" in update.headers["Location"]
    assert toggle.status_code == 302
    assert db.list_channels(influencer_id)[0]["status"] == "pending"


def test_pending_whatsapp_channel_activates_only_after_successful_test_post(whatsapp_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Pending Creator", "pendingcreator-21")
    channel_id = db.add_channel(
        influencer_id,
        "whatsapp_channel",
        "120363012345678901@newsletter",
        status="pending",
        role="whatsapp",
        wa_session_key=f"inf-{influencer_id}-wa",
    )

    async def failed_test(_influencer, _channel, _text):
        return "failed: admin posting permission is required"

    async def successful_test(_influencer, _channel, _text):
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", failed_test)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    failed = whatsapp_dashboard.post(
        f"/channel/{channel_id}/send-test",
        data={"inf_id": str(influencer_id)},
    )
    assert failed.status_code == 302
    assert "test_status=failed" in failed.headers["Location"]
    assert db.list_channels(influencer_id)[0]["status"] == "pending"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", successful_test)
    posted = whatsapp_dashboard.post(
        f"/channel/{channel_id}/send-test",
        data={"inf_id": str(influencer_id)},
    )
    assert posted.status_code == 302
    assert "test_status=posted" in posted.headers["Location"]
    assert db.list_channels(influencer_id)[0]["status"] == "ready"


def test_whatsapp_channel_uses_creator_amazon_tag_after_activation(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "whatsapp-channel-dispatch.sqlite3")
    db.init()
    influencer_id = db.add_influencer("Tagged Creator", "taggedcreator-21")
    channel_id = db.add_channel(
        influencer_id,
        "whatsapp_channel",
        "120363012345678901@newsletter",
        status="ready",
        role="whatsapp",
    )
    sent = []

    async def fake_send_text(session_key, to_jid, text):
        sent.append((session_key, to_jid, text))
        return {"ok": True}

    async def no_wait(_session_key):
        return None

    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)
    monkeypatch.setattr(pipeline, "apply_whatsapp_safety_pacing", no_wait)

    result = asyncio.run(
        pipeline.render_and_dispatch(
            "Amazon deal https://www.amazon.in/dp/B0ABCDEFGH?tag=old-21",
            influencer_ids=[influencer_id],
        )
    )

    assert result[influencer_id][channel_id] == "posted"
    assert len(sent) == 1
    assert sent[0][0] == f"inf-{influencer_id}-wa"
    assert sent[0][1] == "120363012345678901@newsletter"
    assert "tag=taggedcreator-21" in sent[0][2]
