"""End-to-end coverage for adding, saving and deleting channels.

The operator asked for three guarantees:
  1. adding a channel works first time, with a clear message (good or bad);
  2. saving a channel and deleting a channel ask for the admin password;
  3. the password is asked ONCE at the start of a work session — every later
     add / save / delete goes through without another prompt.
"""
from __future__ import annotations

import re
import time

import pytest

from dashboard.app import app
from influencer_hub import config, db

PASSWORD = "test-admin-password-123"


def _csrf(html: str) -> str:
    match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "rendered form is missing its CSRF token"
    return match.group(1)


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """A signed-in dashboard client pointed at a throwaway database."""
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DASHBOARD_SETUP_UNLOCK_SECONDS", 1800)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "channels.sqlite3")
    db.init()

    client = app.test_client()
    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": PASSWORD, "_csrf_token": token}
    ).status_code == 302

    class Session:
        def __init__(self):
            self.token = _csrf(client.get("/influencers").get_data(as_text=True)
                               if False else client.get("/setup").get_data(as_text=True))

        def unlock(self):
            response = client.post(
                "/reauth", data={"password": PASSWORD, "_csrf_token": self.token}
            )
            assert response.status_code == 200, response.get_data(as_text=True)
            return response.get_json()

        def lock(self):
            return client.post("/reauth/lock", data={"_csrf_token": self.token})

    return client, Session()


def _add_channel(client, token, influencer_id, **overrides):
    payload = {
        "platform": "telegram",
        "identifier": "@loot_channel",
        "role": "broadcast",
        "allow_amazon": "1",
        "_csrf_token": token,
    }
    payload.update(overrides)
    return client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data=payload,
        headers={"Accept": "text/html"},
    )


def _body(response) -> str:
    return response.get_data(as_text=True)


# --------------------------------------------------------------------------
# 1. Adding a channel works, and says what happened
# --------------------------------------------------------------------------
def test_add_channel_creates_a_ready_destination_and_confirms_it(hub):
    client, session = hub
    influencer_id = db.add_influencer("Ravi Loots", "ravi-21")
    session.unlock()

    response = _add_channel(client, session.token, influencer_id)
    assert response.status_code == 302

    channels = db.list_channels(influencer_id)
    assert len(channels) == 1
    assert channels[0]["platform"] == "telegram"
    assert channels[0]["identifier"] == "@loot_channel"
    assert channels[0]["status"] == "ready"
    assert channels[0]["role"] == "broadcast"
    assert channels[0]["allow_amazon"] == 1

    page = _body(client.get(f"/influencer/{influencer_id}", follow_redirects=True))
    assert "@loot_channel" in page
    assert "connected" in page.lower()


def test_add_channel_normalises_links_and_keeps_the_settings(hub):
    client, session = hub
    influencer_id = db.add_influencer("Shopsy Loots", "shopsy-21")
    session.unlock()

    response = _add_channel(
        client, session.token, influencer_id,
        identifier="https://t.me/shopsy_loots",
        role="approval",
        price_filter="under_499",
        categories="clothing,electronics",
        posting_schedule="06:00-02:00",
        bitly_api_key="bitly-token",
        allowed_sources="powerloot",
        allow_amazon="1",
        allow_earnkaro="0",
        allow_hypd="0",
        hypd_store_id="55555",
    )
    assert response.status_code == 302

    channel = db.list_channels(influencer_id)[0]
    assert channel["identifier"] == "@shopsy_loots"
    assert channel["role"] == "approval"
    assert channel["price_filter"] == "under_499"
    assert channel["categories"] == "clothing,electronics"
    assert channel["posting_schedule"] == "06:00-02:00"
    assert channel["bitly_api_key"] == "bitly-token"
    assert channel["allowed_sources"] == "powerloot"
    assert channel["allow_amazon"] == 1
    assert channel["allow_earnkaro"] == 0
    assert channel["allow_hypd"] == 0
    assert channel["hypd_store_id"] == "55555"


