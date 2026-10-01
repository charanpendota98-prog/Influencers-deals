"""Dashboard controls must persist the same affiliate routing the pipeline uses."""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, earnkaro, pipeline


@pytest.fixture
def dashboard_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "dashboard-affiliates.sqlite3")
    db.init()
    db.migrate()
    app.config.update(TESTING=True)
    return app.test_client()


def test_new_profiles_and_channels_inherit_global_hypd_store(dashboard_client):
    db.set_global_setting("hypd_store_id", "52525")

    influencer_id = db.add_influencer("Global Store Creator", "globalstore-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@global_store_creator", status="ready"
    )

    influencer = db.get_influencer(influencer_id)
    channel = db.list_channels(influencer_id)[0]
    assert influencer["hypd_store_id"] == "52525"
    assert channel["id"] == channel_id
    assert channel["hypd_store_id"] == "52525"


def test_quick_add_saves_creator_amazon_tag_and_selected_networks(dashboard_client):
    response = dashboard_client.post(
        "/quick-add",
        data={
            "name": "Creator One",
            "tag": "creatorone-21",
            "phone_number": "919876543210",
            "broadcast_tg": "@creatorone_deals",
            "allow_amazon": "1",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
            "only_amazon": "1",
            "hypd_store_id": "77777",
        },
    )
    assert response.status_code == 302

    influencer = db.list_influencers()[0]
    channel = db.list_channels(influencer["id"])[0]
    assert influencer["amazon_tag"] == "creatorone-21"
    assert influencer["only_amazon"] == 1
    assert influencer["allow_amazon"] == 1
    assert influencer["allow_earnkaro"] == 1
    assert influencer["allow_hypd"] == 1
    assert influencer["hypd_store_id"] == "77777"
    assert channel["allow_earnkaro"] == 1
    assert channel["allow_hypd"] == 1
    assert channel["hypd_store_id"] == "77777"


def test_onboarding_persists_creator_tag_networks_and_hypd_store(dashboard_client):
    response = dashboard_client.post(
        "/onboard",
        data={
            "name": "Onboarded Creator",
            "tag": "onboarded-21",
            "allow_amazon": "1",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
            "only_amazon": "1",
            "hypd_store_id": "65432",
            # Leave Telegram and WhatsApp setup unchecked so the test is offline.
        },
    )
    assert response.status_code == 200

    influencer = db.list_influencers()[0]
    assert influencer["amazon_tag"] == "onboarded-21"
    assert influencer["allow_amazon"] == 1
    assert influencer["allow_earnkaro"] == 1
    assert influencer["allow_hypd"] == 1
    assert influencer["only_amazon"] == 1
    assert influencer["hypd_store_id"] == "65432"
    assert db.list_channels(influencer["id"]) == []


def test_bulk_csv_import_preserves_tags_networks_and_global_hypd_default(dashboard_client):
    db.set_global_setting("hypd_store_id", "74123")
    csv_text = (
        "name,tag,broadcast_tg,allow_amazon,allow_earnkaro,allow_hypd,only_amazon,hypd_store_id,strip_amazon\n"
        "Bulk Creator,bulkcreator-21,@bulk_creator,0,1,0,0,,0\n"
        "Only Amazon,,@bulk_amazon,0,1,1,1,,0\n"
    )

    response = dashboard_client.post("/bulk-import", data={"csv_text": csv_text})

    assert response.status_code == 302
    influencers = {inf["name"]: inf for inf in db.list_influencers()}
    bulk = influencers["Bulk Creator"]
    only_amazon = influencers["Only Amazon"]
    assert bulk["amazon_tag"] == "bulkcreator-21"
    assert bulk["allow_amazon"] == 0
    assert bulk["allow_earnkaro"] == 1
    assert bulk["allow_hypd"] == 0
    assert bulk["hypd_store_id"] == "74123"
    assert only_amazon["amazon_tag"] == config.AMAZON_ASSOCIATE_TAG
    assert only_amazon["only_amazon"] == 1
    # The explicit override affects routing only; imported switch selections persist.
    assert only_amazon["allow_amazon"] == 0
    assert only_amazon["allow_earnkaro"] == 1
    assert only_amazon["allow_hypd"] == 1
    assert only_amazon["hypd_store_id"] == "74123"
    bulk_channel = db.list_channels(bulk["id"])[0]
    assert bulk_channel["allow_amazon"] == 0
    assert bulk_channel["allow_earnkaro"] == 1
    assert bulk_channel["allow_hypd"] == 0
    assert bulk_channel["hypd_store_id"] == "74123"
    only_amazon_channel = db.list_channels(only_amazon["id"])[0]
    assert only_amazon_channel["only_amazon"] == 1
    assert only_amazon_channel["allow_amazon"] == 0
    assert only_amazon_channel["allow_earnkaro"] == 1
    assert only_amazon_channel["allow_hypd"] == 1


