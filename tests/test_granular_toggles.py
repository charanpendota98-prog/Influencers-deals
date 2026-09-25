import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from influencer_hub import db, pipeline

def test_granular_amazon_earnkaro_switches():
    db.init()
    # 1. Influencer with ONLY AMAZON = True (EarnKaro OFF)
    inf1 = db.add_influencer("Only Amz User", "myamz-21", only_amazon=True)
    ch1 = db.add_channel(inf1, "telegram", "@onlyamz_chan", role="broadcast", status="ready")

    # 2. Influencer with ALLOW AMAZON = False (Amazon OFF, only EarnKaro)
    inf2 = db.add_influencer("Only FK User", "nofk-21", allow_amazon=False)
    ch2 = db.add_channel(inf2, "telegram", "@onlyfk_chan", role="broadcast", status="ready")

    # 3. Influencer with NEW Amazon Tag update
    inf3 = db.add_influencer("Tag Update User", "oldtag-21")
    ch3 = db.add_channel(inf3, "telegram", "@tagupdate_chan", role="broadcast", status="ready")

    async def _run_tests():
        with patch("influencer_hub.pipeline.dispatch_to_channel", new=AsyncMock(return_value="sent")):
            # Test Flipkart deal
            fk_deal = "Flipkart Men Shoes Rs 499 https://www.flipkart.com/p/itm12345"
            res_fk = await pipeline.render_and_dispatch(fk_deal, influencer_ids=[inf1, inf2])
            # inf1 has only_amazon=True -> should SKIP fk deal!
            assert res_fk[inf1][ch1] == "skipped"
            # inf2 has allow_amazon=False -> should SEND fk deal!
            assert res_fk[inf2][ch2] == "sent"

            # Test Amazon deal
            amz_deal = "Amazon Men Shoes Rs 499 https://www.amazon.in/dp/B08XYZ1234"
            res_amz = await pipeline.render_and_dispatch(amz_deal, influencer_ids=[inf1, inf2])
            # inf1 has only_amazon=True -> should SEND amz deal!
            assert res_amz[inf1][ch1] == "sent"
            # inf2 has allow_amazon=False -> should SKIP amz deal!
            assert res_amz[inf2][ch2] == "skipped"

            # Test Tag update for inf3
            db.update_influencer(inf3, amazon_tag="newtag-21")
            captured = []
            async def mock_disp(inf, ch, text):
                captured.append(text)
                return "sent"
            with patch("influencer_hub.pipeline.dispatch_to_channel", side_effect=mock_disp):
                await pipeline.render_and_dispatch(amz_deal, influencer_ids=[inf3])
                assert len(captured) == 1
                assert "newtag-21" in captured[0]
                assert "oldtag-21" not in captured[0]

    asyncio.run(_run_tests())
    db.delete_influencer(inf1)
    db.delete_influencer(inf2)
    db.delete_influencer(inf3)
