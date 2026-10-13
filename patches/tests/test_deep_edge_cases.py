import pytest
from unittest.mock import patch, AsyncMock
from influencer_hub import db, link_router, pipeline, bitly_client

def test_indian_timing_edge_cases():
    # 1. 24/7 or empty or all
    assert link_router.is_time_in_schedule("", "03:00") is True
    assert link_router.is_time_in_schedule("all", "03:00") is True
    assert link_router.is_time_in_schedule("24/7", "03:00") is True

    # 2. Next-day window 06:00-02:00
    assert link_router.is_time_in_schedule("06:00-02:00", "06:00") is True
    assert link_router.is_time_in_schedule("06:00-02:00", "12:00") is True
    assert link_router.is_time_in_schedule("06:00-02:00", "23:59") is True
    assert link_router.is_time_in_schedule("06:00-02:00", "00:00") is True
    assert link_router.is_time_in_schedule("06:00-02:00", "01:59") is True
    assert link_router.is_time_in_schedule("06:00-02:00", "02:00") is True
    # Sleep window (02:01 - 05:59)
    assert link_router.is_time_in_schedule("06:00-02:00", "02:05") is False
    assert link_router.is_time_in_schedule("06:00-02:00", "04:00") is False
    assert link_router.is_time_in_schedule("06:00-02:00", "05:59") is False

    # 3. Multiple disjoint windows
    multi = "06:00-09:00, 18:00-23:00"
    assert link_router.is_time_in_schedule(multi, "07:30") is True
    assert link_router.is_time_in_schedule(multi, "12:00") is False
    assert link_router.is_time_in_schedule(multi, "20:00") is True
    assert link_router.is_time_in_schedule(multi, "01:00") is False

def test_categories_11_detection():
    samples = {
        "clothing": "⚡ Raymond Men Formal Shirt & Trousers at ₹799",
        "electronics": "🎧 boAt Airdopes 141 Bluetooth TWS Earbuds at ₹899",
        "home": "🍳 Hawkins Stainless Steel Pressure Cooker 3L Pan at ₹1,299",
        "daily": "🌾 Fortune Sunlite Refined Sunflower Oil 5L & Atta at ₹599",
        "beauty": "💄 Mamaearth Moisture Matte Lipstick Red at ₹299",
        "appliances": "❄️ Haier 190L Direct Cool Single Door Refrigerator at ₹12,990",
        "baby": "🍼 Philips Avent Feeding Bottle & Pampers Diaper at ₹450",
        "sports": "🏸 Li-Ning G-Force Badminton Racquet & Shuttle at ₹999",
        "recharge_freebies": "🎁 Jio Recharge 100% Cashback Voucher Coupon Code",
        "books_stationery": "📚 Rich Dad Poor Dad Paperback Book & Notebooks at ₹199",
        "automotive": "🏍️ Vega Cliff Full Face Helmet & Bike Mobile Holder at ₹849",
    }
    for cat, deal in samples.items():
        detected = link_router.classify_deal_category(deal)
        assert cat in detected, f"Expected {cat} in {detected} for deal '{deal}'"
        assert link_router.matches_category_filter(deal, cat) is True

def test_only_amazon_filter_strictness():
    # Influencer with only_amazon = True
    inf_id = db.add_influencer("Strict Amz Partner", "stramz-21", phone_number="919988776655",
                               only_amazon=True)
    ch_app = db.add_channel(inf_id, "telegram", "@stramz_app", role="approval", only_amazon=True, status="ready")
    ch_main = db.add_channel(inf_id, "telegram", "@stramz_main", role="broadcast", only_amazon=True, status="ready")

    import asyncio

    async def _test():
        with patch("influencer_hub.pipeline.dispatch_to_channel", new=AsyncMock(return_value="sent")):
            # 1. Non Amazon deal must be SKIPPED
            res_fk = await pipeline.render_and_dispatch("Flipkart Smart Watch ₹999 https://fkrt.it/watch", influencer_ids=[inf_id])
            assert res_fk[inf_id][ch_app] == "skipped"
            assert res_fk[inf_id][ch_main] == "skipped"

            # 2. Amazon deal must be SENT
            res_amz = await pipeline.render_and_dispatch("Amazon Smart Watch ₹999 https://www.amazon.in/dp/B00112233", influencer_ids=[inf_id])
            assert res_amz[inf_id][ch_app] == "sent"
            assert res_amz[inf_id][ch_main] == "sent"

    asyncio.run(_test())
    db.delete_influencer(inf_id)
