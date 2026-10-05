"""Easy Setup: one screen, three switches, and the routing they promise.

Rules the operator asked for, in one sentence:
  Amazon → only Amazon · EarnKaro → the other merchants · HYPD → Meesho.
"""
from __future__ import annotations

import re

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
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "easy.sqlite3")
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "")
    db.init()

    client = app.test_client()
    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": PASSWORD, "_csrf_token": token}
    ).status_code == 302

    class Session:
        def __init__(self):
            self.token = _csrf(client.get("/easy-setup").get_data(as_text=True))

        def unlock(self):
            response = client.post(
                "/reauth", data={"password": PASSWORD, "_csrf_token": self.token}
            )
            assert response.status_code == 200

    return client, Session()


def _setup(client, token, **overrides):
    payload = {
        "name": "Ravi Loots",
        "amazon_tag": "ravi-21",
        "approval_channel": "@ravi_amazon",
        "main_channel": "@ravi_loots",
        "allow_amazon": "1",
        "allow_earnkaro": "1",
        "allow_hypd": "1",
        "_csrf_token": token,
    }
    payload.update(overrides)
    return client.post("/easy-setup", data=payload, headers={"Accept": "text/html"})


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------
def test_easy_setup_page_is_reachable_and_explains_the_three_rules(hub):
    client, session = hub
    page = client.get("/easy-setup").get_data(as_text=True)
    assert "Easy Setup" in page
    for label in ("Amazon", "EarnKaro", "HYPD"):
        assert label in page
    # All three switches default to ON.
    assert page.count('checked') >= 3


def test_easy_setup_creates_creator_plus_both_channels_and_starts_posting(hub):
    client, session = hub
    session.unlock()

    response = _setup(client, session.token)
    assert response.status_code == 302

    influencer = next(
        profile for profile in db.list_influencers() if profile["name"] == "Ravi Loots"
    )
    assert influencer["amazon_tag"] == "ravi-21"
    assert influencer["active"] == 1
    assert influencer["allow_amazon"] == 1
    assert influencer["allow_earnkaro"] == 1
    assert influencer["allow_hypd"] == 1

    channels = {channel["role"]: channel for channel in db.list_channels(influencer["id"])}
    assert set(channels) == {"approval", "broadcast"}
    assert channels["approval"]["identifier"] == "@ravi_amazon"
    assert channels["broadcast"]["identifier"] == "@ravi_loots"
    for channel in channels.values():
        assert channel["platform"] == "telegram"
        assert channel["status"] == "ready", "channels must post without another step"

    summary = client.get("/easy-setup").get_data(as_text=True)
    assert "is ready" in summary
    assert "@ravi_loots" in summary


def test_easy_setup_updates_an_existing_creator_instead_of_duplicating(hub):
    client, session = hub
    session.unlock()

    _setup(client, session.token)
    response = _setup(
        client, session.token,
        amazon_tag="ravi-new-21", main_channel="@ravi_loots_2",
    )
    assert response.status_code == 302

    profiles = [p for p in db.list_influencers() if p["name"] == "Ravi Loots"]
    assert len(profiles) == 1, "a second copy of the creator must not appear"
    assert profiles[0]["amazon_tag"] == "ravi-new-21"
    assert len(db.list_channels(profiles[0]["id"])) == 3  # approval + both mains


def test_easy_setup_validates_channels(hub):
    client, session = hub
    session.unlock()

    no_channels = _setup(client, session.token, approval_channel="", main_channel="")
    assert no_channels.status_code == 200
    assert db.list_influencers() == []

    bad_channel = _setup(client, session.token, main_channel="ab")
    assert bad_channel.status_code == 200
    assert db.list_influencers() == []

    same_channel = _setup(client, session.token, approval_channel="@same", main_channel="@same")
    assert same_channel.status_code == 200
    assert db.list_influencers() == []


def test_easy_setup_normalises_channel_links(hub):
    client, session = hub
    session.unlock()

    _setup(
        client, session.token,
        approval_channel="https://t.me/ravi_amazon",
        main_channel="t.me/ravi_loots",
    )
    influencer = db.list_influencers()[0]
    identifiers = {channel["identifier"] for channel in db.list_channels(influencer["id"])}
    assert identifiers == {"@ravi_amazon", "@ravi_loots"}


def test_easy_setup_keeps_only_the_switches_that_are_on(hub):
    client, session = hub
    session.unlock()

    _setup(
        client, session.token,
        allow_amazon="1", allow_earnkaro="0", allow_hypd="0",
    )
    influencer = db.list_influencers()[0]
    assert (influencer["allow_amazon"], influencer["allow_earnkaro"], influencer["allow_hypd"]) == (1, 0, 0)
    for channel in db.list_channels(influencer["id"]):
        assert (channel["allow_amazon"], channel["allow_earnkaro"], channel["allow_hypd"]) == (1, 0, 0)


