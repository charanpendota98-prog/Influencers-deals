import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from influencer_hub import db, pipeline, link_router

def test_hourly_loot_analysis_and_highlight():
    db.init()
    inf_id = db.add_influencer("Hourly Loot Tester", "hourloot-21")
    ch_id = db.add_channel(inf_id, "telegram", "@hourloot_test", role="broadcast", status="ready")

    # Record 2 posted deals in the last hour:
    # 1. Normal deal (small discount)
    deal1 = "Normal deal: Shirt at ₹999 https://www.amazon.in/dp/B00111111"
    sig1 = link_router.deal_signature(deal1)
    db.record_post(inf_id, ch_id, sig1, status="posted", deal_text=deal1)

    # 2. Mega Loot deal (huge 85% discount, ₹199 price, loot keyword)
    deal2 = "🔥 CRAZY LOOT ERROR! boAt Earbuds Flat 85% Off! Deal Price: ₹199 https://www.amazon.in/dp/B00222222"
    sig2 = link_router.deal_signature(deal2)
    db.record_post(inf_id, ch_id, sig2, status="posted", deal_text=deal2)

    # Check scores
    score1 = link_router.calculate_deal_loot_score(deal1)
    score2 = link_router.calculate_deal_loot_score(deal2)
    assert score2 > score1

    sent_messages = []
    async def mock_dispatch(inf, ch, text):
        sent_messages.append(text)
        return "posted"

    async def _test():
        with patch("influencer_hub.pipeline.dispatch_to_channel", side_effect=mock_dispatch):
            res = await pipeline.run_hourly_loot_highlight(influencer_ids=[inf_id])
            assert res[inf_id][ch_id] == "posted"
            assert len(sent_messages) == 1
            # Verify the banner format and that deal2 was selected as Loot of the Hour
            assert "𝗟𝗢𝗢𝗧 𝗢𝗙 𝗧𝗛𝗘 𝗛𝗢𝗨𝗥" in sent_messages[0]
            assert "boAt Earbuds" in sent_messages[0]
            assert "₹199" in sent_messages[0]

    asyncio.run(_test())
    db.delete_influencer(inf_id)

test_hourly_loot_analysis_and_highlight()
