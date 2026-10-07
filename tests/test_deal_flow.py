"""Deal flow: sources keep feeding, posts keep going out, and both are visible.

The engine already retries, holds a cursor on failure and heart-beats. What
these tests pin is the *flow* itself:

* every handled message records per-source activity (deals seen, posted, failed
  and why), so a quiet source is distinguishable from a broken one;
* a failed delivery holds the cursor and is counted as a failure, not silently
  swallowed;
* `/api/flow` and the Easy Setup card answer "is posting still flowing?" with
  worker state, per-source last-seen and per-channel post counts;
* a source that stopped producing, or a worker that stopped heart-beating, is
  reported as such instead of looking healthy.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import time
from unittest.mock import AsyncMock

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, worker

TEMPLATE = (
    pathlib.Path(__file__).resolve().parent.parent
    / "dashboard" / "templates" / "easy_setup.html"
)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "flow.sqlite3")
    db.init()
    return app.test_client()


# --------------------------------------------------------------------------
# db: per-source activity
# --------------------------------------------------------------------------
def test_source_activity_accumulates_and_remembers_the_last_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "activity.sqlite3")
    db.init()
    db.record_source_activity("@loot", message_id=5, posted=2)
    db.record_source_activity("@loot", message_id=9, failed=1, error="failed: flood")
    rows = {row["source_key"]: row for row in db.list_source_activity()}

    row = rows["@loot"]
    assert row["deals_seen"] == 2
    assert row["posts_dispatched"] == 2
    assert row["failures"] == 1
    assert row["last_error"] == "failed: flood"
    assert row["last_message_id"] == 9
    assert row["last_seen_at"] > 0

    # A cursor never moves backwards, even if an older message is replayed.
    db.record_source_activity("@loot", message_id=3, posted=1)
    assert db.get_worker_offset("@loot") == 0  # offsets are separate from activity
    row = next(r for r in db.list_source_activity() if r["source_key"] == "@loot")
    assert row["last_message_id"] == 9
    assert row["deals_seen"] == 3


def test_channel_post_activity_reports_last_post_and_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "channel-activity.sqlite3")
    db.init()
    influencer = db.add_influencer("Flow Creator", "flow-21")
    channel = db.add_channel(influencer, "telegram", "@flow", status="ready")
    db.record_post(influencer, channel, "sig-1", status="posted")
    db.record_post(influencer, channel, "sig-2", status="failed", error="boom")

    row = next(r for r in db.channel_post_activity(hours=24) if r["channel_id"] == channel)
    assert row["influencer_name"] == "Flow Creator"
    assert row["last_posted_at"] is not None
    assert row["posted_in_window"] == 1
    assert row["failed_total"] == 1


# --------------------------------------------------------------------------
# worker: every handled message is accounted for
# --------------------------------------------------------------------------
def _record(source: str, message_id: int, text: str = "deal") -> dict:
    return {
        "source_id": source,
        "message_id": message_id,
        "text": text,
        "source": source,
    }


def test_worker_records_posted_and_failed_per_source(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "worker-flow.sqlite3")
    db.init()

    async def fake_pull(limit=None, use_dummy=False):
        return [
            _record("@one", 10, "Deal one https://www.flipkart.com/p/itm123456"),
            _record("@two", 20, "Deal two https://www.amazon.in/dp/B0D9P2M1PB"),
        ]

    calls = {"n": 0}

    async def fake_run_once(deals):
        calls["n"] += 1
        if calls["n"] == 1:
            return {1: {7: "posted"}}
        return {1: {7: "failed: telegram flood wait"}}

    monkeypatch.setattr(worker.puller, "pull_new_deals", fake_pull)
    monkeypatch.setattr(worker.pipeline, "run_once", fake_run_once)

    stats = asyncio.run(worker.process_pending_batch(limit=10))
    assert stats["pulled"] == 2
    assert stats["handled"] == 1
    assert stats["retry_sources"] == 1

    rows = {row["source_key"]: row for row in db.list_source_activity()}
    assert rows["@one"]["posts_dispatched"] == 1
    assert rows["@one"]["failures"] == 0
    # The failed delivery is visible with its reason, and its cursor is held.
    assert rows["@two"]["failures"] == 1
    assert "flood wait" in rows["@two"]["last_error"]
    assert db.get_worker_offset("@one") == 10
    assert db.get_worker_offset("@two") == 0


def test_worker_counts_a_crash_as_a_failure_without_losing_the_cursor(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "worker-crash.sqlite3")
    db.init()

    async def fake_pull(limit=None, use_dummy=False):
        return [_record("@crash", 42, "Boom https://www.flipkart.com/p/itm999999")]

    async def fake_run_once(deals):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(worker.puller, "pull_new_deals", fake_pull)
    monkeypatch.setattr(worker.pipeline, "run_once", fake_run_once)

    stats = asyncio.run(worker.process_pending_batch(limit=10))
    assert stats["retry_sources"] == 1
    assert db.get_worker_offset("@crash") == 0
    row = next(r for r in db.list_source_activity() if r["source_key"] == "@crash")
    assert row["failures"] == 1
    assert "RuntimeError" in row["last_error"]


def test_worker_advances_media_only_messages_so_they_cannot_block_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "worker-media.sqlite3")
    db.init()

    async def fake_pull(limit=None, use_dummy=False):
        return [_record("@media", 77, "")]

    async def fake_run_once(deals):  # pragma: no cover - must not be called
        raise AssertionError("empty text has nothing to render")

    monkeypatch.setattr(worker.puller, "pull_new_deals", fake_pull)
    monkeypatch.setattr(worker.pipeline, "run_once", fake_run_once)

    stats = asyncio.run(worker.process_pending_batch(limit=10))
    assert stats["handled"] == 1
    assert db.get_worker_offset("@media") == 77


# --------------------------------------------------------------------------
# the flow board
# --------------------------------------------------------------------------
def test_flow_api_reports_healthy_flow(monkeypatch, tmp_path, hub):
    influencer = db.add_influencer("Flow Creator", "flow-21")
    channel = db.add_channel(influencer, "telegram", "@flow", status="ready")
    db.record_post(influencer, channel, "sig-1", status="posted")
    db.add_source("Live source", "@live_source")
    db.record_source_activity("@live_source", message_id=11, posted=1)
    db.record_worker_heartbeat("running", poll_completed=True)

    flow = hub.get("/api/flow").get_json()
    assert flow["ok"] is True
    assert flow["worker"]["alive"] is True
    assert flow["sources"]["active"] == 1
    assert flow["sources"]["delivered"] == 1
    assert flow["sources"]["stalled"] == 0
    assert flow["channels"]["posted_in_window"] == 1
    assert flow["notes"] == []
    row = flow["sources"]["rows"][0]
    assert row["spec"] == "@live_source"
    assert row["stalled"] is False
    assert row["never_delivered"] is False


def test_flow_api_flags_a_stalled_source_and_a_silent_worker(hub):
    db.add_source("Quiet source", "@quiet_source")
    # Delivered once, long ago: stalled.
    db.record_source_activity("@quiet_source", message_id=1, posted=1)
    with db._connect() as con:  # noqa: SLF001 - direct timestamp control in a test
        con.execute(
            "UPDATE source_activity SET last_seen_at=? WHERE source_key=?",
            (time.time() - 10 * 3600, "@quiet_source"),
        )
        con.commit()

    flow = hub.get("/api/flow?stall_seconds=3600").get_json()
    assert flow["sources"]["stalled"] == 1
    assert flow["worker"]["alive"] is False
    assert any("worker" in note.lower() for note in flow["notes"])
    assert any("stalled" in note.lower() or "delivered before" in note.lower()
               for note in flow["notes"])


def test_flow_api_flags_sources_that_never_delivered(hub):
    db.add_source("Never source", "@never_source")
    flow = hub.get("/api/flow").get_json()
    assert flow["sources"]["never_delivered"] == 1
    assert flow["sources"]["rows"][0]["never_delivered"] is True
    assert any("not delivered a deal yet" in note for note in flow["notes"])


def test_easy_setup_page_shows_the_flow_card(hub):
    db.add_source("Live source", "@live_source")
    db.record_source_activity("@live_source", message_id=11, posted=2)
    db.record_worker_heartbeat("running", poll_completed=True)
    page = hub.get("/easy-setup").get_data(as_text=True)

    assert "Deal flow — sources → posts" in page
    assert "/api/flow" in page
    assert "@live_source" in page
    assert "Worker <strong>running</strong>" in page


def test_flow_card_template_stays_machine_readable():
    """The card documents the JSON endpoint and both query knobs."""
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "/api/flow" in text
    assert "stall_seconds" in text
    assert "window_hours" in text


def test_flow_api_surfaces_the_switches_that_control_flow(hub):
    flow = hub.get("/api/flow").get_json()
    assert flow["settings"] == {
        "hourly_loot_enabled": False,
        "only_earning_deals": False,
    }
    db.set_global_setting("hourly_loot_enabled", "1")
    assert hub.get("/api/flow").get_json()["settings"]["hourly_loot_enabled"] is True


def test_flow_api_explains_an_idle_worker_with_quiet_channels(hub):
    influencer = db.add_influencer("Idle Creator", "idle-31")
    db.add_channel(influencer, "telegram", "@idle", status="ready")
    db.record_worker_heartbeat("running", poll_completed=True)

    flow = hub.get("/api/flow").get_json()
    assert any("hourly loot sweep is off" in note for note in flow["notes"])


def test_flow_api_is_read_only_and_json(hub):
    response = hub.get("/api/flow")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/json")
    assert json.loads(response.get_data(as_text=True))["ok"] is True


# --------------------------------------------------------------------------
# live source visibility: prove WHICH dialogs are actually read
# --------------------------------------------------------------------------
def test_flow_api_reports_the_live_telegram_selection(hub, monkeypatch):
    db.add_source("Priority Source", "https://t.me/+privatehash")
    report = {
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
        "selected_dialogs": [{"name": "Loot Group One", "id": "42", "username": "loot_one", "kind": "group"}],
        "selected_dialogs_truncated": 235,
    }
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(
        dashboard_module.puller, "inspect_source_selection",
        AsyncMock(return_value=report),
    )
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    flow = hub.get("/api/flow").get_json()

    live = flow["live"]
    assert live["checked"] is True
    assert live["selection_mode"] == "joined_dialog_fallback"
    assert live["selected_sources"] == 236
    assert live["dialogs"][0]["name"] == "Loot Group One"
    assert live["dialogs_truncated"] == 235
    assert any("private invite label(s)" in note for note in flow["notes"])
    assert any("Live check" in note for note in flow["notes"])


def test_flow_api_pins_the_live_selection_when_nothing_is_readable(hub, monkeypatch):
    db.add_source("Missing source", "@not_joined")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(
        dashboard_module.puller, "inspect_source_selection",
        AsyncMock(return_value={
            "ok": False,
            "authorized": True,
            "configured_sources": 1,
            "selected_sources": 0,
            "selection_mode": "configured_selectors",
        }),
    )
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    flow = hub.get("/api/flow").get_json()

    assert flow["live"]["selected_sources"] == 0
    assert any("no joined dialog to read" in note for note in flow["notes"])


def test_flow_api_skips_the_live_probe_on_request(hub, monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("live probe must be skipped with ?live=0")

    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "test-api-hash")
    monkeypatch.setattr(dashboard_module.puller, "inspect_source_selection", explode)

    flow = hub.get("/api/flow?live=0").get_json()

    assert flow["live"] == {"checked": False, "reason": "probe_disabled"}


def test_flow_card_names_the_dialog_a_source_actually_read(hub):
    db.add_source("Priority Source", "https://t.me/+privatehash")
    db.record_source_activity(
        "https://t.me/+privatehash", source_name="Real Loot Group", message_id=9, posted=1,
    )
    flow = hub.get("/api/flow?live=0").get_json()
    row = flow["sources"]["rows"][0]

    assert row["source_name"] == "Real Loot Group"
    page = hub.get("/easy-setup?live=0").get_data(as_text=True)
    assert "Real Loot Group" in page
