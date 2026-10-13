"""Deal sources: the default source, the multi-select picker, and no duplicates.

The operator's asks, in their words: "pakkana source channel kuda vundlai em
select cheyakapothe defaults gaa https://t.me/LootZoneIndia11 … pakka manam set
chesina source channels pakkana multiple ga select cheskunelaga vundlai" and
"multiple posts same products asalu same channel lo cheyoddu … 1 hours lo same
products vasthe skip cheyali".

This module pins:

* :mod:`influencer_hub.sources` — the default source, the token a tick stores,
  normalization of anything an operator pastes, and the one-click "make sure the
  default source exists";
* the guided wizard — the source picker beside the channels, the default when
  nothing is ticked, "every source" only when asked for;
* the duplicate guards that keep the same product out of the same channel twice.
"""
from __future__ import annotations

import re

import pytest

from influencer_hub import config, db, sources


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """A real hub (CSRF on), the same shape the guided tests use."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sources.sqlite3")
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
        yield test_client
        test_client.get("/logout")


def _csrf(client, url="/guided") -> str:
    page = client.get(url).get_data(as_text=True)
    return re.search(r'name="_csrf_token" value="([^"]+)"', page).group(1)


# --------------------------------------------------------------------------
# 1. The default source
# --------------------------------------------------------------------------
def test_the_default_source_is_the_one_the_owner_named():
    assert sources.DEFAULT_SPEC == "https://t.me/LootZoneIndia11"
    assert sources.default_token() in {"LootZoneIndia11", "lootzoneindia11"}


def test_token_for_accepts_every_shape_an_operator_pastes():
    for raw in ("https://t.me/LootZoneIndia11", "@LootZoneIndia11", "LootZoneIndia11",
                "t.me/LootZoneIndia11", "https://t.me/LootZoneIndia11/"):
        assert sources.token_for(raw).lower() == "lootzoneindia11"
    assert sources.token_for("https://t.me/+AbC123") == "+AbC123"     # private invite
    assert sources.token_for("") == ""


def test_normalize_selection_stores_plain_tokens():
    assert sources.normalize_selection(
        "https://t.me/LootZoneIndia11, @powerloot\nmyntra_loots, , lootzoneindia11"
    ) == "LootZoneIndia11, powerloot, myntra_loots"
    assert sources.selection_tokens("") == []
    assert sources.selection_line("") == "every source in the hub"
    assert sources.selection_line("lootzoneindia11") == "lootzoneindia11"


def test_ensure_default_adds_the_source_once_and_names_what_it_did():
    first = sources.ensure_default()
    assert first["added"] is True and first["source_id"]
    assert [row["spec"] for row in db.list_sources(active_only=False)] == [sources.DEFAULT_SPEC]

    again = sources.ensure_default()
    assert again["added"] is False and again["source_id"] == first["source_id"]
    assert len(db.list_sources(active_only=False)) == 1


def test_a_creator_posts_from_the_default_source_without_selecting_anything():
    inf_id = db.add_influencer("Ravi Loots", "ravi-21",
                               allowed_sources=sources.default_token())
    profile = db.get_influencer(inf_id)
    from influencer_hub import pipeline

    # The source filter the pipeline applies matches by token, either direction.
    assert pipeline._is_source_allowed("LootZoneIndia11", profile["allowed_sources"])
    assert pipeline._is_source_allowed("lootzoneindia11", profile["allowed_sources"])
    assert not pipeline._is_source_allowed("SomeoneElseLoots", profile["allowed_sources"])
    # Nothing selected means every source; the token above is the opposite.
    assert pipeline._is_source_allowed("Anyone", "")


def test_choice_rows_list_the_configured_sources_and_the_tokens_in_use():
    sources.ensure_default()
    db.add_source("Power Loots", "@powerloot")
    db.add_influencer("Ravi", "ravi-21", allowed_sources="lootzoneindia11")
    db.add_influencer("Sita", "sita-21", allowed_sources="legacy_token")

    rows = {row["token"].lower(): row for row in sources.choice_rows()}
    assert rows["lootzoneindia11"]["is_default"] is True
    assert rows["lootzoneindia11"]["used_by"] == 1
    assert rows["powerloot"]["label"] == "Power Loots"
    # A token a creator already uses is never silently lost by the picker.
    assert rows["legacy_token"]["kind"] == "in use"


# --------------------------------------------------------------------------
# 2. The picker in the guided wizard
# --------------------------------------------------------------------------
def test_the_guided_channels_step_shows_the_picker(client):
    token = _csrf(client)
    client.post("/guided/creator",
                data={"_csrf_token": token, "name": "Ravi Loots", "amazon_tag": "ravi-21"})
    page = client.get("/guided?creator=1&step=channels").get_data(as_text=True)
    assert "Deal sources" in page and 'name="sources"' in page
    assert 'name="sources_all"' in page
    assert "LootZoneIndia11" in page           # the default row is offered


def test_saving_channels_without_ticking_any_source_uses_the_default(client):
    token = _csrf(client)
    client.post("/guided/creator",
                data={"_csrf_token": token, "name": "Ravi Loots", "amazon_tag": "ravi-21"})

    page = client.get("/guided?creator=1&step=channels").get_data(as_text=True)
    assert "LootZoneIndia11" in page and "default" in page
    client.post("/guided/channels",
                data={"_csrf_token": token, "creator": "1",
                      "main_channel": "@ravi_loots"})

    profile = db.get_influencer(1)
    assert profile["allowed_sources"] == sources.default_token()
    # …and the default source really exists, so the worker can read it.
    assert [row["spec"] for row in db.list_sources(active_only=False)] == [sources.DEFAULT_SPEC]


def test_ticking_several_sources_saves_all_of_them(client):
    token = _csrf(client)
    db.add_source("Power Loots", "@powerloot")
    client.post("/guided/creator",
                data={"_csrf_token": token, "name": "Ravi Loots", "amazon_tag": "ravi-21"})
    client.post("/guided/channels",
                data={"_csrf_token": token, "creator": "1", "main_channel": "@ravi_loots",
                      "sources": ["LootZoneIndia11", "powerloot"]})

    assert db.get_influencer(1)["allowed_sources"] == "LootZoneIndia11, powerloot"


def test_every_source_is_still_possible_when_asked_for_explicitly(client):
    token = _csrf(client)
    client.post("/guided/creator",
                data={"_csrf_token": token, "name": "Ravi Loots", "amazon_tag": "ravi-21"})
    client.post("/guided/channels",
                data={"_csrf_token": token, "creator": "1", "main_channel": "@ravi_loots",
                      "sources_all": "1"})
    assert db.get_influencer(1)["allowed_sources"] == ""


def test_a_pasted_source_url_is_stored_as_a_token(client):
    token = _csrf(client)
    client.post("/guided/creator",
                data={"_csrf_token": token, "name": "Ravi Loots", "amazon_tag": "ravi-21"})
    client.post("/guided/channels",
                data={"_csrf_token": token, "creator": "1", "main_channel": "@ravi_loots",
                      "sources_text": "https://t.me/LootZoneIndia11, @powerloot"})
    assert db.get_influencer(1)["allowed_sources"] == "LootZoneIndia11, powerloot"


def test_setup_offers_the_default_source_in_one_click(client):
    page = client.get("/setup").get_data(as_text=True)
    assert "/sources/ensure-default" in page
    token = _csrf(client, "/setup")
    client.post("/sources/ensure-default", data={"_csrf_token": token})
    assert [row["spec"] for row in db.list_sources(active_only=False)] == [sources.DEFAULT_SPEC]
    page = client.get("/setup").get_data(as_text=True)
    assert "default: LootZoneIndia11" in page


# --------------------------------------------------------------------------
# 3. No duplicates in the same channel
# --------------------------------------------------------------------------
def test_the_same_product_is_not_posted_twice_to_the_same_channel(monkeypatch):
    import asyncio

    from unittest.mock import AsyncMock

    from influencer_hub import pipeline

    posted: list[str] = []

    async def fake_dispatch(_influencer, _channel, text):
        posted.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    inf_id = db.add_influencer("Ravi Loots", "ravi-21",
                               allowed_sources=sources.default_token())
    channel = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                             role="broadcast")
    deal = ("🔥 BOAT AIRDOPES 141 at ₹1299\n"
            "https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21")

    first = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[inf_id],
                                                     source_channel="LootZoneIndia11"))
    # The same product again — a different source post, a different tracker,
    # even a different wording: it must not reach the channel a second time.
    second = asyncio.run(pipeline.render_and_dispatch(
        deal.replace("BOAT AIRDOPES 141", "boAt Airdopes 141 Earbuds")
            .replace("tag=someoneelse-21", "tag=another-21"),
        influencer_ids=[inf_id], source_channel="LootZoneIndia11"))

    assert first[inf_id][channel] == "posted"
    assert second[inf_id][channel].startswith("skipped")
    assert len(posted) == 1
    reasons = {row["status"] for row in db.skip_reasons()}
    assert any("dedup" in reason or "duplicate" in reason for reason in reasons), reasons
    # And the delivery log names the guard that stopped it.
    assert db.list_recent_posts(hours=1)[0]["status"].startswith("skipped")


def test_a_deal_from_a_source_the_creator_does_not_use_is_skipped(monkeypatch):
    import asyncio

    from unittest.mock import AsyncMock

    from influencer_hub import pipeline

    posted: list[str] = []

    async def fake_dispatch(_influencer, _channel, text):
        posted.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    inf_id = db.add_influencer("Ravi Loots", "ravi-21",
                               allowed_sources="LootZoneIndia11")
    channel = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                             role="broadcast")
    deal = "🔥 Deal https://www.amazon.in/dp/B0ABCDEFGH?tag=someoneelse-21"

    other = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[inf_id],
                                                    source_channel="SomeOtherLootZone"))
    assert other[inf_id][channel] == "skipped" and posted == []

    mine = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[inf_id],
                                                    source_channel="LootZoneIndia11"))
    assert mine[inf_id][channel] == "posted" and len(posted) == 1
