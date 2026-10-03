from __future__ import annotations

import asyncio

import pytest

from dashboard import app as dashboard_module
from influencer_hub import config, db, polls, pipeline, whatsapp_client


@pytest.fixture
def poll_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "poll-dashboard.sqlite3")
    monkeypatch.setitem(dashboard_module.app.config, "TESTING", True)
    db.init()
    monkeypatch.setattr(dashboard_module, "_run", lambda coroutine: asyncio.run(coroutine))
    async def disconnected(_key):
        return {"status": "offline"}
    monkeypatch.setattr(whatsapp_client, "session_status", disconnected)
    return dashboard_module.app.test_client()


def test_dashboard_sends_one_unique_poll_then_rejects_repeat(poll_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Poll Composer", "poll-composer-21")
    telegram_id = db.add_channel(
        influencer_id, "telegram", "@poll_composer", role="broadcast", status="ready"
    )
    whatsapp_id = db.add_channel(
        influencer_id, "whatsapp_group", "120363010@g.us", role="whatsapp", status="ready"
    )
    sent = []

    async def fake_dispatch(channel, question, options, allow_multiple=False):
        sent.append((channel["id"], question, tuple(options), allow_multiple))
        return {"ok": True}

    monkeypatch.setattr(pipeline, "dispatch_poll", fake_dispatch)
    payload = {
        "poll_question": "What category should we post next?",
        "poll_options": "Fashion\nTech\nHome",
        "poll_platforms": ["telegram", "whatsapp"],
        "allow_multiple": "1",
    }

    first = poll_dashboard.post(
        f"/influencer/{influencer_id}/send-poll", data=payload, follow_redirects=True
    )
    assert first.status_code == 200
    assert b"Unique poll queued for 2 destination(s)" in first.data
    assert sent == []  # external dispatch runs in the durable background queue
    assert b"What category should we post next?" in first.data
    assert b"2 queued/sending" in first.data

    stats = asyncio.run(polls.process_pending_polls(limit=10))
    assert stats["processed"] == 2
    assert stats["posted"] == 2
    assert {entry[0] for entry in sent} == {telegram_id, whatsapp_id}
    assert all(entry[3] is True for entry in sent)

    duplicate_payload = dict(payload, poll_question="WHAT CATEGORY should we post next!!!")
    second = poll_dashboard.post(
        f"/influencer/{influencer_id}/send-poll", data=duplicate_payload,
        follow_redirects=True,
    )
    assert second.status_code == 200
    assert b"has already been used" in second.data
    assert len(sent) == 2


def test_dashboard_rejects_invalid_poll_before_network_dispatch(poll_dashboard, monkeypatch):
    influencer_id = db.add_influencer("Invalid Poll", "invalid-poll-21")
    db.add_channel(influencer_id, "telegram", "@invalid_poll", status="ready")
    sent = []

    async def fake_dispatch(*args, **kwargs):
        sent.append(args)

    monkeypatch.setattr(pipeline, "dispatch_poll", fake_dispatch)
    response = poll_dashboard.post(
        f"/influencer/{influencer_id}/send-poll",
        data={
            "poll_question": "Pick one",
            "poll_options": "Same\nsame!",
            "poll_platforms": ["telegram"],
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"duplicate answer options" in response.data
    assert sent == []
    assert db.list_recent_polls(influencer_id) == []
