from __future__ import annotations

import asyncio
from unittest.mock import patch

from dashboard.app import app
from influencer_hub import channel_navigation, config, db, pipeline


def _creator(name: str):
    return db.add_influencer(name, f"{name.lower().replace(' ', '')}-21")


def test_smart_navigation_lists_only_other_ready_channels_of_that_creator():
    creator = _creator("Creator One")
    current = db.add_channel(creator, "telegram", "@creator_one_main",
                             status="ready", role="broadcast", title="Creator One Main")
    db.add_channel(creator, "telegram", "@creator_one_vip",
                   status="ready", role="broadcast", title="Creator One VIP")
    db.add_channel(creator, "telegram", "-100999888777",
                   invite_link="https://t.me/+PrivateInvite", status="active",
                   role="broadcast", title="Creator One Private")
    db.add_channel(creator, "telegram", "@creator_one_pending",
                   status="pending", role="broadcast", title="Not Ready")
    db.add_channel(creator, "whatsapp_group", "120363000000000@g.us",
                   status="ready", role="whatsapp", title="WhatsApp Group")

    another_creator = _creator("Creator Two")
    db.add_channel(another_creator, "telegram", "@creator_two_main",
                   status="ready", role="broadcast", title="Creator Two Main")

    buttons = channel_navigation.creator_channel_buttons(creator, current)
    assert buttons == [
        {"text": "Join Creator One VIP", "url": "https://t.me/creator_one_vip"},
        {"text": "Join Creator One Private", "url": "https://t.me/+PrivateInvite"},
    ]
    assert all("creator_two" not in button["url"] for button in buttons)
    assert all("Not Ready" not in button["text"] for button in buttons)


def test_smart_navigation_rejects_non_telegram_and_unsafe_invites():
    rows = [
        {"id": 1, "platform": "telegram", "status": "ready", "identifier": "@safe", "title": "Safe"},
        {"id": 2, "platform": "telegram", "status": "ready", "identifier": "@bad", "invite_link": "javascript:alert(1)"},
        {"id": 3, "platform": "telegram", "status": "ready", "identifier": "-100123"},
        {"id": 4, "platform": "whatsapp_group", "status": "ready", "identifier": "@wa"},
        {"id": 5, "influencer_id": 10, "platform": "telegram", "status": "ready", "identifier": "@other_creator"},
        {"id": 6, "platform": "telegram", "status": "ready", "identifier": "@badport", "invite_link": "https://t.me:444/bad"},
    ]
    assert channel_navigation.creator_channel_buttons(9, channels=rows) == [
        {"text": "Join Safe", "url": "https://t.me/safe"}
    ]


def test_route_simulator_reports_counts_and_exact_per_channel_buttons():
    rows = [
        {"id": 1, "influencer_id": 9, "platform": "telegram", "status": "ready", "identifier": "-10001", "title": "Main"},
        {"id": 2, "influencer_id": 9, "platform": "telegram", "status": "ready", "identifier": "@alerts", "title": "Alerts"},
        {"id": 3, "influencer_id": 9, "platform": "telegram", "status": "paused", "identifier": "@paused", "title": "Paused"},
        {"id": 4, "influencer_id": 10, "platform": "telegram", "status": "ready", "identifier": "@other_creator", "title": "Other creator"},
    ]
    preview = channel_navigation.navigation_matrix(9, rows)
    assert preview["ready_channels"] == 2
    assert preview["linkable_channels"] == 1
    assert preview["offline_channels"] == 1
    assert preview["unlinked_channels"] == 1
    assert [row["channel"] for row in preview["rows"]] == ["Main", "Alerts"]
    assert preview["rows"][0]["buttons"] == [
        {"text": "Join Alerts", "url": "https://t.me/alerts"}
    ]
    assert preview["rows"][1]["buttons"] == []


def test_dispatch_adds_creator_specific_channel_buttons_to_telegram_post():
    creator = _creator("Button Creator")
    current = db.add_channel(creator, "telegram", "@button_creator_main",
                             status="ready", role="broadcast", title="Main")
    db.add_channel(creator, "telegram", "@button_creator_alerts",
                   status="ready", role="broadcast", title="Alerts")
    db.set_global_setting("nav_button_enabled", "0")
    influencer = db.get_influencer(creator)
    influencer.update({
        "custom_button_enabled": 1,
        "custom_button_url": channel_navigation.SMART_CHANNEL_NAV,
    })
    channel = next(c for c in db.list_channels(creator) if c["id"] == current)
    # An old URL left on a disabled channel must not mask creator smart mode.
    channel.update({"custom_button_enabled": 0, "custom_button_url": "https://t.me/stale"})
    posted = []

    async def fake_post(identifier, text, **kwargs):
        posted.append((identifier, text, kwargs))

    with patch("influencer_hub.telegram_ops.post_to_channel", new=fake_post):
        result = asyncio.run(pipeline._dispatch_to_channel(influencer, channel, "Deal text"))

    assert result == "posted"
    assert posted == [(
        "@button_creator_main",
        "Deal text",
        {"buttons": [{"text": "Join Alerts", "url": "https://t.me/button_creator_alerts"}]},
    )]


def test_smart_navigation_setting_is_saved_per_influencer(monkeypatch, tmp_path):
    # A standalone Flask test avoids relying on a global dashboard fixture.
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "smart-navigation.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    creator = _creator("Navigation Admin")
    client = app.test_client()
    response = client.post(
        f"/influencer/{creator}/update-profile",
        data={"name": "Navigation Admin", "smart_channel_nav": "1"},
    )
    assert response.status_code == 302
    saved = db.get_influencer(creator)
    assert saved["custom_button_enabled"] == 1
    assert saved["custom_button_url"] == channel_navigation.SMART_CHANNEL_NAV
    page = client.get(f"/influencer/{creator}")
    assert page.status_code == 200
    assert b"Smart cross-channel buttons (this creator only)" in page.data
    assert b"Live route simulator" in page.data


