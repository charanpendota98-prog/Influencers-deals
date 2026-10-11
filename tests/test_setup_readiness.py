"""Setup readiness: the checklist that tells the operator what to do next.

Both the dashboard's command center and Easy Setup show the same read-only
engine (``influencer_hub.readiness``), and ``/api/readiness`` serves it as JSON.
These tests pin the honesty rules: a pending WhatsApp destination is not "live",
a fallback Amazon tag is not "the creator's own tag", the phone step follows the
stored session, the action buttons work from Easy Setup and come back to it, and
an off-site ``next`` value is ignored.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, readiness, whatsapp_client

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "readiness.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def _creator_with_channels(name="Ravi Loots", tag="ravi-21", *, wa_status="pending",
                           phone_status=None):
    inf_id = db.add_influencer(name, tag)
    db.add_channel(inf_id, "telegram", f"@{tag}_preview", status="ready", role="approval")
    db.add_channel(inf_id, "telegram", f"@{tag}_loots", status="ready", role="broadcast")
    wa_id = db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                           status=wa_status, role="whatsapp")
    # The session row always exists once a destination is saved: it is what the
    # readiness engine reads to answer "is the creator's phone linked?".
    key = f"inf-{inf_id}-wa"
    db.upsert_wa_session(inf_id, key, phone="919876543210")
    db.set_wa_session_status(key, phone_status or "offline")
    return inf_id, wa_id


def _step(creator: dict, step_id: str) -> dict:
    return next(step for step in creator["steps"] if step["id"] == step_id)


# --------------------------------------------------------------------------
# 1. The engine
# --------------------------------------------------------------------------
def test_the_headline_step_is_a_mandatory_one_never_an_optional_extra(client):
    inf_id = db.add_influencer("Fresh Creator", "fresh-21")

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["next_step"]["id"] == "main"          # mandatory: post somewhere
    assert creator["ready"] is False
    assert _step(creator, "approval")["state"] == "pending"  # optional, not "next"
    assert _step(creator, "tag")["done"] is True
    assert "fresh-21" in _step(creator, "tag")["detail"]


def test_the_phone_step_reports_not_linked_after_the_destination_exists(client):
    inf_id, _ = _creator_with_channels(phone_status=None)

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["next_step"]["id"] == "phone"
    assert _step(creator, "whatsapp")["done"] is True   # the link IS saved
    assert _step(creator, "activate")["done"] is False  # still Pending
    assert _step(creator, "mirror")["state"] == "pending"


def test_linking_the_phone_moves_the_headline_to_the_visible_test_post(client):
    inf_id, _ = _creator_with_channels(phone_status="connected")

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["next_step"]["id"] == "activate"
    assert "Test post" in creator["next_step"]["action"]
    assert creator["ready"] is False          # a Pending target is not live yet


def test_the_shared_default_tag_is_flagged_as_a_warning_not_a_silent_success(client):
    """The DB always stores some tag, so the honest signal is "is it THEIRS?"."""
    inf_id = db.add_influencer("Tagless Creator", config.AMAZON_ASSOCIATE_TAG)

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["own_tag"] is True          # deals still post and still earn
    assert creator["tag_is_shared_default"] is True
    tag_step = _step(creator, "tag")
    assert tag_step["done"] is True
    assert "shared" in tag_step["detail"]
    assert any("shared default tag" in message for message in creator["warnings"])

    plan = readiness.overview(influencers=[db.get_influencer(inf_id)])
    assert plan["summary"]["shared_tag"] == 1
    assert plan["warnings"] and plan["warnings"][0]["creator_id"] == inf_id


def test_a_personal_tag_produces_no_warnings(client):
    inf_id = db.add_influencer("Proper Creator", "proper-21")

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["own_tag"] is True
    assert creator["tag_is_shared_default"] is False
    assert creator["warnings"] == []
    assert "proper-21" in _step(creator, "tag")["detail"]


def test_a_finished_creator_reads_as_ready_and_live(client):
    inf_id, _ = _creator_with_channels(wa_status="ready", phone_status="connected")

    creator = readiness.creator_readiness(db.get_influencer(inf_id))

    assert creator["ready"] is True
    assert creator["next_step"] is None
    assert creator["mirror_active"] is True       # ready target + ready main channel
    assert "120363012345678901@newsletter" in _step(creator, "mirror")["detail"]


def test_overview_summarises_steps_and_names_the_hub_level_blockers(client):
    _creator_with_channels(phone_status="connected")
    db.add_influencer("Second Creator", "second-21")

    plan = readiness.overview()

    assert plan["summary"]["creators"] == 2
    assert plan["summary"]["ready"] == 0
    assert 0 < plan["summary"]["percent"] < 100
    assert plan["globals"]["sources"]["done"] is False     # no source configured
    assert plan["globals"]["worker"]["done"] is False      # no heartbeat ever
    labels = {action["label"] for action in plan["actions"]}
    assert {"Deal sources", "Worker running"} <= labels


# --------------------------------------------------------------------------
# 2. The dashboard command center
# --------------------------------------------------------------------------
def test_the_dashboard_shows_the_command_center_and_the_next_action(client):
    inf_id, _ = _creator_with_channels(phone_status=None)

    body = client.get("/").get_data(as_text=True)

    assert "Setup command center" in body
    assert "Ravi Loots" in body
    assert "ravi-21" in body
    assert "pair-wa-phone" not in body  # actions live on the creator page
    assert "/influencer/%d#whatsapp-pair" % inf_id in body
    assert "Do this next" in body


def test_api_readiness_is_read_only_and_can_be_narrowed_or_overlaid(client, monkeypatch):
    inf_id, _ = _creator_with_channels(phone_status=None)

    full = client.get("/api/readiness").get_json()
    assert full["ok"] is True
    assert {creator["id"] for creator in full["creators"]} == {inf_id}
    assert full["creators"][0]["wa_status"] == "offline"   # saved, not linked
    assert full["creators"][0]["next_step"]["id"] == "phone"

    # The hub is not involved unless asked; with live=1 the overlay is used and
    # nothing is written back to the database.
    def fake_snapshot(path):
        assert path == "/sessions"
        return {"ok": True, "sessions": [{"key": f"inf-{inf_id}-wa", "status": "connected"}]}

    monkeypatch.setattr(dashboard_module, "_wa_hub_get", fake_snapshot)
    live = client.get(f"/api/readiness?live=1&inf_id={inf_id}").get_json()

    assert live["live"] is True
    assert live["live_error"] == ""
    assert live["creators"][0]["wa_status"] == "connected"
    assert live["creators"][0]["next_step"]["id"] == "activate"
    # Read-only: the live overlay is never written back to the stored session.
    assert db.list_wa_sessions(inf_id)[0]["status"] == "offline"
    assert client.get("/api/readiness?inf_id=999999").status_code == 404


def test_a_broken_hub_never_hides_the_checks_the_dashboard_already_knows(client, monkeypatch):
    def breaking_snapshot(path):
        raise OSError("hub unreachable")

    monkeypatch.setattr(dashboard_module, "_wa_hub_get", breaking_snapshot)

    payload = client.get("/api/readiness?live=1").get_json()

    assert payload["ok"] is True
    assert "hub unreachable" in payload["live_error"]
    assert client.get("/").status_code == 200


# --------------------------------------------------------------------------
# 3. Easy Setup: pick a creator, see the next step, do it in place
# --------------------------------------------------------------------------
def test_easy_setup_lists_creators_with_their_readiness(client):
    inf_id, _ = _creator_with_channels(phone_status=None)

    body = client.get(f"/easy-setup?creator={inf_id}").get_data(as_text=True)

    assert "creator-pill" in body
    assert "Ravi Loots" in body
    assert "what is done, and what to do next" in body
    assert "step-track" in body
    assert "ravi-21" in body
    # The one-screen edit path prefills the saved channels and the saved tag.
    assert "@ravi-21_loots" in body
    assert "Get login code" in body
    assert f'data-key="inf-{inf_id}-wa"' in body


def test_easy_setup_offers_one_click_channel_creation_after_the_phone_is_linked(client):
    inf_id = db.add_influencer("Fresh WhatsApp Creator", "fresh-wa-21")
    db.add_channel(inf_id, "telegram", "@fresh_wa_loots", status="ready", role="broadcast")
    key = f"inf-{inf_id}-wa"
    db.upsert_wa_session(inf_id, key, phone="919876543210")
    db.set_wa_session_status(key, "offline")

    before = client.get(f"/easy-setup?creator={inf_id}").get_data(as_text=True)
    assert "Create WhatsApp Channel" not in before  # phone not linked yet

    db.set_wa_session_status(key, "connected")
    body = client.get(f"/easy-setup?creator={inf_id}").get_data(as_text=True)

    assert "Create WhatsApp Channel" in body
    assert f'action="/influencer/{inf_id}/create-newsletter"' in body


def test_easy_setup_offers_the_test_post_only_once_the_phone_is_linked(client):
    inf_id, wa_id = _creator_with_channels(phone_status=None)

    offline = client.get(f"/easy-setup?creator={inf_id}").get_data(as_text=True)
    assert "Send Test post" not in offline

    db.set_wa_session_status(f"inf-{inf_id}-wa", "connected")
    linked = client.get(f"/easy-setup?creator={inf_id}").get_data(as_text=True)

    assert "Send Test post" in linked
    assert f'action="/channel/{wa_id}/send-test"' in linked


def test_an_inline_action_comes_back_to_easy_setup_with_its_status(client, monkeypatch):
    inf_id, _ = _creator_with_channels(phone_status=None)

    async def refusing_request(session_key, phone, label=""):
        raise RuntimeError("WhatsApp is not reachable right now")

    monkeypatch.setattr(whatsapp_client, "request_pairing_code", refusing_request)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))

    response = client.post(
        f"/influencer/{inf_id}/pair-wa-phone",
        data={"pairing_phone": "919876543210",
              "next": f"/easy-setup?creator={inf_id}"},
    )

    location = response.headers["Location"]
    assert location.startswith(f"/easy-setup?creator={inf_id}")
    assert "wa_pair_status=failed" in location
    row = db.list_wa_sessions(inf_id)[0]
    assert row["pairing_error"] == "WhatsApp is not reachable right now"


def test_an_offsite_next_value_is_ignored(client, monkeypatch):
    inf_id, _ = _creator_with_channels(phone_status=None)
    def refusing(coro):
        coro.close()
        return {"ok": False, "error": "nope"}

    monkeypatch.setattr(dashboard_module, "_run", refusing)

    response = client.post(
        f"/influencer/{inf_id}/pair-wa-phone",
        data={"pairing_phone": "919876543210", "next": "//evil.example/steal"},
    )

    assert response.headers["Location"] == f"/influencer/{inf_id}?wa_pair_status=failed"


# --------------------------------------------------------------------------
# 4. Setup Center + the browser's live refresh
# --------------------------------------------------------------------------
def test_setup_center_cards_state_each_creators_next_step(client):
    _creator_with_channels("Blocked Creator", "blocked-21", phone_status=None)
    _creator_with_channels("Finished Creator", "finished-21", wa_status="ready",
                           phone_status="connected")

    body = client.get("/setup").get_data(as_text=True)

    assert "creator-next" in body
    assert "Phone paired" in body                # the blocked creator's next label
    assert "ready · " in body                    # the finished creator's step count
    assert "dashboard command center" in body    # points at the priority list


def test_the_browser_refreshes_the_same_readiness_endpoint():
    """The live UI must read the read-only JSON API, never a write endpoint."""
    script = (REPO_ROOT / "dashboard" / "static" / "app.js").read_text(encoding="utf-8")

    assert "data-live-src" in script
    assert "command-center" in script
    assert "next-steps" in script
    assert "&inf_id=" in script        # narrows the same endpoint to one creator

    # GET-only: the readiness pollers never post, so the browser can never
    # create a session, send a message or flip a switch by itself.
    pollers = script.split("Setup command center: live readiness")[-1]
    assert "POST" not in pollers
    assert "fetch(src" in pollers