def test_unchecked_quick_add_networks_are_saved_as_off(dashboard_client):
    response = dashboard_client.post(
        "/quick-add",
        data={"name": "Creator Two", "tag": "creatortwo-21"},
    )
    assert response.status_code == 302

    influencer = db.list_influencers()[0]
    assert influencer["allow_amazon"] == 0
    assert influencer["allow_earnkaro"] == 0
    assert influencer["allow_hypd"] == 0


def test_channel_hidden_checkbox_pairs_keep_checked_values(dashboard_client):
    influencer_id = db.add_influencer("Channel Flags", "flags-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@channel_flags", status="ready",
        allow_amazon=False, allow_earnkaro=False, allow_hypd=True,
    )

    response = dashboard_client.post(
        f"/channel/{channel_id}/update",
        data={
            "inf_id": str(influencer_id),
            "identifier": "@channel_flags",
            "role": "broadcast",
            "status": "ready",
            "only_amazon": "1",
            "allow_amazon": ["0", "1"],
            "allow_earnkaro": ["0", "1"],
            "allow_hypd": ["0", "1"],
            "custom_button_enabled": ["0", "1"],
            "custom_button_text": "Join our channel",
            "custom_button_url": "https://t.me/our_channel",
        },
    )
    assert response.status_code == 302

    channel = db.list_channels(influencer_id)[0]
    assert channel["only_amazon"] == 1
    assert channel["allow_amazon"] == 1
    assert channel["allow_earnkaro"] == 1
    assert channel["allow_hypd"] == 1
    assert channel["custom_button_enabled"] == 1


def test_profile_tag_and_network_updates_are_persisted(dashboard_client):
    influencer_id = db.add_influencer(
        "Profile Update", "oldcreator-21", allow_earnkaro=False, allow_hypd=False
    )
    inherited_channel_id = db.add_channel(
        influencer_id, "telegram", "@profile_default", status="ready"
    )
    override_channel_id = db.add_channel(
        influencer_id, "telegram", "@profile_override", status="ready",
        hypd_store_id="99999",
    )
    response = dashboard_client.post(
        f"/influencer/{influencer_id}/update-profile",
        data={
            "name": "Profile Update",
            "amazon_tag": "newcreator-21",
            "only_amazon": "1",
            "allow_amazon": "1",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
            "hypd_store_id": "88888",
        },
    )
    assert response.status_code == 302

    influencer = db.get_influencer(influencer_id)
    assert influencer["amazon_tag"] == "newcreator-21"
    assert influencer["only_amazon"] == 1
    assert influencer["allow_amazon"] == 1
    assert influencer["allow_earnkaro"] == 1
    assert influencer["allow_hypd"] == 1
    assert influencer["hypd_store_id"] == "88888"
    channels = {channel["id"]: channel for channel in db.list_channels(influencer_id)}
    assert channels[inherited_channel_id]["hypd_store_id"] == "88888"
    assert channels[override_channel_id]["hypd_store_id"] == "99999"


def test_manual_channel_saves_selected_networks_and_hypd_id(dashboard_client):
    influencer_id = db.add_influencer(
        "Manual Channel Creator", "manual-21", hypd_store_id="43210"
    )
    response = dashboard_client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={
            "platform": "telegram",
            "identifier": "@manual_channel",
            "role": "broadcast",
            "allow_amazon": "1",
            "allow_earnkaro": "0",
            "allow_hypd": "1",
            "hypd_store_id": "43210",
        },
    )
    assert response.status_code == 302

    channel = db.list_channels(influencer_id)[0]
    assert channel["allow_amazon"] == 1
    assert channel["allow_earnkaro"] == 0
    assert channel["allow_hypd"] == 1
    assert channel["hypd_store_id"] == "43210"


