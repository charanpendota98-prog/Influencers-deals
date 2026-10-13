import asyncio
from unittest.mock import patch
import pytest

from influencer_hub import bitly_client, link_router as lr, pipeline, db


def test_across_midnight_schedule_window():
    """Verify 06:00 to 02:00 next day timing schedule.
    Active all day and night until 2:00 AM; holds/pauses strictly between 2:00 AM and 6:00 AM.
    """
    sched = "06:00-02:00"

    # 1. Daytime 10:00 AM -> Active
    assert lr.is_time_in_schedule(sched, current_time="10:00") is True
    # 2. Late Night 11:30 PM (23:30) -> Active
    assert lr.is_time_in_schedule(sched, current_time="23:30") is True
    # 3. Post-Midnight 1:15 AM (01:15) -> Active
    assert lr.is_time_in_schedule(sched, current_time="01:15") is True
    # 4. Sleep/Break window 3:30 AM (03:30) -> PAUSED/HOLD!
    assert lr.is_time_in_schedule(sched, current_time="03:30") is False
    # 5. Sleep/Break window 5:00 AM (05:00) -> PAUSED/HOLD!
    assert lr.is_time_in_schedule(sched, current_time="05:00") is False


def test_top_8_category_verticals():
    deals = {
        "clothing": "⚡ Levi's Denim Jeans & Shirt at ₹999",
        "electronics": "🔥 Samsung Galaxy 5G Smartphone & Laptop at ₹34,999",
        "home": "🏠 Prestige Pressure Cooker & Kitchen Non-Stick Pan at ₹1,199",
        "daily": "🧴 Dettol Antiseptic Liquid & Surf Excel Detergent Powder at ₹249",
        "beauty": "💄 Maybelline Matte Lipstick & Skincare Moisturizer Serum at ₹399",
        "appliances": "❄️ LG 260L Double Door Refrigerator Fridge & Washing Machine at ₹18,990",
        "baby": "👶 Pampers Diapers Pants & Feeding Bottle Toys for Baby at ₹499",
        "sports": "🏋️ MuscleBlaze Whey Protein & Fitness Gym Dumbbell at ₹1,899",
    }

    for cat, deal in deals.items():
        matched = lr.classify_deal_category(deal)
        assert cat in matched
        assert lr.matches_category_filter(deal, cat) is True
        # Cross category check
        assert lr.matches_category_filter(deal, "clothing" if cat != "clothing" else "appliances") is False


def test_bitly_pool_multi_api_keys_failover():
    """Verify that multiple comma-separated keys are attempted and cached properly."""
    bitly_client.BITLY_CACHE.clear()
    long_url = "https://www.flipkart.com/p/itm12345678"

    class FakeResp:
        def __init__(self, status, link=""):
            self.status = status
            self.link = link

        async def json(self):
            return {"link": self.link}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    # First key fails with 429 rate-limit; second key succeeds!
    attempts = []

    def fake_post(url, json=None, headers=None, timeout=None):
        auth = headers.get("Authorization", "")
        attempts.append(auth)
        if "Bearer key1" in auth:
            return FakeResp(429)  # Rate limit
        return FakeResp(200, "https://bit.ly/success-key2")

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        res = asyncio.run(bitly_client.shorten_urls([long_url], token="key1, key2"))
        assert res[long_url] == "https://bit.ly/success-key2"
        # Verify both keys were tried in sequence
        assert any("key1" in a for a in attempts)
        assert any("key2" in a for a in attempts)
