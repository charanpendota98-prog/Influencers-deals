from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from influencer_hub import config, db, polls, pipeline, telegram_ops


@pytest.fixture
def poll_database(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "polls.sqlite3")
    db.init()
    return tmp_path / "polls.sqlite3"


def test_question_and_option_validation_normalizes_punctuation_and_case():
    question, options, normalized = polls.validate_poll(
        "  Which deals should we share next?!  ",
        ["Fashion", " Electronics ", "Home"],
    )
    assert question == "Which deals should we share next?!"
    assert options == ["Fashion", "Electronics", "Home"]
    assert normalized == polls.normalize_poll_question("which DEALS should we share next")

    with pytest.raises(ValueError, match="duplicate answer options"):
        polls.validate_poll("Pick one", ["Yes", " yes!!! "])
    with pytest.raises(ValueError, match="between 2 and 10"):
        polls.validate_poll("Pick one", ["Only one"])
    with pytest.raises(ValueError, match="100 characters"):
        polls.validate_poll("Pick one", ["A" * 101, "Other"])


def test_poll_question_is_globally_deduplicated_and_near_duplicates_are_blocked(poll_database):
    first = db.create_poll_question(
        11,
        "Which category of deals should we feature in our next shopping update?",
        polls.normalize_poll_question(
            "Which category of deals should we feature in our next shopping update?"
        ),
        ["Fashion", "Electronics"],
    )
    assert first["created"] is True

    exact_duplicate = db.create_poll_question(
        22,
        "WHICH category of deals should we feature in our next shopping update!!!",
        polls.normalize_poll_question(
            "WHICH category of deals should we feature in our next shopping update!!!"
        ),
        ["Home", "Beauty"],
    )
    assert exact_duplicate["created"] is False
    assert exact_duplicate["duplicate_kind"] == "exact"
    assert exact_duplicate["existing_question"] == first_question(first["poll_id"])

    near_duplicate = db.create_poll_question(
        22,
        "What category of deals should we feature in our next shopping update?",
        polls.normalize_poll_question(
            "What category of deals should we feature in our next shopping update?"
        ),
        ["Fashion", "Electronics"],
    )
    assert near_duplicate["created"] is False
    assert near_duplicate["duplicate_kind"] == "similar"


def first_question(poll_id: int) -> str:
    history = db.list_recent_polls(11)
    assert history and history[0]["id"] == poll_id
    return history[0]["question"]


def test_eligible_targets_include_telegram_and_whatsapp_groups_only(poll_database):
    influencer_id = db.add_influencer("Poll Target Test", "poll-target-21")
    tg_id = db.add_channel(
        influencer_id, "telegram", "@poll_broadcast", role="broadcast", status="ready"
    )
    wa_id = db.add_channel(
        influencer_id, "whatsapp_group", "120363001@g.us", role="whatsapp", status="ready"
    )
    db.add_channel(
        influencer_id, "whatsapp_group", "120363001@g.us", role="whatsapp", status="ready",
        wa_session_key="alternate-account",
    )
    db.add_channel(
        influencer_id, "whatsapp_channel", "120363002@newsletter", role="whatsapp", status="ready"
    )
    db.add_channel(
        influencer_id, "telegram", "@poll_approval", role="approval", status="ready"
    )
    db.add_channel(
        influencer_id, "whatsapp_group", "120363003@g.us", role="whatsapp", status="pending"
    )
    db.add_channel(
        influencer_id, "whatsapp_group", "919876543210@s.whatsapp.net", role="whatsapp", status="ready"
    )

    influencer = db.get_influencer(influencer_id)
    targets = polls.eligible_poll_targets(influencer, db.list_channels(influencer_id))
    assert {(target["id"], target["poll_platform"]) for target in targets} == {
        (tg_id, "telegram"),
        (wa_id, "whatsapp"),
    }

    influencer["whatsapp_enabled"] = 0
    assert [target["poll_platform"] for target in polls.eligible_poll_targets(
        influencer, db.list_channels(influencer_id)
    )] == ["telegram"]
    influencer["active"] = 0
    assert polls.eligible_poll_targets(influencer, db.list_channels(influencer_id)) == []


def test_poll_delivery_queue_is_durable_claimed_once_and_auditable(poll_database):
    influencer_id = db.add_influencer("Poll History Test", "poll-history-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@poll_history", role="broadcast", status="ready"
    )
    record = db.create_poll_question(
        influencer_id, "Which fresh deal category?", "which fresh deal category",
        ["Fashion", "Home"], allow_multiple=True, channel_ids=[channel_id],
    )
    poll_id = record["poll_id"]

    queued = db.list_recent_polls(influencer_id)[0]
    assert queued["pending_count"] == 1
    claimed = db.claim_next_poll_delivery()
    assert claimed["poll_id"] == poll_id
    assert claimed["options"] == ["Fashion", "Home"]
    assert db.claim_next_poll_delivery() is None
    db.finish_poll_delivery(poll_id, channel_id, "posted", platform_message_id="tg-1")

    history = db.list_recent_polls(influencer_id)
    assert history[0]["options"] == ["Fashion", "Home"]
    assert history[0]["allow_multiple"] == 1
    assert history[0]["destination_count"] == 1
    assert history[0]["posted_count"] == 1
    assert history[0]["failed_count"] == 0
    assert "@poll_history: posted" in history[0]["destination_details"]


