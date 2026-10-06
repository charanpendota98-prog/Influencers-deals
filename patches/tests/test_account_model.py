"""The account model, pinned.

One rule, three networks:

    Amazon        -> the CREATOR's own Associate tag (their id signs the post)
    EarnKaro      -> OUR central Affiliaters/EarnKaro account (vault credentials)
    Meesho / HYPD -> OUR central HYPD store id

`influencer_hub/accounts.py` is the single source of truth, and the pipeline,
the dashboard previews and Money Radar must all resolve the same pair of
accounts — otherwise the audit would report leaks that do not exist, or miss
the ones that do.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from dashboard.app import app
from influencer_hub import accounts, config, db, money_radar, pipeline

FLIPKART = "https://www.flipkart.com/product/p/itm123456"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setitem(app.config, "TESTING", True)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "accounts.sqlite3")
    db.init()
    return app.test_client()


# --------------------------------------------------------------------------
# Amazon: the creator's own id
# --------------------------------------------------------------------------
def test_creator_tag_wins_and_the_channel_override_still_count(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "amazon.sqlite3")
    db.init()
    creator = {"amazon_tag": "ravi-21"}
    channel = {"amazon_override_tag": ""}
    assert accounts.creator_amazon_tag(creator, channel) == "ravi-21"
    assert accounts.amazon_tag_source(creator, channel) == "creator"
    assert accounts.amazon_tag_is_creators_own(creator, channel) is True

    channel = {"amazon_override_tag": "ravi-shopsy-21"}
    assert accounts.creator_amazon_tag(creator, channel) == "ravi-shopsy-21"
    assert accounts.amazon_tag_source(creator, channel) == "channel"


def test_missing_own_tag_falls_back_but_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "amazon-fallback.sqlite3")
    db.init()
    assert accounts.creator_amazon_tag({"amazon_tag": ""}) == config.AMAZON_ASSOCIATE_TAG
    assert accounts.amazon_tag_source({"amazon_tag": ""}) == "fallback"
    assert accounts.amazon_tag_is_creators_own({"amazon_tag": ""}) is False


def test_creators_missing_own_tag_lists_the_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "amazon-checklist.sqlite3")
    db.init()
    db.add_influencer("Has Tag", "hastag-21")
    db.add_influencer("Needs Tag", "")
    listed = {row["name"] for row in accounts.creators_missing_own_tag()}
    assert "Needs Tag" in listed
    assert "Has Tag" not in listed


# --------------------------------------------------------------------------
# HYPD: always our store while central accounts are on
# --------------------------------------------------------------------------
def test_hypd_store_is_ours_even_when_a_creator_brings_their_own(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hypd-central.sqlite3")
    db.init()
    db.set_global_setting("hypd_store_id", "93944")
    influencer = {"hypd_store_id": "55555"}
    channel = {"hypd_store_id": "77777"}

    assert accounts.central_network_accounts_enabled() is True
    assert accounts.hypd_store_for(influencer, channel) == "93944"
    assert accounts.hypd_store_for(influencer, {}) == "93944"
    assert accounts.hypd_store_for({}, channel) == "93944"


def test_the_setting_off_restores_the_legacy_priority(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hypd-legacy.sqlite3")
    db.init()
    db.set_global_setting("hypd_store_id", "93944")
    db.set_global_setting("central_network_accounts", "0")
    influencer = {"hypd_store_id": "55555"}
    channel = {"hypd_store_id": "77777"}

    assert accounts.central_network_accounts_enabled() is False
    assert accounts.hypd_store_for(influencer, channel) == "77777"
    assert accounts.hypd_store_for(influencer, {}) == "55555"
    assert accounts.hypd_store_for({}, {}) == "93944"


# --------------------------------------------------------------------------
# EarnKaro: our vault credentials, never a creator row
# --------------------------------------------------------------------------
def test_earnkaro_accounts_come_from_the_vault(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ek.sqlite3")
    db.init()
    db.set_global_setting("earnkaro_api_key", "vault-token")
    db.set_global_setting("earnkaro_publisher_id", "5478322")
    assert accounts.central_earnkaro_api_key() == "vault-token"
    assert accounts.central_earnkaro_publisher_id() == "5478322"


def test_earnkaro_conversion_is_creator_agnostic(monkeypatch, tmp_path):
    """Two creators, one conversion call, OUR publisher id for both."""
    from influencer_hub import earnkaro

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ek-shared.sqlite3")
    db.init()
    db.set_global_setting("earnkaro_api_key", "vault-token")
    db.set_global_setting("earnkaro_publisher_id", "5478322")
    earnkaro.CACHE.clear()

    class Session:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"json": json, "headers": headers})

            class Response:
                status = 200

                async def __aenter__(self_inner):
                    return self_inner

                async def __aexit__(self_inner, *args):
                    return False

                async def text(self_inner):
                    return (
                        '{"success":1,"data":"https://www.flipkart.com/p/itm1'
                        '?id=5478322"}'
                    )

            return Response()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("influencer_hub.earnkaro.aiohttp.ClientSession", lambda: Session())
        # Both creators post through the same converter client.
        first = asyncio.run(earnkaro.convert_links({FLIPKART}))
        second = asyncio.run(earnkaro.convert_links({FLIPKART}))
    assert first == second
    assert list(first.values())[0].endswith("id=5478322")


# --------------------------------------------------------------------------
# End to end: two creators, one deal
# --------------------------------------------------------------------------
def test_one_deal_posts_each_creators_own_tag_and_our_store(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "two-creators.sqlite3")
    db.init()
    db.set_global_setting("hypd_store_id", "93944")

    ravi = db.add_influencer("Ravi", "ravi-21", hypd_store_id="55555")
    sampath = db.add_influencer("Sampath", "sampath-21", hypd_store_id="66666")
    for influencer_id in (ravi, sampath):
        db.add_channel(
            influencer_id, "telegram", f"@creator_{influencer_id}", status="ready",
            allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
        )

    captured: list[str] = []

    async def fake_dispatch(_influencer, _channel, text):
        captured.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for",
                        AsyncMock(return_value={FLIPKART: "https://ekaro.in/ours"}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    deal = (
        "Amazon https://www.amazon.in/dp/B0ABCDEFGH?tag=source-21\n"
        "HYPD https://hypd.store/12345/afflink/product-token\n"
        f"Flipkart {FLIPKART}"
    )
    asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[ravi, sampath]))

    assert len(captured) == 2
    ravi_post = next(text for text in captured if "ravi-21" in text)
    sampath_post = next(text for text in captured if "sampath-21" in text)

    for post in (ravi_post, sampath_post):
        # Amazon -> the creator's own tag
        assert "tag=source-21" not in post
        # HYPD -> OUR store, never the creator's own
        assert "https://hypd.store/93944/afflink/product-token" in post
        assert "hypd.store/55555" not in post
        assert "hypd.store/66666" not in post
        # EarnKaro -> our converted link
        assert "https://ekaro.in/ours" in post
    assert ravi_post != sampath_post


# --------------------------------------------------------------------------
# Money Radar audits with the same accounts
# --------------------------------------------------------------------------
def test_money_radar_audits_the_posted_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "radar-accounts.sqlite3")
    db.init()
    db.set_global_setting("hypd_store_id", "93944")
    creator = {"id": 1, "amazon_tag": "ravi-21", "hypd_store_id": "55555"}
    channel = {"id": 1, "amazon_override_tag": "", "hypd_store_id": "66666"}
    assert money_radar._creator_routing(creator, channel) == ("ravi-21", "93944")


# --------------------------------------------------------------------------
# The dashboard says which account earns
# --------------------------------------------------------------------------
def test_routing_preview_shows_the_account_model(hub):
    db.set_global_setting("earnkaro_api_key", "vault-token")
    preview = hub.get(
        "/api/routing-preview?amazon_tag=ravi-21&allow_amazon=1&allow_earnkaro=1&allow_hypd=1"
    ).get_json()["preview"]

    by_network = {row["network"]: row for row in preview["model"]}
    assert by_network["Amazon"]["earns"] == "creator"
    assert by_network["Amazon"]["account"] == "ravi-21"
    assert by_network["EarnKaro"]["earns"] == "central"
    assert by_network["Meesho (HYPD)"]["earns"] == "central"
    assert by_network["Meesho (HYPD)"]["account"] == config.HYPD_STORE_ID

    amazon_row = next(row for row in preview["rows"] if row["kind"] == "amazon")
    assert "OWN tag" in amazon_row["note"]
    hypd_row = next(row for row in preview["rows"] if row["kind"] == "hypd")
    assert "OUR HYPD store" in hypd_row["note"]
    merchant_row = next(row for row in preview["rows"] if row["kind"] == "merchant")
    assert "OUR EarnKaro account" in merchant_row["note"]


def test_model_rows_copy_is_explicit_about_who_earns():
    rows = accounts.model_rows(amazon_tag="ravi-21")
    assert rows[0]["detail"].startswith("Every Amazon link is posted with this creator")
    assert "OUR EarnKaro account" in rows[1]["detail"]
    assert "OUR store" in rows[2]["detail"]
