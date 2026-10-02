"""WhatsApp Channel link resolution, pending activation, and affiliate dispatch tests."""
from __future__ import annotations

import asyncio

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

    async def successful_test(_influencer, _channel, _text):
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", successful_test)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = whatsapp_dashboard.post(
        f"/channel/{channel_id}/send-test",
        data={"inf_id": str(influencer_id)},
    )

    assert response.status_code == 302
    assert "test_status=posted" in response.headers["Location"]
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