def test_easy_setup_strict_only_amazon_disables_the_other_networks(hub):
    client, session = hub
    session.unlock()

    _setup(client, session.token, only_amazon="1")
    influencer = db.list_influencers()[0]
    assert influencer["only_amazon"] == 1
    assert influencer["allow_amazon"] == 1


def test_easy_setup_is_password_protected_like_every_other_setup_change(hub):
    client, session = hub
    locked = _setup(client, session.token)
    assert locked.status_code == 302
    assert locked.headers["Location"].endswith("reauth=1")
    assert db.list_influencers() == []


# --------------------------------------------------------------------------
# Routing: what each kind of link becomes
# --------------------------------------------------------------------------
def test_routing_preview_amazon_uses_only_the_creator_tag(hub):
    client, session = hub
    preview = client.get(
        "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=1&allow_earnkaro=1&allow_hypd=1"
    ).get_json()["preview"]

    amazon_row = next(row for row in preview["rows"] if row["kind"] == "amazon")
    assert amazon_row["state"] == "ok"
    assert amazon_row["result"] == "https://www.amazon.in/dp/B0D9P2M1PB?th=1&tag=ravi-21"
    assert "ravi-21" in amazon_row["result"]


def test_routing_preview_sends_other_merchants_to_earnkaro(hub):
    client, session = hub
    url = "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=1&allow_earnkaro=1&allow_hypd=1"
    merchant_rows = [
        row for row in client.get(url).get_json()["preview"]["rows"]
        if row["kind"] == "merchant"
    ]
    assert len(merchant_rows) == 2
    # No EarnKaro key configured yet -> warn, not silently "converted".
    assert all(row["state"] == "warn" for row in merchant_rows)
    assert all("EarnKaro" in row["note"] for row in merchant_rows)

    monkeypatched = config.EARNKARO_API_KEY
    config.EARNKARO_API_KEY = "ek-test-key"
    try:
        ready_rows = [
            row for row in client.get(url).get_json()["preview"]["rows"]
            if row["kind"] == "merchant"
        ]
        assert all(row["state"] == "ok" for row in ready_rows)
    finally:
        config.EARNKARO_API_KEY = monkeypatched


def test_routing_preview_sends_meesho_to_hypd(hub):
    client, session = hub
    base = "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=1&allow_earnkaro=1"

    on = client.get(f"{base}&allow_hypd=1").get_json()["preview"]
    meesho = next(row for row in on["rows"] if row["kind"] == "meesho")
    assert meesho["state"] == "warn"  # raw Meesho cannot be minted into an afflink
    assert "HYPD" in meesho["note"]
    afflink = next(row for row in on["rows"] if row["kind"] == "hypd")
    assert afflink["state"] == "ok"
    assert afflink["result"] == "https://hypd.store/93944/afflink/SAMPLETOKEN"

    off = client.get(f"{base}&allow_hypd=0").get_json()["preview"]
    assert next(row for row in off["rows"] if row["kind"] == "meesho")["state"] == "off"
    assert next(row for row in off["rows"] if row["kind"] == "hypd")["state"] == "off"


def test_routing_preview_uses_the_creator_hypd_store(hub):
    client, session = hub
    preview = client.get(
        "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=1&allow_earnkaro=1"
        "&allow_hypd=1&hypd_store_id=55555"
    ).get_json()["preview"]
    afflink = next(row for row in preview["rows"] if row["kind"] == "hypd")
    assert afflink["result"] == "https://hypd.store/55555/afflink/SAMPLETOKEN"
    assert "55555" in preview["summary"]


def test_routing_preview_shows_off_states_and_informational_links(hub):
    client, session = hub
    preview = client.get(
        "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=0&allow_earnkaro=0&allow_hypd=0"
    ).get_json()["preview"]

    states = {row["kind"]: row["state"] for row in preview["rows"]}
    assert states["amazon"] == "off"
    assert states["merchant"] == "off"
    assert states["meesho"] == "off"
    assert states["hypd"] == "off"
    assert states["other"] == "ok", "informational links are never dropped"
    assert "Amazon off" in preview["summary"]


def test_routing_preview_strict_mode_is_amazon_only(hub):
    client, session = hub
    preview = client.get(
        "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=0&allow_earnkaro=1"
        "&allow_hypd=1&only_amazon=1"
    ).get_json()["preview"]
    assert preview["strict"] is True
    states = {row["kind"]: row["state"] for row in preview["rows"]}
    assert states["amazon"] == "ok"
    assert states["merchant"] == "off"
    assert states["meesho"] == "off"