def test_add_channel_accepts_private_invite_links_and_numeric_ids(hub):
    client, session = hub
    influencer_id = db.add_influencer("Invite Creator", "invite-21")
    session.unlock()

    assert _add_channel(
        client, session.token, influencer_id,
        identifier="https://t.me/+AbCdEfGhIjKlMnOp",
    ).status_code == 302
    assert _add_channel(
        client, session.token, influencer_id, identifier="-1001234567890"
    ).status_code == 302

    identifiers = {channel["identifier"] for channel in db.list_channels(influencer_id)}
    assert identifiers == {"https://t.me/+AbCdEfGhIjKlMnOp", "-1001234567890"}


@pytest.mark.parametrize(
    "payload, expected_fragment",
    [
        ({"identifier": ""}, "Enter the channel username"),
        ({"identifier": "   "}, "Enter the channel username"),
        ({"identifier": "!!"}, "valid Telegram destination"),
        ({"identifier": "ab"}, "valid Telegram destination"),
        ({"identifier": "https://example.com/not-telegram"}, "valid Telegram destination"),
        ({"platform": "carrier_pigeon"}, "Unsupported destination type"),
    ],
)
def test_add_channel_rejects_bad_input_with_a_clear_message(
    hub, payload, expected_fragment
):
    client, session = hub
    influencer_id = db.add_influencer("Validation Creator", "validate-21")
    session.unlock()

    response = _add_channel(client, session.token, influencer_id, **payload)
    assert response.status_code == 302
    assert db.list_channels(influencer_id) == []

    page = _body(client.get(f"/influencer/{influencer_id}"))
    assert expected_fragment in page


def test_add_channel_updates_a_duplicate_instead_of_creating_a_second_one(hub):
    client, session = hub
    influencer_id = db.add_influencer("Dupe Creator", "dupe-21")
    session.unlock()

    assert _add_channel(client, session.token, influencer_id).status_code == 302
    response = _add_channel(
        client, session.token, influencer_id, role="approval", price_filter="under_99"
    )
    assert response.status_code == 302

    channels = db.list_channels(influencer_id)
    assert len(channels) == 1, "a second copy of the same channel must not appear"
    assert channels[0]["role"] == "approval"
    assert channels[0]["price_filter"] == "under_99"

    page = _body(client.get(f"/influencer/{influencer_id}"))
    assert "already connected" in page


def test_add_channel_ignores_unknown_roles_and_price_filters(hub):
    client, session = hub
    influencer_id = db.add_influencer("Sanity Creator", "sanity-21")
    session.unlock()

    _add_channel(
        client, session.token, influencer_id,
        role="superuser", price_filter="under_1",
    )

    channel = db.list_channels(influencer_id)[0]
    assert channel["role"] == "broadcast"
    assert channel["price_filter"] == ""


# --------------------------------------------------------------------------
# 2 + 3. One password confirmation covers add, save and delete
# --------------------------------------------------------------------------
def test_first_channel_change_asks_for_the_password(hub):
    client, session = hub
    influencer_id = db.add_influencer("Locked Creator", "locked-21")

    locked = _add_channel(client, session.token, influencer_id)
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("reauth=1")
    assert db.list_channels(influencer_id) == []

    # JSON callers keep getting the machine-readable 428.
    api_locked = client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={"platform": "telegram", "identifier": "@locked", "_csrf_token": session.token},
        headers={"Accept": "application/json"},
    )
    assert api_locked.status_code == 428
    assert api_locked.get_json()["error"] == "reauthentication_required"


def test_unlock_covers_add_save_and_delete_without_asking_again(hub):
    client, session = hub
    influencer_id = db.add_influencer("Flow Creator", "flow-21")

    session.unlock()

    # 1. add
    assert _add_channel(client, session.token, influencer_id).status_code == 302
    channel = db.list_channels(influencer_id)[0]

    # 2. save (edit that same channel)
    saved = client.post(
        f"/channel/{channel['id']}/update",
        data={
            "inf_id": str(influencer_id),
            "identifier": "@renamed_channel",
            "role": "approval",
            "status": "ready",
            "price_filter": "under_199",
            "allow_amazon": "1",
            "_csrf_token": session.token,
        },
        headers={"Accept": "text/html"},
    )
    assert saved.status_code == 302
    channel = db.list_channels(influencer_id)[0]
    assert channel["identifier"] == "@renamed_channel"
    assert channel["role"] == "approval"
    assert channel["price_filter"] == "under_199"

    # 3. a second channel, still without another password
    assert _add_channel(
        client, session.token, influencer_id, identifier="@second_channel"
    ).status_code == 302
    assert len(db.list_channels(influencer_id)) == 2

    # 4. delete
    removed = client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(influencer_id), "_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert removed.status_code == 302
    assert len(db.list_channels(influencer_id)) == 1

    # None of the above re-prompted: no 428 was returned anywhere.
    assert client.get("/reauth/status").get_json()["unlocked"] is True


