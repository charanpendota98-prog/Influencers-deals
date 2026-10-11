"""Dashboard controls for the mirror: the global switch and the per-channel one.

These pin the operator-facing half of "main channel -> WhatsApp": an explicit
Mirror: OFF has to survive a save, "" stays "inherit the global switch", the
global switch is written without disturbing the unrelated universal-button
settings, and the creator page states the mirror's real state (sources,
destinations, mirrored posts) instead of only showing a checkbox.
"""
from __future__ import annotations

import re

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "mirror-switch.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def _channel(channel_id: int) -> dict:
    return next(ch for ch in db.list_channels() if ch["id"] == channel_id)


def test_per_channel_mirror_switch_saves_all_three_states(client):
    inf_id = db.add_influencer("Switch Creator", "switch-21")
    channel_id = db.add_channel(
        inf_id, "telegram", "@switch_main", status="ready", role="broadcast",
    )

    client.post(f"/channel/{channel_id}/update",
                data={"inf_id": str(inf_id), "mirror_whatsapp": "0"})
    assert _channel(channel_id)["mirror_whatsapp"] == "0"

    client.post(f"/channel/{channel_id}/update",
                data={"inf_id": str(inf_id), "mirror_whatsapp": "1"})
    assert _channel(channel_id)["mirror_whatsapp"] == "1"

    # An empty value means "follow the global switch" again.
    client.post(f"/channel/{channel_id}/update",
                data={"inf_id": str(inf_id), "mirror_whatsapp": ""})
    assert _channel(channel_id)["mirror_whatsapp"] == ""

    # A WhatsApp destination has no mirror switch; its row must stay untouched.
    wa_id = db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                           status="ready", role="whatsapp", mirror_whatsapp="1")
    client.post(f"/channel/{wa_id}/update",
                data={"inf_id": str(inf_id), "identifier": "120363012345678901@newsletter"})
    assert _channel(wa_id)["mirror_whatsapp"] == "1"


def test_global_mirror_switch_is_saved_without_touching_the_button_settings(client):
    db.set_global_setting("nav_button_text", "Join Shpsy Loots ❤️")
    db.set_global_setting("nav_button_url", "https://t.me/shpsy_loots")

    response = client.post("/global-settings/update",
                           data={"mirror_main_to_whatsapp": "0"})

    assert response.status_code == 302
    assert db.get_global_setting("mirror_main_to_whatsapp") == "0"
    # The button card was not on the form, so its settings must survive intact.
    assert db.get_global_setting("nav_button_text") == "Join Shpsy Loots ❤️"
    assert db.get_global_setting("nav_button_url") == "https://t.me/shpsy_loots"

    client.post("/global-settings/update", data={"mirror_main_to_whatsapp": "1"})
    assert db.get_global_setting("mirror_main_to_whatsapp") == "1"

    # Saving the universal-button card must not silently flip the mirror.
    client.post("/global-settings/update", data={
        "nav_button_enabled": "1",
        "nav_button_text": "Join Now",
        "nav_button_url": "https://t.me/other",
    })
    assert db.get_global_setting("mirror_main_to_whatsapp") == "1"
    assert db.get_global_setting("nav_button_text") == "Join Now"


def test_creator_page_states_the_mirror_facts(client, monkeypatch):
    inf_id = db.add_influencer("Card Creator", "card-21")
    db.add_channel(inf_id, "telegram", "@card_main", status="ready", role="broadcast")
    db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                   status="ready", role="whatsapp")
    monkeypatch.setattr(dashboard_module.whatsapp_client, "session_status", lambda _key: None)
    monkeypatch.setattr(dashboard_module.whatsapp_client, "pairing_state", lambda _key: None)
    monkeypatch.setattr(dashboard_module, "_run", lambda _coro: {"status": "offline"})

    body = client.get(f"/influencer/{inf_id}").get_data(as_text=True)

    assert "Mirror: Main Channel → WhatsApp" in body
    assert "ACTIVE" in body
    assert "@card_main" in body
    assert "120363012345678901@newsletter" in body
    # The phone-linking (OTP) path sits next to the QR path.
    assert 'action="/influencer/%d/pair-wa-phone"' % inf_id in body
    assert "Get Login Code" in body
    assert 'id="pairbox"' in body
    assert re.search(r'name="mirror_whatsapp"', body)
