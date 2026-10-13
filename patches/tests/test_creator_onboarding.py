"""Adding a creator with everything on one screen, then their WhatsApp channel.

The operator's ask, in their words: "add influencer click chesthe anni fill
chestham … whatsapp channel kuda add cheyali ante valladi scan or code tho
connect chesaka vallu admin ga vunna channel ni isthanu adi save cheskovali ah
channel lo post velthu vundali … pakkana source channel select cheskunelaga
vundlai multiplega kudaa … channel names source channel names chupinchali
dropdown lo".

So this module pins:

* the add-creator form carries **everything** (identity, tag, all three
  destinations, networks, categories, hours, sources, mirror) and the source
  picker shows **names** and accepts many at once;
* a WhatsApp channel is saved with its **own name**, and the dropdown the
  operator picks from lists the chats where the paired number is **admin**;
* picking a channel from the dropdown is what makes posts flow — a saved
  destination is `pending` until the test send succeeds, and the mirror flag
  travels with it;
* the send gap is a random 60–80 seconds ("60-80 seconds random gaps").
"""
from __future__ import annotations

import re

import pytest

from influencer_hub import config, db, pipeline, sources, whatsapp_client


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "onboard.sqlite3")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "owner-password-16chars")
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_COOKIE_SECURE", False)
    db.init()
    import dashboard.app as dashboard_module

    monkeypatch.setattr(dashboard_module.app, "testing", False)
    dashboard_module.app.config.update(TESTING=False, WTF_CSRF_ENABLED=True)
    monkeypatch.setattr(dashboard_module, "_dashboard_secret_key_is_persistent",
                        lambda: True)
    with dashboard_module.app.test_client() as test_client:
        page = test_client.get("/login").get_data(as_text=True)
        token = re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)
        test_client.post("/login", data={"_csrf_token": token,
                                        "password": "owner-password-16chars"})
        test_client.dashboard_module = dashboard_module
        yield test_client
        test_client.get("/logout")


def _csrf(client, url="/setup"):
    page = client.get(url).get_data(as_text=True)
    return re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)


# --------------------------------------------------------------------------
# 1. one form, everything in it
# --------------------------------------------------------------------------
def test_the_add_form_carries_everything_including_sources_and_the_mirror(client):
    sources.ensure_default()
    db.add_source("Power Loots", "@powerloot")
    page = client.get("/setup").get_data(as_text=True)

    for field in ("name", "phone_number", "tag", "insta_id", "approval_tg",
                  "broadcast_tg", "whatsapp_id", "whatsapp_name", "price_filter",
                  "categories", "posting_schedule", "only_amazon", "allow_amazon",
                  "allow_earnkaro", "allow_hypd", "hypd_store_id", "bitly_api_key",
                  "mirror_whatsapp"):
        assert f'name="{field}"' in page, f"the add form is missing {field}"

    # the source picker shows NAMES, and it can tick several at once
    assert 'name="sources"' in page
    assert "LootZoneIndia11" in page and "Power Loots" in page
    assert "Post from every source in the hub" in page
    assert "📡 Deal sources" in page


def test_the_picker_saves_several_named_sources_at_once(client):
    sources.ensure_default()
    db.add_source("Power Loots", "@powerloot")
    client.post("/quick-add", data={
        "_csrf_token": _csrf(client),
        "name": "Multi Source", "tag": "multisource-21",
        "sources": ["LootZoneIndia11", "powerloot"],
    })
    assert db.list_influencers()[0]["allowed_sources"] == "LootZoneIndia11, powerloot"


def test_typing_names_in_the_paste_box_works_too(client):
    sources.ensure_default()
    db.add_source("Power Loots", "@powerloot")
    client.post("/quick-add", data={
        "_csrf_token": _csrf(client),
        "name": "Typed Source", "tag": "typedsource-21",
        "sources_text": "Power Loots, https://t.me/LootZoneIndia11",
    })
    assert db.list_influencers()[0]["allowed_sources"] == "powerloot, LootZoneIndia11"


def test_one_line_add_creates_the_channels_and_the_mirror(client):
    sources.ensure_default()
    client.post("/quick-add", data={
        "_csrf_token": _csrf(client),
        "name": "One Line", "tag": "oneline-21", "phone_number": "919000098765",
        "approval_tg": "@oneline_amazon", "broadcast_tg": "@oneline_loots",
        "whatsapp_id": "120363001112223334@newsletter", "whatsapp_name": "One Line Loot Deals",
        "allow_amazon": "1", "make_it_live": "1",
    })
    profile = db.list_influencers()[0]
    channels = {channel["role"]: channel for channel in db.list_channels(profile["id"])}
    assert set(channels) == {"approval", "broadcast", "whatsapp"}
    assert channels["broadcast"]["mirror_whatsapp"] == "1", (
        "the main channel must mirror into WhatsApp unless the operator unticks it"
    )
    assert channels["whatsapp"]["title"] == "One Line Loot Deals"
    assert channels["whatsapp"]["status"] == "pending", (
        "a WhatsApp destination activates only after a successful test send"
    )