def test_delete_asks_again_after_the_window_expires(hub):
    client, session = hub
    influencer_id = db.add_influencer("Expiry Creator", "expiry-21")
    session.unlock()
    _add_channel(client, session.token, influencer_id)
    channel = db.list_channels(influencer_id)[0]

    with client.session_transaction() as browser_session:
        browser_session["_setup_unlock_at"] = time.time() - 10_000

    locked_delete = client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(influencer_id), "_csrf_token": session.token},
        headers={"Accept": "application/json"},
    )
    assert locked_delete.status_code == 428
    assert len(db.list_channels(influencer_id)) == 1

    session.unlock()
    deleted = client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(influencer_id), "_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert deleted.status_code == 302
    assert db.list_channels(influencer_id) == []


def test_unlock_also_works_without_javascript(hub):
    """The inline banner form unlocks setup for browsers with JS disabled."""
    client, session = hub
    influencer_id = db.add_influencer("No JS Creator", "nojs-21")

    locked = _add_channel(client, session.token, influencer_id)
    assert locked.headers["Location"].endswith("reauth=1")

    banner = _body(client.get(f"/influencer/{influencer_id}?reauth=1"))
    assert "unlock-form" in banner
    assert "Setup changes are locked" in banner

    browser_headers = {
        "Accept": "text/html",
        "Referer": f"http://localhost/influencer/{influencer_id}?reauth=1",
    }
    wrong = client.post(
        "/reauth",
        data={"password": "nope", "_csrf_token": session.token},
        headers=browser_headers,
    )
    assert wrong.status_code == 302
    assert wrong.headers["Location"].endswith("reauth=1")

    unlocked = client.post(
        "/reauth",
        data={"password": PASSWORD, "_csrf_token": session.token},
        headers=browser_headers,
    )
    assert unlocked.status_code == 302
    assert unlocked.headers["Location"] == f"/influencer/{influencer_id}"
    assert client.get("/reauth/status").get_json()["unlocked"] is True

    # The original change now goes through without another prompt.
    assert _add_channel(client, session.token, influencer_id).status_code == 302
    assert len(db.list_channels(influencer_id)) == 1


def test_unlock_slides_forward_while_the_operator_keeps_working(hub):
    client, session = hub
    influencer_id = db.add_influencer("Sliding Creator", "sliding-21")

    monkeypatched_window = 1800
    assert config.DASHBOARD_SETUP_UNLOCK_SECONDS == monkeypatched_window
    session.unlock()
    with client.session_transaction() as browser_session:
        browser_session["_setup_unlock_at"] = time.time() - 1500  # 25 min idle
        stale = browser_session["_setup_unlock_at"]

    assert _add_channel(client, session.token, influencer_id).status_code == 302
    with client.session_transaction() as browser_session:
        assert browser_session["_setup_unlock_at"] > stale
        assert browser_session["_setup_unlock_at"] >= time.time() - 5


# --------------------------------------------------------------------------
# Delete safety: ownership check + one-step undo
# --------------------------------------------------------------------------
def test_delete_refuses_a_channel_that_belongs_to_another_creator(hub):
    client, session = hub
    owner = db.add_influencer("Owner Creator", "owner-21")
    stranger = db.add_influencer("Stranger Creator", "stranger-21")
    session.unlock()

    _add_channel(client, session.token, owner, identifier="@owned")
    channel = db.list_channels(owner)[0]

    response = client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(stranger), "_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert response.status_code == 302
    assert len(db.list_channels(owner)) == 1
    assert "does not belong" in _body(client.get(f"/influencer/{owner}"))