def test_routing_preview_needs_no_password_but_does_need_a_session(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "preview-auth.sqlite3")
    db.init()

    client = app.test_client()
    assert client.get("/api/routing-preview").status_code == 401

    token = _csrf(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": PASSWORD, "_csrf_token": token}
    ).status_code == 302
    assert client.get("/api/routing-preview").get_json()["ok"] is True


def test_influencer_page_shows_the_routing_table(hub):
    client, session = hub
    session.unlock()
    _setup(client, session.token)
    influencer = db.list_influencers()[0]

    page = client.get(f"/influencer/{influencer['id']}").get_data(as_text=True)
    assert "What gets posted, link by link" in page
    assert "Main / broadcast channels" in page
    assert "Approval channels" in page
    assert "tag=ravi-21" in page


# --------------------------------------------------------------------------
# Deals keep flowing from the sources
# --------------------------------------------------------------------------
def test_easy_setup_offers_sources_when_none_are_configured(hub):
    client, session = hub
    page = client.get("/easy-setup").get_data(as_text=True)
    assert "No deal sources yet" in page
    assert "seed_default_sources" in page or "recommended sources" in page


def test_easy_setup_reports_how_many_sources_will_feed_the_creator(hub):
    client, session = hub
    db.add_source("Loot Source One", "@loot_source_one")
    db.add_source("Loot Source Two", "@loot_source_two")
    session.unlock()

    _setup(client, session.token)
    page = client.get("/easy-setup").get_data(as_text=True)
    assert "2 active source(s)" in page


# --------------------------------------------------------------------------
# End to end: Easy Setup -> a real deal from a source -> what each channel posts
# --------------------------------------------------------------------------
def test_easy_setup_then_a_source_deal_posts_on_each_route(hub, monkeypatch):
    """Set up in one screen, then confirm the pipeline posts the right links."""
    import asyncio
    from unittest.mock import AsyncMock

    from influencer_hub import pipeline

    client, session = hub
    session.unlock()
    _setup(client, session.token)

    influencer = db.list_influencers()[0]
    channels = {channel["role"]: channel for channel in db.list_channels(influencer["id"])}

    flipkart = "https://www.flipkart.com/product/p/itmEASY123"
    converted = "https://ekaro.in/ravi-loot"
    captured: dict[int, list[str]] = {}

    async def fake_dispatch(_influencer, channel, text):
        captured.setdefault(int(channel["id"]), []).append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={flipkart: converted}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    deal = (
        "🔥 Portronics Mini Fan at Rs.799\n"
        "https://www.amazon.in/dp/B0EASYSETUP?tag=source-21\n"
        f"Flipkart: {flipkart}\n"
        "Meesho: https://www.meesho.com/sample/p/itmeasy\n"
        "HYPD: https://hypd.store/11111/afflink/easytoken"
    )
    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer["id"]]))

    # Main channel: every switched-on network, each on its own route.
    assert result[influencer["id"]][channels["broadcast"]["id"]] == "posted"
    main_post = captured[channels["broadcast"]["id"]][0]
    assert "tag=ravi-21" in main_post and "source-21" not in main_post
    assert converted in main_post and flipkart not in main_post
    assert "https://hypd.store/93944/afflink/easytoken" in main_post
    assert "https://www.meesho.com/sample/p/itmeasy" in main_post

    # Approval channel: Amazon only, native, with the disclosure.
    assert result[influencer["id"]][channels["approval"]["id"]] == "posted"
    approval_post = captured[channels["approval"]["id"]][0]
    assert "tag=ravi-21" in approval_post
    assert "flipkart" not in approval_post.lower()
    assert "meesho" not in approval_post.lower()
    assert "#ad" in approval_post


def test_easy_setup_with_only_amazon_posts_amazon_and_nothing_else(hub, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from influencer_hub import pipeline

    client, session = hub
    session.unlock()
    _setup(
        client, session.token,
        allow_amazon="1", allow_earnkaro="0", allow_hypd="0", only_amazon="1",
    )

    influencer = db.list_influencers()[0]
    channel = next(c for c in db.list_channels(influencer["id"]) if c["role"] == "broadcast")
    captured: list[str] = []

    async def fake_dispatch(_influencer, _channel, text):
        captured.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    deal = (
        "Amazon https://www.amazon.in/dp/B0ONLYAMZ?tag=source-21\n"
        "Flipkart https://www.flipkart.com/product/p/itmONLY\n"
        "Meesho https://www.meesho.com/only/p/itm1"
    )
    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer["id"]]))
    assert result[influencer["id"]][channel["id"]] == "posted"
    post = captured[0]
    assert "tag=ravi-21" in post
    assert "flipkart" not in post.lower()
    assert "meesho" not in post.lower()
