import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from influencer_hub import db, pipeline


def test_universal_and_per_influencer_navigation_button():
    db.init()
    db.migrate()

    # 1. Influencer WITHOUT channel-specific button -> should fallback to Universal Global Button!
    inf1 = db.add_influencer("Global Fallback Inf", "tag1-21")
    ch1 = db.add_channel(inf1, "telegram", "@target_ch1", role="broadcast", status="ready")

    # 2. Influencer WITH custom button overriding the global button
    inf2 = db.add_influencer("Custom Inf", "tag2-21")
    ch2 = db.add_channel(
        inf2, "telegram", "@target_ch2", role="broadcast", status="ready",
        custom_button_enabled=True,
        custom_button_text="Special VIP Hub 💎",
        custom_button_url="https://t.me/special_vip"
    )

    # Set Universal Setting (as seen in photo)
    db.set_global_setting("nav_button_enabled", "1")
    db.set_global_setting("nav_button_text", "Join Shpsy Loots ❤️")
    db.set_global_setting("nav_button_url", "https://t.me/shpsy_loots_official")

    posted_calls = []

    async def fake_post(identifier, text, button_text="", button_url=""):
        posted_calls.append({
            "ident": identifier,
            "text": text,
            "btn_text": button_text,
            "btn_url": button_url
        })
        return True

    async def _run():
        with patch("influencer_hub.telegram_ops.post_to_channel", new=fake_post):
            deal = "Amazon Headphones Rs 299 https://www.amazon.in/dp/B08XYZ1234"
            await pipeline.render_and_dispatch(deal, influencer_ids=[inf1, inf2])

        # Verify ch1 got the Universal Button: "Join Shpsy Loots ❤️"
        c1_call = next(c for c in posted_calls if c["ident"] == "@target_ch1")
        assert c1_call["btn_text"] == "Join Shpsy Loots ❤️"
        assert c1_call["btn_url"] == "https://t.me/shpsy_loots_official"

        # Verify ch2 got its specific override button: "Special VIP Hub 💎"
        c2_call = next(c for c in posted_calls if c["ident"] == "@target_ch2")
        assert c2_call["btn_text"] == "Special VIP Hub 💎"
        assert c2_call["btn_url"] == "https://t.me/special_vip"

    asyncio.run(_run())
