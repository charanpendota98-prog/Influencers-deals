import asyncio
import time
from unittest.mock import patch
import pytest

from influencer_hub import link_router as lr, pipeline


def test_category_classification_and_filtering():
    fashion_deal = "⚡ Levi's Men's Slim Fit Jeans at ₹999! Grab your favorite shirt and pants."
    elec_deal = "🔥 Apple iPhone 15 / Sony Headphones Wireless Earbuds at ₹49,999!"
    home_deal = "🏠 Prestige Non-Stick Cookware Pan & Bedsheet set at ₹599"
    daily_deal = "🧴 Dettol Soap & Face Wash Cream lotion at ₹149"
    other_deal = "🎟️ Free movie ticket voucher contest"

    # Category classification
    assert "clothing" in lr.classify_deal_category(fashion_deal)
    assert "electronics" in lr.classify_deal_category(elec_deal)
    assert "home" in lr.classify_deal_category(home_deal)
    assert "daily" in lr.classify_deal_category(daily_deal)
    assert "other" in lr.classify_deal_category(other_deal)

    # Filter matching
    assert lr.matches_category_filter(fashion_deal, "clothing") is True
    assert lr.matches_category_filter(fashion_deal, "electronics") is False
    assert lr.matches_category_filter(elec_deal, "electronics, home") is True
    assert lr.matches_category_filter(home_deal, "clothing, daily") is False
    assert lr.matches_category_filter(fashion_deal, "all") is True
    assert lr.matches_category_filter(fashion_deal, "") is True


def test_time_schedule_window_filtering():
    # Example windows: '06:00-09:00,18:00-23:00'
    sched = "06:00-09:00,18:00-23:00"

    # Morning active
    assert lr.is_time_in_schedule(sched, current_time="07:30") is True
    # Afternoon outside window -> Skip!
    assert lr.is_time_in_schedule(sched, current_time="14:00") is False
    # Evening active
    assert lr.is_time_in_schedule(sched, current_time="20:15") is True
    # Midnight outside window -> Skip!
    assert lr.is_time_in_schedule(sched, current_time="02:00") is False

    # Default / empty is 24/7
    assert lr.is_time_in_schedule("", current_time="02:00") is True
    assert lr.is_time_in_schedule("all", current_time="02:00") is True


def test_whatsapp_pacing_human_gap():
    """Verify that apply_whatsapp_safety_pacing enforces 45-65s gap between consecutive posts."""
    pipeline.WA_PACING_STATE.clear()
    session = "test-session-pacing"

    sleep_durations = []

    async def fake_sleep(duration):
        sleep_durations.append(duration)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        # 1st post: initial post
        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))
        assert len(sleep_durations) == 0  # No wait on first post

        # 2nd post immediately after: should sleep between 45 and 65 seconds
        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))
        assert len(sleep_durations) == 1
        assert 45.0 <= sleep_durations[0] <= 65.0


def test_whatsapp_pacing_hourly_cooldown():
    """Verify that after 1 hour of posting, an extended 120-150s rest is enforced."""
    pipeline.WA_PACING_STATE.clear()
    session = "test-session-hourly"

    # Seed state as if 1 hour has elapsed
    now = time.time()
    pipeline.WA_PACING_STATE[session] = {
        "last_post_ts": now - 100,  # last post was 100s ago
        "hour_start_ts": now - 3700, # hour started >3600s ago
        "posts_this_hour": 15,
    }

    sleep_durations = []

    async def fake_sleep(duration):
        sleep_durations.append(duration)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))
        # Should have taken the hourly break (120 to 150s)
        assert len(sleep_durations) >= 1
        assert 120.0 <= sleep_durations[0] <= 150.0