def test_pipeline_uses_creator_amazon_tag_earnkaro_and_profile_hypd(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "pipeline-affiliates.sqlite3")
    db.init()
    influencer_id = db.add_influencer(
        "Routing Creator", "routingcreator-21", allow_amazon=False,
        allow_earnkaro=True, allow_hypd=True, hypd_store_id="77777",
    )
    # Amazon OFF lets the selected EarnKaro/HYPD routes run. The channel inherits
    # the profile's HYPD store unless it has an explicit override.
    channel_id = db.add_channel(
        influencer_id, "telegram", "@routing_creator", status="ready",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True,
    )
    channel = db.list_channels(influencer_id)[0]
    assert channel["hypd_store_id"] == "77777"

    flipkart = "https://www.flipkart.com/product/p/itm123456"
    converted = "https://ekaro.in/creator-one"
    captured = []

    async def fake_dispatch(_influencer, _channel, text):
        captured.append(text)
        return "posted"

    ek_convert = AsyncMock(return_value={flipkart: converted})
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", ek_convert)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    deal = (
        "Amazon item https://www.amazon.in/dp/B0ABCDEFGH?tag=source-21\n"
        "HYPD item https://hypd.store/12345/afflink/product-token\n"
        f"Flipkart item {flipkart}"
    )

    result = asyncio.run(
        pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id])
    )
    assert result[influencer_id][channel_id] == "posted"
    ek_convert.assert_awaited_once()
    assert "amazon.in" not in captured[0]
    assert "https://hypd.store/77777/afflink/product-token" in captured[0]
    assert converted in captured[0]
    assert flipkart not in captured[0]


def test_raw_meesho_uses_hypd_toggle_and_never_earnkaro(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "meesho-hypd-only.sqlite3")
    db.init()
    hypd_id = db.add_influencer(
        "Meesho HYPD", "meeshypd-21", allow_amazon=False,
        allow_earnkaro=True, allow_hypd=True,
    )
    no_hypd_id = db.add_influencer(
        "Meesho HYPD Off", "meeshypdoff-21", allow_amazon=False,
        allow_earnkaro=True, allow_hypd=False,
    )
    hypd_channel = db.add_channel(
        hypd_id, "telegram", "@meesho_hypd", status="ready",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True,
    )
    no_hypd_channel = db.add_channel(
        no_hypd_id, "telegram", "@meesho_hypd_off", status="ready",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=False,
    )
    raw_meesho = "https://www.meesho.com/s/p/7amuq5"
    sent = []

    async def fake_dispatch(_influencer, _channel, text):
        sent.append(text)
        return "posted"

    earnkaro_converter = AsyncMock(return_value={})
    monkeypatch.setattr(earnkaro, "convert_links", earnkaro_converter)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    result = asyncio.run(
        pipeline.render_and_dispatch(
            f"Meesho product {raw_meesho}",
            influencer_ids=[hypd_id, no_hypd_id],
        )
    )

    assert result[hypd_id][hypd_channel] == "posted"
    assert result[no_hypd_id][no_hypd_channel] == "skipped"
    assert raw_meesho in sent[0]
    earnkaro_converter.assert_not_awaited()


def test_amazon_earnkaro_and_hypd_switches_route_independently(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "combined-networks.sqlite3")
    db.init()
    influencer_id = db.add_influencer(
        "Multi-Network Creator", "profilecreator-21", allow_amazon=True,
        allow_earnkaro=True, allow_hypd=True,
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@multi_network_creator", status="ready",
        allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
        amazon_override_tag="channelcreator-21",
    )
    posted = []

    async def fake_dispatch(_influencer, _channel, text):
        posted.append(text)
        return "posted"

    flipkart = "https://www.flipkart.com/p/itm111111"
    converted = "https://ekaro.in/creator-link"
    earnkaro_map = AsyncMock(return_value={flipkart: converted})
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", earnkaro_map)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    mixed_deal = (
        "Amazon item https://www.amazon.in/dp/B0ABCDEFGH?tag=source-21\n"
        f"Flipkart item {flipkart}\n"
        "HYPD item https://hypd.store/12345/afflink/product-token"
    )
    result = asyncio.run(
        pipeline.render_and_dispatch(mixed_deal, influencer_ids=[influencer_id])
    )
    assert result[influencer_id][channel_id] == "posted"
    assert "tag=channelcreator-21" in posted[0]
    assert "source-21" not in posted[0]
    assert converted in posted[0]
    assert flipkart not in posted[0]
    assert "https://hypd.store/93944/afflink/product-token" in posted[0]
    earnkaro_map.assert_awaited_once()

    # A merchant-only deal can still route through the selected network while
    # Amazon is also enabled.
    second = asyncio.run(
        pipeline.render_and_dispatch(
            "Flipkart only https://www.flipkart.com/p/itm222222",
            influencer_ids=[influencer_id],
        )
    )
    assert second[influencer_id][channel_id] == "posted"
    assert "flipkart.com" in posted[1]
    db.delete_influencer(influencer_id)


