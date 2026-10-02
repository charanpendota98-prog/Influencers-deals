import asyncio
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch

import pytest

from influencer_hub import config, db, link_router as lr, pipeline


@pytest.fixture
def pacing_database(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "whatsapp-pacing.sqlite3")
    db.init()


def _reserve_whatsapp_slot_in_process(args):
    database_path, session_key, now = args
    config.DB_PATH = database_path
    return db.reserve_whatsapp_send(
        session_key, gap_seconds=50.0, rest_seconds=130.0, now=now
    )


def test_category_classification_and_filtering():
    fashion_deal = "⚡ Levi's Men's Slim Fit Jeans at ₹999! Grab your favorite shirt and pants."
    elec_deal = "🔥 Apple iPhone 15 / Sony Headphones Wireless Earbuds at ₹49,999!"
    home_deal = "🏠 Prestige Non-Stick Cookware Pan & Bedsheet set at ₹599"
    daily_deal = "🧴 Dettol Soap & Face Wash Cream lotion at ₹149"
    other_deal = "🎟️ General quiz contest information"

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


def test_whatsapp_pacing_human_gap(pacing_database):
    """The pipeline reserves a persisted 45–65 second gap between sends."""
    session = "test-session-pacing"
    sleep_durations = []

    async def fake_sleep(duration):
        sleep_durations.append(duration)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))
        assert sleep_durations == []  # The first reservation can send now.

        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))
        assert len(sleep_durations) == 1
        assert 45.0 <= sleep_durations[0] <= 65.0


def test_whatsapp_pacing_hourly_cooldown(pacing_database, monkeypatch):
    """The hourly rest is reserved in SQLite and survives process restarts."""
    session = "test-session-hourly"
    first_reserved_at = time.time() - 3700.0
    assert db.reserve_whatsapp_send(
        session, gap_seconds=50.0, rest_seconds=130.0, now=first_reserved_at
    ) == 0.0

    # Make the runtime-selected break deterministic while retaining the real
    # persisted timestamp transition in the pacing implementation.
    monkeypatch.setattr(pipeline.random, "uniform", lambda low, _high: low)
    sleep_durations = []

    async def fake_sleep(duration):
        sleep_durations.append(duration)

    # Re-running initialization models a worker/dashboard restart: it must not
    # clear the persisted reservation or its hourly boundary.
    db.init()
    with patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(pipeline.apply_whatsapp_safety_pacing(session))

    assert sleep_durations == pytest.approx([120.0], abs=0.1)


def test_whatsapp_pacing_honors_hourly_boundary_for_queued_slots(pacing_database):
    """A queued slot cannot cross the hourly boundary without the long rest."""
    session = "test-session-queued-hourly"
    assert db.reserve_whatsapp_send(
        session, gap_seconds=50.0, rest_seconds=130.0, now=1000.0
    ) == 0.0
    # Reserve just before the one-hour boundary; this slot is still eligible.
    assert db.reserve_whatsapp_send(
        session, gap_seconds=50.0, rest_seconds=130.0, now=4590.0
    ) == 0.0
    # Its next slot would be scheduled beyond the boundary, so reserve the
    # hourly rest after that already-reserved gap instead of skipping the rest.
    assert db.reserve_whatsapp_send(
        session, gap_seconds=50.0, rest_seconds=130.0, now=4591.0
    ) == pytest.approx(179.0)


def test_whatsapp_pacing_reservations_are_atomic_across_processes(pacing_database):
    """Parallel worker/dashboard processes receive distinct, ordered slots."""
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process SQLite reservation test requires fork support")

    session = "test-session-concurrent"
    args = [(str(config.DB_PATH), session, 1000.0)] * 8
    with ProcessPoolExecutor(
        max_workers=8, mp_context=multiprocessing.get_context("fork")
    ) as executor:
        delays = list(executor.map(_reserve_whatsapp_slot_in_process, args))

    assert sorted(delays) == pytest.approx(
        [0.0, 50.0, 100.0, 150.0, 200.0, 250.0, 300.0, 350.0]
    )