# --------------------------------------------------------------------------
# 2. the admin channel dropdown
# --------------------------------------------------------------------------
def test_the_pairing_card_offers_the_channels_they_are_admin_of(
        client, monkeypatch):
    inf_id = db.add_influencer("Admin Ravi", "adminravi-21")
    db.add_channel(inf_id, "telegram", "@adminravi_amazon", status="ready", role="approval")
    dashboard_module = client.dashboard_module
    monkeypatch.setattr(
        dashboard_module.whatsapp_client, "session_status",
        lambda _key: _done({"status": "connected", "phone": "919000011111"}))
    monkeypatch.setattr(
        dashboard_module.whatsapp_client, "list_chats",
        lambda _key: _done([{"jid": "120363000000000001@newsletter", "name": "Ravi Loot Deals",
                             "isGroup": False, "isChannel": True}]))
    monkeypatch.setattr(
        dashboard_module.whatsapp_client, "list_admin_chats",
        lambda _key: _done([{"jid": "120363000000000001@newsletter", "name": "Ravi Loot Deals",
                             "kind": "channel", "role": "owner", "members": 1200}]))

    page = client.get(f"/influencer/{inf_id}").get_data(as_text=True)
    assert "is ADMIN" in page
    assert "Ravi Loot Deals" in page
    assert "you are owner" in page
    assert "120363000000000001@newsletter|Ravi Loot Deals" in page, (
        "the option must carry the name so it can be saved with the JID"
    )
    assert "Main Telegram channel lo vachinavi anni WhatsApp channel lo kuda vellali" in page


def test_picking_a_channel_saves_its_name_and_the_mirror(client, monkeypatch):
    inf_id = db.add_influencer("Pick Ravi", "pickravi-21")
    dashboard_module = client.dashboard_module
    monkeypatch.setattr(
        dashboard_module.whatsapp_client, "list_admin_chats",
        lambda _key: _done([{"jid": "120363000000000002@newsletter",
                             "name": "Pick Loot Deals", "kind": "channel",
                             "role": "owner", "members": 55}]))

    client.post(f"/influencer/{inf_id}/wa-connect-chat", data={
        "_csrf_token": _csrf(client, f"/influencer/{inf_id}"),
        "chat_jid": "120363000000000002@newsletter|Pick Loot Deals",
        "mirror_whatsapp": "1",
    })
    channel = db.list_channels(inf_id)[0]
    assert channel["identifier"] == "120363000000000002@newsletter"
    assert channel["title"] == "Pick Loot Deals"
    assert channel["mirror_whatsapp"] == "1"
    assert channel["status"] == "pending"


def test_a_listing_failure_never_breaks_the_card(client, monkeypatch):
    """No hub, no phone: the card still renders and the link box still works."""
    inf_id = db.add_influencer("Offline Ravi", "offlineravi-21")

    async def boom(_key):
        raise RuntimeError("hub is not reachable")

    dashboard_module = client.dashboard_module
    monkeypatch.setattr(dashboard_module.whatsapp_client, "session_status", boom)
    page = client.get(f"/influencer/{inf_id}")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Paste WhatsApp Group/Channel Link" in body or "invite_link" in body


# --------------------------------------------------------------------------
# 3. the anti-ban gap
# --------------------------------------------------------------------------
def test_the_send_gap_is_a_random_sixty_to_eighty_seconds():
    values = [pipeline.whatsapp_gap_seconds() for _ in range(400)]
    assert all(60.0 <= value <= 80.0 for value in values), (min(values), max(values))
    assert len(set(round(value, 3) for value in values)) > 50, "the gap must vary"
    assert (config.WA_SEND_GAP_MIN, config.WA_SEND_GAP_MAX) == (60, 80)


def test_a_reversed_or_silly_window_is_repaired(monkeypatch):
    monkeypatch.setattr(config, "WA_SEND_GAP_MIN", 90)
    monkeypatch.setattr(config, "WA_SEND_GAP_MAX", 60)
    assert all(60.0 <= pipeline.whatsapp_gap_seconds() <= 90.0 for _ in range(50))


def test_the_admin_listing_is_soft_when_the_hub_is_down(monkeypatch):
    async def explode(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(whatsapp_client, "_request", explode)
    assert _run(whatsapp_client.list_admin_chats("inf-1")) == []


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _done(value):
    import asyncio

    async def _inner(*_args, **_kwargs):
        return value

    return _inner()
