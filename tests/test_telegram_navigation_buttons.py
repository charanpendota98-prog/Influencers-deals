import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from influencer_hub import db, pipeline

def test_telegram_custom_navigation_button():
    db.init()
    # 1. Influencer with custom navigation button enabled
    inf_id = db.add_influencer("Button Promo Influencer", "btninf-21")
    db.update_influencer(inf_id,
                         custom_button_enabled=True,
                         custom_button_text="⚡ Join Our Main Loot Channel",
                         custom_button_url="https://t.me/our_main_channel")

    ch_tg = db.add_channel(inf_id, "telegram", "@partner_deals", role="broadcast", status="ready")
    ch_wa = db.add_channel(inf_id, "whatsapp", "12036300000000@g.us", role="broadcast", status="ready")

    tg_calls = []
    async def mock_tg_post(identifier, text, media_path=None, button_text=None, button_url=None):
        tg_calls.append({"identifier": identifier, "text": text, "button_text": button_text, "button_url": button_url})

    wa_calls = []
    async def mock_wa_send(key, identifier, text):
        wa_calls.append({"identifier": identifier, "text": text})

    async def _test():
        with patch("influencer_hub.telegram_ops.post_to_channel", side_effect=mock_tg_post), \
             patch("influencer_hub.whatsapp_client.send_text", side_effect=mock_wa_send):

            deal_text = "🔥 Men Sneakers Flat 70% Off ₹499 https://www.amazon.in/dp/B00112233"
            await pipeline.render_and_dispatch(deal_text, influencer_ids=[inf_id])

            # Verify Telegram inline navigation button was passed
            assert len(tg_calls) == 1
            assert tg_calls[0]["button_text"] == "⚡ Join Our Main Loot Channel"
            assert tg_calls[0]["button_url"] == "https://t.me/our_main_channel"

            # Verify WhatsApp text footer navigation link was cleanly appended
            assert len(wa_calls) == 1
            assert "https://t.me/our_main_channel" in wa_calls[0]["text"]
            assert "👉 ⚡ Join Our Main Loot Channel" in wa_calls[0]["text"]

    asyncio.run(_test())
    db.delete_influencer(inf_id)

test_telegram_custom_navigation_button()
