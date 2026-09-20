import asyncio
from unittest.mock import patch, MagicMock
import pytest
from influencer_hub import bitly_client, link_router as lr, config


def test_clean_source_post_removes_promos_and_keeps_title():
    raw_deal = (
        "🔥 Boat Rockerz 450 Bluetooth On Ear Headphones with Mic\n"
        "⚡ Deal Price: ₹1,299 (MRP ₹3,990)\n"
        "Link: https://www.amazon.in/dp/B07PR1CL3S?tag=old-21\n"
        "\n"
        "👉 Join @PowerLootDeals for more loots\n"
        "https://t.me/PowerLootDeals\n"
        "Join fast! Share with your friends"
    )

    cleaned = lr.clean_source_post(raw_deal)

    # Product name & price must be strictly preserved
    assert "Boat Rockerz 450" in cleaned
    assert "₹1,299" in cleaned
    assert "https://www.amazon.in/dp/B07PR1CL3S?tag=old-21" in cleaned

    # Telegram source branding and watermarks must be cleanly removed
    assert "@PowerLootDeals" not in cleaned
    assert "t.me/PowerLootDeals" not in cleaned
    assert "Join fast" not in cleaned


def test_clean_source_post_inline_watermark_removal():
    raw_deal = (
        "Samsung Galaxy S23 5G - Join @SecretLoots\n"
        "Price: ₹49,999\n"
        "Buy here: https://www.amazon.in/dp/B0BT9CXXXX\n"
        "Powered by Admin • Loot alert by @AdminX"
    )
    cleaned = lr.clean_source_post(raw_deal)
    assert "Samsung Galaxy S23 5G" in cleaned
    assert "₹49,999" in cleaned
    assert "@SecretLoots" not in cleaned
    assert "@AdminX" not in cleaned


def test_bitly_shortener_caching_and_api():
    class FakeResp:
        def __init__(self, status, data):
            self.status = status
            self._data = data

        async def json(self):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    bitly_client.BITLY_CACHE.clear()

    long_url = "https://www.flipkart.com/very/long/product/url/path/with/tracking?id=12345"

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_post.return_value = FakeResp(200, {"link": "https://bit.ly/3xyzABC"})
        res = asyncio.run(bitly_client.shorten_urls([long_url], token="fake-token"))
        assert res[long_url] == "https://bit.ly/3xyzABC"
        assert mock_post.call_count == 1

        # Second call should use in-memory cache without hitting post
        res2 = asyncio.run(bitly_client.shorten_urls([long_url], token="fake-token"))
        assert res2[long_url] == "https://bit.ly/3xyzABC"
        assert mock_post.call_count == 1


def test_multi_link_bitly_rendering():
    text = (
        "🔥 Mega Shoe Sale!\n"
        "Puma Shoes: https://www.amazon.in/dp/B081234567\n"
        "Nike Shoes: https://www.flipkart.com/p/itm123456"
    )
    shortened_map = {
        "https://www.amazon.in/dp/B081234567?tag=influencer-21": "https://bit.ly/puma-short",
        "https://ekaro.in/ekaro123": "https://bit.ly/nike-short",
    }
    ek_map = {
        "https://www.flipkart.com/p/itm123456": "https://ekaro.in/ekaro123"
    }

    # Render for regular broadcast channel with Bitly shortening enabled
    rendered_broadcast = lr.render_for_influencer(
        text, "influencer-21", earnkaro_links=ek_map, shortened_links=shortened_map, role="broadcast"
    )
    assert "https://bit.ly/puma-short" in rendered_broadcast
    assert "https://bit.ly/nike-short" in rendered_broadcast
    assert "Puma Shoes:" in rendered_broadcast
    assert "Nike Shoes:" in rendered_broadcast

    # Render for approval channel MUST NEVER use bitly short links (Amazon associate compliance)
    rendered_approval = lr.render_for_influencer(
        text, "influencer-21", earnkaro_links=ek_map, shortened_links=shortened_map, role="approval"
    )
    assert "https://bit.ly" not in rendered_approval
    assert "https://www.amazon.in/dp/B081234567?tag=influencer-21" in rendered_approval
    assert "#ad (paid link)" in rendered_approval


def test_strict_dedup_across_varied_sources():
    # Two different sources sharing the same product with varied channel watermarks and links
    source_1 = (
        "🔥 LOOT DEAL 🔥\n"
        "Sony Bravia 55 inch TV at ₹54,990\n"
        "https://www.amazon.in/dp/B09XYZ9999?tag=source1-21\n"
        "Join @SourceOneLoots"
    )
    source_2 = (
        "⚡ Huge Price Drop: Sony Bravia 55 inch 4K TV ₹54,990\n"
        "Grab here: https://www.amazon.in/gp/product/B09XYZ9999?ref=xyz\n"
        "Join @SuperDealsHub"
    )

    sig1 = lr.deal_signature(source_1)
    sig2 = lr.deal_signature(source_2)

    # Signatures must be strictly identical so no channel receives duplicates!
    assert sig1 == sig2