def test_queued_poll_dispatches_once_and_blocks_repeat_submission(poll_database, monkeypatch):
    influencer_id = db.add_influencer("Poll Dispatch Test", "poll-dispatch-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@poll_dispatch", role="broadcast", status="ready"
    )
    targets = polls.eligible_poll_targets(
        db.get_influencer(influencer_id), db.list_channels(influencer_id)
    )
    sent = []

    async def fake_dispatch(channel, question, options, allow_multiple=False):
        sent.append((channel["id"], question, list(options), allow_multiple))
        return {"ok": True, "id": "tg-message-1"}

    monkeypatch.setattr(pipeline, "dispatch_poll", fake_dispatch)
    question, options, normalized = polls.validate_poll(
        "Which style should we feature next?", ["Casual", "Formal"]
    )
    first = polls.enqueue_poll(
        influencer_id, question, options, normalized, False, targets
    )
    second = polls.enqueue_poll(
        influencer_id, question.upper(), options, normalized, False, targets
    )
    assert first["duplicate"] is False
    assert first["queued_count"] == 1
    assert second["duplicate"] is True
    assert sent == []  # the web request queues work without waiting for network pacing

    stats = asyncio.run(polls.process_pending_polls(limit=5))
    assert stats == {"processed": 1, "posted": 1, "failed": 0, "skipped": 0}
    assert sent == [(channel_id, question, options, False)]


def test_failed_poll_is_not_automatically_retried(poll_database, monkeypatch):
    influencer_id = db.add_influencer("Poll Failure Test", "poll-failure-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@poll_failure", role="broadcast", status="ready"
    )
    targets = polls.eligible_poll_targets(
        db.get_influencer(influencer_id), db.list_channels(influencer_id)
    )
    question, options, normalized = polls.validate_poll(
        "Which fresh category should we show?", ["Fashion", "Home"]
    )
    queued = polls.enqueue_poll(
        influencer_id, question, options, normalized, False, targets
    )

    async def fail_dispatch(*_args, **_kwargs):
        raise RuntimeError("temporary platform timeout")

    monkeypatch.setattr(pipeline, "dispatch_poll", fail_dispatch)
    first = asyncio.run(polls.process_pending_polls(limit=5))
    second = asyncio.run(polls.process_pending_polls(limit=5))
    assert first == {"processed": 1, "posted": 0, "failed": 1, "skipped": 0}
    assert second == {"processed": 0, "posted": 0, "failed": 0, "skipped": 0}
    history = db.list_recent_polls(influencer_id)[0]
    assert history["failed_count"] == 1
    assert history["pending_count"] == 0
    assert "temporary platform timeout" in history["destination_details"]
    assert queued["duplicate"] is False


def test_dispatch_poll_uses_native_platform_routes_and_whatsapp_pacing():
    wa_target = {
        "id": 8,
        "influencer_id": 3,
        "platform": "whatsapp_group",
        "poll_platform": "whatsapp",
        "identifier": "120363000@g.us",
    }
    with patch("influencer_hub.pipeline.apply_whatsapp_safety_pacing", new=AsyncMock()) as pace, \
         patch("influencer_hub.pipeline.whatsapp_client.send_poll", new=AsyncMock(return_value={"ok": True})) as send:
        response = asyncio.run(pipeline.dispatch_poll(
            wa_target, "Question?", ["A", "B"], allow_multiple=True
        ))
    assert response == {"ok": True}
    pace.assert_awaited_once_with("inf-3-wa")
    send.assert_awaited_once_with(
        "inf-3-wa", "120363000@g.us", "Question?", ["A", "B"], allow_multiple=True
    )

    newsletter = dict(wa_target, identifier="120363000@newsletter")
    with pytest.raises(ValueError, match="only in Telegram channels and WhatsApp groups"):
        asyncio.run(pipeline.dispatch_poll(newsletter, "Question?", ["A", "B"]))


def test_telegram_poll_uses_native_send_media_request(monkeypatch):
    class FakeClient:
        def __init__(self):
            self.request = None

        def is_connected(self):
            return True

        async def get_input_entity(self, selector):
            return ("peer", selector)

        async def __call__(self, request):
            self.request = request
            return {"ok": True}

    fake_client = FakeClient()
    monkeypatch.setattr(telegram_ops, "_client", lambda: fake_client)

    def constructor(name):
        return lambda **kwargs: {"type": name, **kwargs}

    media_types = SimpleNamespace(
        PollAnswer=constructor("PollAnswer"),
        TextWithEntities=constructor("TextWithEntities"),
        Poll=constructor("Poll"),
        InputMediaPoll=constructor("InputMediaPoll"),
    )
    functions = SimpleNamespace(
        messages=SimpleNamespace(SendMediaRequest=constructor("SendMediaRequest"))
    )
    telethon_module = ModuleType("telethon")
    telethon_module.functions = functions
    telethon_module.helpers = SimpleNamespace(generate_random_long=lambda: 123456)
    telethon_module.types = media_types
    monkeypatch.setitem(sys.modules, "telethon", telethon_module)

    response = asyncio.run(telegram_ops.post_poll_to_channel(
        "@poll_channel", "Choose a category?", ["Fashion", "Tech"], allow_multiple=True
    ))
    request = fake_client.request
    poll = request["media"]["poll"]
    assert response == {"ok": True}
    assert request["type"] == "SendMediaRequest"
    assert request["peer"] == ("peer", "@poll_channel")
    assert poll["multiple_choice"] is True
    assert [answer["option"] for answer in poll["answers"]] == [b"\x00", b"\x01"]
    assert [answer["text"]["text"] for answer in poll["answers"]] == ["Fashion", "Tech"]