def test_only_amazon_override_suppresses_other_selected_routes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "only-amazon-override.sqlite3")
    db.init()
    influencer_id = db.add_influencer(
        "Explicit Amazon Only", "overridecreator-21", only_amazon=True,
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True,
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@explicit_amazon_only", status="ready",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True,
    )
    posted = []

    async def fake_dispatch(_influencer, _channel, text):
        posted.append(text)
        return "posted"

    earnkaro_map = AsyncMock(return_value={})
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", earnkaro_map)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    deal = (
        "Amazon item https://www.amazon.in/dp/B0ABCDEFGH?tag=source-21\n"
        "Flipkart item https://www.flipkart.com/p/itm333333\n"
        "HYPD item https://hypd.store/12345/afflink/product-token"
    )

    result = asyncio.run(
        pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id])
    )

    assert result[influencer_id][channel_id] == "posted"
    assert "tag=overridecreator-21" in posted[0]
    assert "flipkart.com" not in posted[0]
    assert "hypd.store" not in posted[0]
    earnkaro_map.assert_not_awaited()
    db.delete_influencer(influencer_id)


def test_each_influencer_gets_its_own_amazon_tag_from_source_links(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "two-amazon-tags.sqlite3")
    db.init()
    first_id = db.add_influencer(
        "First Tag", "firstcreator-21", allow_earnkaro=False, allow_hypd=False
    )
    second_id = db.add_influencer(
        "Second Tag", "secondcreator-21", allow_earnkaro=False, allow_hypd=False
    )
    first_channel = db.add_channel(first_id, "telegram", "@first_tag", status="ready")
    second_channel = db.add_channel(second_id, "telegram", "@second_tag", status="ready")
    posted = {}

    async def fake_dispatch(influencer, _channel, text):
        posted[influencer["id"]] = text
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    source = "Amazon deal https://www.amazon.in/dp/B0ABCDEFGH?tag=source-21&ref=feed"

    result = asyncio.run(
        pipeline.render_and_dispatch(source, influencer_ids=[first_id, second_id])
    )

    assert result[first_id][first_channel] == "posted"
    assert result[second_id][second_channel] == "posted"
    assert "tag=firstcreator-21" in posted[first_id]
    assert "tag=secondcreator-21" in posted[second_id]
    assert "tag=source-21" not in posted[first_id]
    assert "tag=source-21" not in posted[second_id]


def test_earnkaro_is_not_called_when_network_is_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "earnkaro-off.sqlite3")
    db.init()
    influencer_id = db.add_influencer(
        "EarnKaro Off", "off-21", allow_earnkaro=False, allow_hypd=False
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@earnkaro_off", status="ready",
        allow_earnkaro=True, allow_hypd=True,
    )
    converter = AsyncMock(return_value={})
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", converter)
    monkeypatch.setattr(
        pipeline, "dispatch_to_channel", AsyncMock(return_value="posted")
    )

    result = asyncio.run(
        pipeline.render_and_dispatch(
            "Flipkart deal https://www.flipkart.com/item/p/itm123456",
            influencer_ids=[influencer_id],
        )
    )
    assert result[influencer_id][channel_id] == "skipped"
    converter.assert_not_awaited()


def test_dashboard_index_renders_profile_aware_dry_run_simulator(dashboard_client):
    db.add_influencer("Preview Profile", "previewprofile-21")

    response = dashboard_client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="sim-profile"' in body
    assert "Preview Profile" in body
    assert "No channel posts are sent" in body


def test_influencer_detail_renders_network_status_and_static_preview(
    dashboard_client, monkeypatch
):
    influencer_id = db.add_influencer(
        "Status Creator", "statuscreator-21", allow_amazon=True,
        allow_earnkaro=False, allow_hypd=True,
    )
    monkeypatch.setattr(
        dashboard_module.whatsapp_client, "session_status", lambda _key: None
    )
    monkeypatch.setattr(dashboard_module, "_run", lambda _coro: {"status": "offline"})

    response = dashboard_client.get(f"/influencer/{influencer_id}")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Affiliate routing for this profile" in body
    assert "statuscreator-21" in body
    assert "ON · Store" in body
    assert "Meesho only; Amazon and other merchants are not routed through HYPD" in body
    assert "HYPD only retags existing HYPD affiliate URLs" in body
    assert "Static rendering only" in body


