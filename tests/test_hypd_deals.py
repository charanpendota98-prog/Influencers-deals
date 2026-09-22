import pytest
from influencer_hub import db, link_router, pipeline


def test_hypd_classification_and_routing():
    # 1. Classification
    u1 = "https://hypd.store/93944/afflink/daol5bac45l0tc0oo5rg"
    u2 = "https://www.hypd.store/12345/afflink/abcde12345"
    assert link_router.classify_url(u1) == "hypd"
    assert link_router.classify_url(u2) == "hypd"

    # 2. Store ID conversion
    converted = link_router.convert_hypd_store_link(u2, "93944")
    assert converted == "https://hypd.store/93944/afflink/abcde12345"

    converted_custom = link_router.convert_hypd_store_link(u1, "77777")
    assert converted_custom == "https://hypd.store/77777/afflink/daol5bac45l0tc0oo5rg"


def test_render_hypd_deal():
    deal_text = (
        "🔥 Meesho Saree at ₹299 only!\n"
        "Buy here: https://hypd.store/12345/afflink/saree001\n"
        "Loot fast!"
    )
    rendered = link_router.render_for_influencer(
        deal_text,
        amazon_tag="influencer-21",
        role="broadcast",
        hypd_store_id="93944"
    )
    assert "https://hypd.store/93944/afflink/saree001" in rendered
    assert "https://hypd.store/12345" not in rendered


def test_hypd_pipeline_toggle(monkeypatch):
    import asyncio
    # Setup test influencer with HYPD ON vs HYPD OFF
    db.init()
    db.migrate()

    iid = db.add_influencer(
        name="HypdTester",
        amazon_tag="hypdtest-21",
        allow_amazon=True,
        allow_earnkaro=False,
        allow_hypd=True,
        hypd_store_id="93944"
    )
    cid = db.add_channel(
        iid, "telegram", "@hypd_channel_test",
        role="broadcast", status="ready",
        allow_amazon=True, allow_earnkaro=False, allow_hypd=True, hypd_store_id="93944"
    )

    dispatched_msgs = []
    async def fake_dispatch(inf, ch, text):
        dispatched_msgs.append((ch["id"], text))
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    async def _run():
        deal = (
            "🔥 Meesho Kurti at ₹149!\n"
            "Link: https://hypd.store/99999/afflink/kurti999"
        )

        # When allow_hypd is True, deal should be posted
        res = await pipeline.render_and_dispatch(deal, influencer_ids=[iid])
        assert res[iid][cid] == "posted"
        assert len(dispatched_msgs) == 1
        assert "https://hypd.store/93944/afflink/kurti999" in dispatched_msgs[0][1]

        # When allow_hypd is turned OFF, it should be skipped
        db.update_influencer(iid, allow_hypd=False)
        db.update_channel_details(cid, allow_hypd=False)

        deal2 = (
            "🔥 Meesho Jeans at ₹249!\n"
            "Link: https://hypd.store/88888/afflink/jeans888"
        )
        res2 = await pipeline.render_and_dispatch(deal2, influencer_ids=[iid])
        assert res2[iid][cid] == "skipped"

    asyncio.run(_run())