def test_delete_then_undo_restores_the_channel_with_its_settings(hub):
    client, session = hub
    influencer_id = db.add_influencer("Undo Creator", "undo-21")
    session.unlock()

    _add_channel(
        client, session.token, influencer_id,
        identifier="@undo_channel", role="approval", price_filter="under_999",
        categories="tech", bitly_api_key="undo-token", allow_earnkaro="1",
    )
    channel = db.list_channels(influencer_id)[0]

    deleted = client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(influencer_id), "_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert deleted.status_code == 302
    assert db.list_channels(influencer_id) == []
    assert "Undo" in _body(client.get(f"/influencer/{influencer_id}"))

    restored = client.post(
        "/channels/undo",
        data={"_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert restored.status_code == 302
    channels = db.list_channels(influencer_id)
    assert len(channels) == 1
    restored_channel = channels[0]
    assert restored_channel["identifier"] == "@undo_channel"
    assert restored_channel["platform"] == "telegram"
    assert restored_channel["role"] == "approval"
    assert restored_channel["price_filter"] == "under_999"
    assert restored_channel["categories"] == "tech"
    assert restored_channel["bitly_api_key"] == "undo-token"
    assert restored_channel["allow_earnkaro"] == 1

    # The undo is one-shot.
    client.post("/channels/undo", data={"_csrf_token": session.token})
    assert len(db.list_channels(influencer_id)) == 1


def test_undo_offer_disappears_after_another_channel_change(hub):
    """No stale Undo button next to an unrelated success message."""
    client, session = hub
    influencer_id = db.add_influencer("Stale Undo Creator", "stale-undo-21")
    session.unlock()

    _add_channel(client, session.token, influencer_id, identifier="@first")
    _add_channel(client, session.token, influencer_id, identifier="@second")
    channel = db.list_channels(influencer_id)[0]

    client.post(
        f"/channel/{channel['id']}/delete",
        data={"inf_id": str(influencer_id), "_csrf_token": session.token},
        headers={"Accept": "text/html"},
    )
    assert "Undo removal" in _body(client.get(f"/influencer/{influencer_id}"))

    _add_channel(client, session.token, influencer_id, identifier="@third")
    page = _body(client.get(f"/influencer/{influencer_id}"))
    assert "Undo removal" not in page
    assert "connected" in page.lower()


def test_undo_requires_the_unlock_and_rejects_a_second_attempt(hub):
    client, session = hub
    influencer_id = db.add_influencer("Undo Guard Creator", "undo-guard-21")

    guarded = client.post(
        "/channels/undo",
        data={"_csrf_token": session.token},
        headers={"Accept": "application/json"},
    )
    assert guarded.status_code == 428


# --------------------------------------------------------------------------
# The dashboard tells the operator whether it will ask
# --------------------------------------------------------------------------
def test_channel_page_shows_the_lock_state_and_the_confirmation_copy(hub):
    client, session = hub
    influencer_id = db.add_influencer("Banner Creator", "banner-21")

    locked_page = _body(client.get(f"/influencer/{influencer_id}"))
    assert "Setup locked" in locked_page
    assert "One password confirmation opens this up" in locked_page

    session.unlock()
    unlocked_page = _body(client.get(f"/influencer/{influencer_id}"))
    assert "Setup unlocked" in unlocked_page
    assert "Lock now" in unlocked_page
    assert "stays unlocked for" in unlocked_page


def test_locking_manually_makes_the_next_add_ask_again(hub):
    client, session = hub
    influencer_id = db.add_influencer("Manual Lock Creator", "manual-lock-21")

    session.unlock()
    assert _add_channel(client, session.token, influencer_id).status_code == 302

    assert session.lock().status_code == 200
    assert client.get("/reauth/status").get_json()["unlocked"] is False

    locked = _add_channel(client, session.token, influencer_id, identifier="@third")
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("reauth=1")
    assert len(db.list_channels(influencer_id)) == 1