def test_dashboard_preview_applies_selected_networks_without_posting(
    dashboard_client, monkeypatch
):
    flipkart = "https://www.flipkart.com/product/p/itm123456"
    converted = "https://ekaro.in/preview-link"
    converter = AsyncMock(return_value={flipkart: converted})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "preview-key")
    sample = (
        "Amazon https://www.amazon.in/dp/B0ABCDEFGH?tag=old-21\n"
        "HYPD https://hypd.store/12345/afflink/product-token\n"
        f"Flipkart {flipkart}"
    )

    response = dashboard_client.post(
        "/api/test-render-deal",
        data={
            "sample_text": sample,
            "amazon_tag": "previewcreator-21",
            "hypd_store_id": "24680",
            "allow_amazon": "0",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
        },
    )
    result = response.get_json()

    assert response.status_code == 200
    assert result["ok"] is True
    assert "amazon.in" not in result["rendered"]
    assert "https://hypd.store/24680/afflink/product-token" in result["rendered"]
    assert converted in result["rendered"]
    assert result["network_settings"] == {
        "amazon": False, "earnkaro": True, "hypd": True,
        "only_amazon": False,
    }
    converter.assert_awaited_once_with({flipkart})


def test_dashboard_preview_reserves_meesho_for_hypd(dashboard_client, monkeypatch):
    raw_meesho = "https://www.meesho.com/s/p/7amuq5"
    converter = AsyncMock(return_value={raw_meesho: "https://ekaro.in/should-not-appear"})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    response = dashboard_client.post(
        "/api/test-render-deal",
        data={
            "sample_text": f"Meesho product {raw_meesho}",
            "allow_amazon": "0",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
        },
    )
    result = response.get_json()

    assert response.status_code == 200
    assert raw_meesho in result["rendered"]
    assert "ekaro.in" not in result["rendered"]
    assert any("reserved for HYPD" in warning for warning in result["warnings"])
    converter.assert_not_awaited()


def test_dashboard_preview_amazon_routes_alongside_selected_networks(
    dashboard_client, monkeypatch
):
    flipkart = "https://www.flipkart.com/product/p/itm123456"
    converted = "https://ekaro.in/preview-link"
    converter = AsyncMock(return_value={flipkart: converted})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    monkeypatch.setattr(dashboard_module, "_run", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "preview-key")
    sample = (
        "Amazon https://www.amazon.in/dp/B0ABCDEFGH?tag=old-21\n"
        "HYPD https://hypd.store/12345/afflink/product-token\n"
        f"Flipkart {flipkart}"
    )

    response = dashboard_client.post(
        "/api/test-render-deal",
        data={
            "sample_text": sample,
            "amazon_tag": "previewcreator-21",
            "hypd_store_id": "24680",
            "allow_amazon": "1",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
        },
    )
    result = response.get_json()

    assert response.status_code == 200
    assert "tag=previewcreator-21" in result["rendered"]
    assert converted in result["rendered"]
    assert flipkart not in result["rendered"]
    assert "https://hypd.store/24680/afflink/product-token" in result["rendered"]
    assert result["network_settings"] == {
        "amazon": True, "earnkaro": True, "hypd": True, "only_amazon": False,
    }
    converter.assert_awaited_once_with({flipkart})


def test_dashboard_preview_only_amazon_override_is_exclusive(
    dashboard_client, monkeypatch
):
    flipkart = "https://www.flipkart.com/product/p/itm123456"
    converter = AsyncMock(return_value={flipkart: "https://ekaro.in/preview-link"})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    sample = (
        "Amazon https://www.amazon.in/dp/B0ABCDEFGH?tag=old-21\n"
        "HYPD https://hypd.store/12345/afflink/product-token\n"
        f"Flipkart {flipkart}"
    )

    response = dashboard_client.post(
        "/api/test-render-deal",
        data={
            "sample_text": sample,
            "amazon_tag": "previewcreator-21",
            "allow_amazon": "1",
            "allow_earnkaro": "1",
            "allow_hypd": "1",
            "only_amazon": "1",
        },
    )
    result = response.get_json()

    assert response.status_code == 200
    assert "tag=previewcreator-21" in result["rendered"]
    assert "flipkart.com" not in result["rendered"]
    assert "hypd.store" not in result["rendered"]
    assert result["network_settings"] == {
        "amazon": True, "earnkaro": False, "hypd": False, "only_amazon": True,
    }
    converter.assert_not_awaited()


def test_dashboard_preview_does_not_call_earnkaro_when_disabled(
    dashboard_client, monkeypatch
):
    converter = AsyncMock(return_value={})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    response = dashboard_client.post(
        "/api/test-render-deal",
        data={
            "sample_text": "Flipkart https://www.flipkart.com/product/p/itm123456",
            "allow_amazon": "1",
            "allow_earnkaro": "0",
            "allow_hypd": "1",
        },
    )
    result = response.get_json()

    assert response.status_code == 200
    assert result["rendered"] == ""
    assert result["network_settings"]["earnkaro"] is False
    converter.assert_not_awaited()