def test_smart_navigation_footer_is_clear_and_line_separated():
    assert channel_navigation.whatsapp_navigation_footer([
        {"text": "Join Main", "url": "https://t.me/main"},
        {"text": "Join Alerts", "url": "https://t.me/alerts"},
    ]) == "👉 Join Main: https://t.me/main\n👉 Join Alerts: https://t.me/alerts"


def test_custom_post_buttons_support_two_safe_ctas_and_reject_unsafe_urls():
    buttons = channel_navigation.custom_post_buttons({
        "custom_button_text": "Join Telegram",
        "custom_button_url": "@creator_deals",
        "custom_button2_text": "Shop offer",
        "custom_button2_url": "https://shop.example/deal",
    })
    assert buttons == [
        {"text": "Join Telegram", "url": "https://t.me/creator_deals"},
        {"text": "Shop offer", "url": "https://shop.example/deal"},
    ]
    assert channel_navigation.custom_post_buttons({
        "custom_button_url": "javascript:alert(1)",
        "custom_button2_url": "http://insecure.example/",
    }) == []


def test_dispatch_posts_two_creator_specific_cta_buttons_to_telegram():
    creator = _creator("Two Button Creator")
    channel_id = db.add_channel(
        creator, "telegram", "@two_button_main", status="ready", role="broadcast",
        custom_button_enabled=True,
        custom_button_text="Join deals", custom_button_url="@two_button_vip",
        custom_button2_text="Shop now", custom_button2_url="https://shop.example/creator",
    )
    db.set_global_setting("nav_button_enabled", "0")
    influencer = db.get_influencer(creator)
    channel = next(c for c in db.list_channels(creator) if c["id"] == channel_id)
    posted = []

    async def fake_post(identifier, text, **kwargs):
        posted.append((identifier, text, kwargs))

    with patch("influencer_hub.telegram_ops.post_to_channel", new=fake_post):
        result = asyncio.run(pipeline._dispatch_to_channel(influencer, channel, "Deal text"))

    assert result == "posted"
    assert posted == [(
        "@two_button_main",
        "Deal text",
        {"buttons": [
            {"text": "Join deals", "url": "https://t.me/two_button_vip"},
            {"text": "Shop now", "url": "https://shop.example/creator"},
        ]},
    )]


def test_profile_form_saves_two_buttons_and_renders_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "two-buttons.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    creator = _creator("Two Button Admin")
    client = app.test_client()
    response = client.post(
        f"/influencer/{creator}/update-profile",
        data={
            "name": "Two Button Admin",
            "custom_button_enabled": "1",
            "custom_button_text": "Join channel",
            "custom_button_url": "@two_button_admin",
            "custom_button2_text": "Shop deals",
            "custom_button2_url": "https://shop.example/deals",
        },
    )
    assert response.status_code == 302
    saved = db.get_influencer(creator)
    assert saved["custom_button_text"] == "Join channel"
    assert saved["custom_button_url"] == "@two_button_admin"
    assert saved["custom_button2_text"] == "Shop deals"
    assert saved["custom_button2_url"] == "https://shop.example/deals"
    page = client.get(f"/influencer/{creator}")
    assert page.status_code == 200
    assert b"custom_button2_url" in page.data


def test_quick_add_form_persists_both_buttons(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "quick-add-buttons.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    response = app.test_client().post("/quick-add", data={
        "name": "Quick Button Creator",
        "tag": "quickbutton-21",
        "custom_button_enabled": "1",
        "custom_button_text": "Join channel",
        "custom_button_url": "@quick_button",
        "custom_button2_text": "Shop",
        "custom_button2_url": "https://shop.example/quick",
    })
    assert response.status_code == 302
    saved = next(i for i in db.list_influencers() if i["name"] == "Quick Button Creator")
    assert saved["custom_button_enabled"] == 1
    assert saved["custom_button_url"] == "@quick_button"
    assert saved["custom_button2_text"] == "Shop"
    assert saved["custom_button2_url"] == "https://shop.example/quick"


def test_whatsapp_custom_ctas_are_rendered_as_a_two_link_footer(monkeypatch):
    creator = _creator("WhatsApp CTA Creator")
    influencer = db.get_influencer(creator)
    influencer.update({
        "custom_button_enabled": 1,
        "custom_button_text": "Join group",
        "custom_button_url": "https://t.me/whatsapp_cta",
        "custom_button2_text": "Shop offer",
        "custom_button2_url": "https://shop.example/wa",
    })
    channel = {
        "id": 919,
        "influencer_id": creator,
        "platform": "whatsapp_group",
        "identifier": "120363000000001@g.us",
        "custom_button_enabled": 0,
    }
    sent = []

    async def no_wait(_key):
        return None

    async def fake_send(_key, _jid, text):
        sent.append(text)
        return {"ok": True}

    monkeypatch.setattr(pipeline, "apply_whatsapp_safety_pacing", no_wait)
    monkeypatch.setattr("influencer_hub.whatsapp_client.send_text", fake_send)
    result = asyncio.run(pipeline._dispatch_to_channel(influencer, channel, "Deal text"))
    assert result == "posted"
    assert sent == [
        "Deal text\n\n👉 Join group: https://t.me/whatsapp_cta\n👉 Shop offer: https://shop.example/wa"
    ]
