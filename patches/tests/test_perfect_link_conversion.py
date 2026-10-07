"""Every link shape a source deal can carry must convert to OUR affiliate link.

These tests pin the four defects the conversion audit found:

1. Opaque Amazon shorts were only recognised on ``amzn.to``/``amzn.in``. A deal
   carrying ``amzn.eu``, ``amzn.asia`` or ``a.co`` (or an upper-case host) was
   classified as an "informational link" and posted completely untagged.
2. A standalone short that could not be resolved was only a warning; the post
   still went out and the reviewer could not tell which link was at risk.
3. When EarnKaro returned nothing, a raw merchant URL was posted silently: the
   post earned exactly zero and nothing told the operator.
4. The approval renderer printed the same canonical Amazon link twice when a
   post carried both an opaque short and the long link for the same product.
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock

from influencer_hub import commission_guard, config, db, earnkaro, link_router, pipeline

TAG = "creator-21"
STORE = "93944"
PUB = "5478322"

SHORT_URLS = (
    "https://amzn.to/4dnF9lU",
    "https://AMZN.TO/4dnF9lU",
    "http://amzn.in/d/abc123",
    "https://amzn.eu/d/abc123",
    "https://amzn.asia/d/xyz789",
    "https://a.co/d/9zKq2",
)


# --------------------------------------------------------------------------
# 1. Opaque Amazon shorts are recognised on every Amazon shortener host
# --------------------------------------------------------------------------
def test_every_amazon_shortener_host_is_detected_and_classified_as_amazon():
    for url in SHORT_URLS:
        assert link_router.is_amazon_short_host(url), url
        assert link_router.classify_url(url) == "amazon", url


def test_short_hosts_are_not_confused_with_long_marketplace_links():
    assert not link_router.is_amazon_short_host(
        "https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21"
    )
    assert link_router.is_our_amazon_marketplace(
        "https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21"
    )
    # A foreign storefront can never pay our India Associate tag, so it stays
    # informational instead of being rewritten.
    assert link_router.classify_url(
        "https://www.amazon.co.uk/dp/B0D9P2M1PB?tag=someone-21"
    ) == "other"


def test_text_helpers_see_shorts_the_old_substring_check_missed():
    assert link_router.text_has_amazon_short("grab https://a.co/d/9zKq2 now")
    assert link_router.text_has_amazon_short("grab https://amzn.eu/d/9z now")
    assert not link_router.text_has_amazon_short(
        "deal https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21"
    )
    assert link_router.unresolved_amazon_shorts(
        "Deal https://amzn.eu/d/9z and https://www.amazon.in/dp/B0ABCDEFGH?tag=creator-21"
    ) == ["https://amzn.eu/d/9z"]


def test_heuristic_replaces_an_a_co_short_with_the_long_link_of_the_same_deal():
    text = (
        "🔥 Deal ₹499\n"
        "https://a.co/d/9zKq2\n"
        "https://www.amazon.in/dp/B08XYZ1234?th=1&tag=competitor-21"
    )
    out = link_router.expand_amazon_shorts_in_text(text, TAG)
    assert "a.co" not in out
    # The long link still carries the source tag at this stage; the render step
    # retags it and the pipeline dedups, so the post carries the canonical OUR
    # link once instead of twice.
    rendered = link_router.deduplicate_urls_in_text(
        link_router.render_for_influencer(out, TAG)
    )
    assert rendered.count("https://www.amazon.in/dp/B08XYZ1234?th=1&tag=creator-21") == 1
    assert "competitor-21" not in rendered


class _FakeAiohttp:
    """Stand in for ``aiohttp`` so the resolver runs without a network."""

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class _Response:
        def __init__(self, url):
            self.url = url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class ClientSession:
        def __init__(self, _final_url, *args, **kwargs):
            self._final_url = _final_url

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            return _FakeAiohttp._Response(self._final_url)


def _resolve(monkeypatch, final_url: str, text: str) -> str:
    import aiohttp

    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda *a, **kw: _FakeAiohttp.ClientSession(final_url)
    )
    return asyncio.run(link_router.expand_amazon_shorts_in_text_async(text, TAG))


def test_standalone_short_is_resolved_to_a_canonical_link_with_our_tag(monkeypatch):
    out = _resolve(
        monkeypatch,
        "https://www.amazon.in/Soundbar/dp/B08XYZ1234?th=1&tag=mama086-21",
        "🔥 Loot ₹499 https://amzn.eu/d/9zKq2",
    )
    assert out == "🔥 Loot ₹499 https://www.amazon.in/dp/B08XYZ1234?th=1&tag=creator-21"
    assert link_router.unresolved_amazon_shorts(out) == []


def test_resolved_short_keeps_the_marketplace_it_pointed_at(monkeypatch):
    out = _resolve(
        monkeypatch,
        "https://www.amazon.co.uk/dp/B08XYZ1234?tag=someone-21",
        "🔥 https://a.co/d/9zKq2",
    )
    assert "https://www.amazon.co.uk/dp/B08XYZ1234?tag=creator-21" in out


def test_unresolvable_short_stays_visible_and_is_left_untouched(monkeypatch):
    text = "🔥 Loot ₹499 https://amzn.asia/d/xyz789"
    out = _resolve(monkeypatch, "https://amzn.asia/d/xyz789", text)
    assert out == text
    assert link_router.unresolved_amazon_shorts(out) == ["https://amzn.asia/d/xyz789"]


# --------------------------------------------------------------------------
# Our-link predicate used by the pre-dispatch guard
# --------------------------------------------------------------------------
def test_our_affiliate_urls_separates_paying_links_from_free_ones():
    assert commission_guard.our_affiliate_urls(
        f"🛍️ ₹199 https://fktr.in/abc123?affExtParam2={PUB}", TAG, STORE, PUB
    )
    assert commission_guard.our_affiliate_urls(
        "👗 ₹299 https://www.meesho.com/kurta/p/abc?mcn=LEHLAH", TAG, STORE, PUB
    )
    assert commission_guard.our_affiliate_urls(
        f"🔥 https://www.amazon.in/dp/B0ABCDEFGH?tag={TAG}", TAG, STORE, PUB
    )
    assert commission_guard.our_affiliate_urls(
        "🔥 https://www.flipkart.com/p/itm123", TAG, STORE, PUB
    ) == []
    assert commission_guard.our_affiliate_urls(
        "🔥 https://www.amazon.in/dp/B0ABCDEFGH?tag=someone-21", TAG, STORE, PUB
    ) == []
    assert commission_guard.our_affiliate_urls(
        "📢 https://example.com/news", TAG, STORE, PUB
    ) == []


def test_unmonetised_links_reports_merchant_failures_not_by_design_meesho():
    rendered = (
        "🔥 https://www.flipkart.com/p/itm123\n"
        "🛍️ https://www.meesho.com/s/p/7amuq5"
    )
    audit = commission_guard.audit_rendered_text(
        rendered, TAG, STORE, PUB,
        allowed_kinds={"amazon", "merchant", "hypd", "meesho", "lehlah"},
    )
    retryable = commission_guard.unmonetised_links(audit, {"merchant", "meesho"})
    assert [detail["kind"] for detail in retryable] == ["merchant"]
    by_design = commission_guard.unmonetised_links(
        audit, {"merchant", "meesho"}, kinds=commission_guard.UNMONETISABLE_KINDS
    )
    assert [detail["kind"] for detail in by_design] == ["meesho"]


def test_known_merchant_hosts_are_the_ones_earnkaro_is_expected_to_convert():
    assert link_router.is_known_merchant_host("https://www.flipkart.com/p/itm123")
    assert link_router.is_known_merchant_host(
        "https://www.myntra.com/shoes/brand/running-shoes/1234567/buy"
    )
    assert not link_router.is_known_merchant_host("https://tinystore.example/p/1")


# --------------------------------------------------------------------------
# 2. A conversion failure on an EarnKaro merchant never posts for free
# --------------------------------------------------------------------------
def _channel(monkeypatch, name: str, *, meesho: bool = False, settings: dict | None = None):
    """Set up one influencer + ready channel on the isolated test database."""
    for key, value in (settings or {}).items():
        db.set_global_setting(key, value)
    influencer_id = db.add_influencer(
        "Conversion Creator", TAG, allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", f"@{name}", status="ready",
        allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
    )
    sent: list[str] = []

    async def fake_dispatch(_influencer, _channel_row, rendered):
        sent.append(rendered)
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    return influencer_id, channel_id, sent


def _run(monkeypatch, name: str, text: str, *, settings: dict | None = None,
         credentials: bool = True, converted: dict | None = None):
    influencer_id, channel_id, sent = _channel(monkeypatch, name, settings=settings)
    if credentials:
        db.set_global_setting("earnkaro_api_key", "fake-key")
        db.set_global_setting("earnkaro_publisher_id", PUB)
    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value=converted or {}))
    result = asyncio.run(pipeline.render_and_dispatch(text, influencer_ids=[influencer_id]))
    return result[influencer_id][channel_id], sent


def test_conversion_failure_on_a_core_merchant_holds_the_deal_for_a_retry(monkeypatch):
    deal = "🔥 Shoes ₹499 https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    status, sent = _run(monkeypatch, "ek_down", deal)
    assert status == "skipped:no_our_affiliate_after_guard"
    assert sent == []
    # Nothing is recorded as posted, so the worker re-reads the source post and
    # publishes the deal (converted) as soon as EarnKaro answers again.
    influencer_id = db.list_influencers()[0]["id"]
    channel_id = db.list_channels(influencer_id)[0]["id"]
    assert not db.already_posted(
        influencer_id, channel_id, link_router.deal_signature(deal)
    )


def test_converted_merchant_posts_our_earnkaro_link(monkeypatch):
    target = "https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    status, sent = _run(
        monkeypatch, "ek_ok", f"🔥 Shoes ₹499 {target}",
        converted={target: f"https://ekaro.in/ek123456?affExtParam2={PUB}"},
    )
    assert status == "posted"
    assert f"https://ekaro.in/ek123456?affExtParam2={PUB}" in sent[0]
    assert target not in sent[0]


def test_zero_commission_post_is_allowed_when_credentials_are_not_configured(monkeypatch):
    """Without an EarnKaro key nothing can ever convert, so volume is untouched."""
    status, sent = _run(
        monkeypatch, "no_key", "🔥 Shoes ₹499 https://www.flipkart.com/p/itmABC123",
        credentials=False,
    )
    assert status == "posted"
    assert "flipkart.com" in sent[0]


def test_zero_commission_post_can_be_allowed_explicitly(monkeypatch):
    status, sent = _run(
        monkeypatch, "allow_free", "🔥 Shoes ₹499 https://www.flipkart.com/p/itmABC123",
        settings={"allow_unconverted_posts": "on"},
    )
    assert status == "posted"
    assert "flipkart.com" in sent[0]


def test_unsupported_store_is_still_posted_instead_of_being_retried_forever(monkeypatch):
    status, sent = _run(
        monkeypatch, "unknown_store", "🔥 ₹199 https://tinystore.example/p/itm123"
    )
    assert status == "posted"
    assert "tinystore.example" in sent[0]


def test_a_deal_with_an_amazon_link_is_posted_even_when_the_merchant_failed(monkeypatch):
    status, sent = _run(
        monkeypatch,
        "amazon_plus_flipkart",
        "🔥 Combo\nAmazon: https://www.amazon.in/dp/B0D9P2M1PB\n"
        "Flipkart: https://www.flipkart.com/x/p/itmAAA111?pid=P1",
    )
    assert status == "posted"
    assert f"tag={TAG}" in sent[0]


# --------------------------------------------------------------------------
# 3. The approval renderer never prints the same canonical link twice
# --------------------------------------------------------------------------
def test_approval_render_shows_the_shared_product_link_once():
    text = (
        "🔥 Soundbar ₹1499\n"
        "https://amzn.to/4dnF9lU?tag=old-21\n"
        "https://www.amazon.in/dp/B08XYZ1234?th=1&tag=competitor-21\n"
        "Flipkart: https://www.flipkart.com/x/p/itmAAA111"
    )
    expanded = link_router.expand_amazon_shorts_in_text(text, TAG)
    rendered = link_router.render_for_influencer(expanded, TAG, role="approval")
    canonical = "https://www.amazon.in/dp/B08XYZ1234?th=1&tag=creator-21"
    assert rendered.count(canonical) == 1
    assert "amzn.to" not in rendered
    assert "flipkart" not in rendered.lower()
    assert "#ad (paid link)" in rendered


# --------------------------------------------------------------------------
# 4. Held-back deals are retried by the worker, never dropped
# --------------------------------------------------------------------------
def test_the_retry_queue_is_idempotent_per_deal_and_reports_attempts():
    first = db.defer_deal(
        "🔥 Shoes ₹499 https://www.flipkart.com/p/itm123", source="@src", delay_seconds=0
    )
    again = db.defer_deal(
        "🔥 Shoes ₹499 https://www.flipkart.com/p/itm123", source="@src", delay_seconds=0
    )
    assert first == again
    assert db.count_deferred_deals() == 1
    assert db.deferred_deal_attempts("🔥 Shoes ₹499 https://www.flipkart.com/p/itm123", "@src") == 0

    due = db.due_deferred_deals()
    assert [row["deal_hash"] for row in due] == [first]
    db.note_deferred_attempt(first, delay_seconds=60, reason="still_unconverted")
    assert db.deferred_deal_attempts("🔥 Shoes ₹499 https://www.flipkart.com/p/itm123", "@src") == 1
    assert db.due_deferred_deals() == []
    db.delete_deferred_deal(first)
    assert db.count_deferred_deals() == 0


def test_parked_deal_is_retried_and_posted_converted_when_earnkaro_recovers(monkeypatch):
    from influencer_hub import worker

    deal = "🔥 Shoes ₹499 https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    monkeypatch.setattr(pipeline, "DEFERRED_RETRY_DELAY_SECONDS", 0)
    influencer_id, channel_id, sent = _channel(monkeypatch, "retry_ok")
    db.set_global_setting("earnkaro_api_key", "fake-key")
    db.set_global_setting("earnkaro_publisher_id", PUB)

    # Cycle 1: EarnKaro is down, so the deal is parked instead of posted free.
    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value={}))
    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id]))
    assert result[influencer_id][channel_id] == "skipped:no_our_affiliate_after_guard"
    assert sent == [] and db.count_deferred_deals() == 1

    # Cycle 2: EarnKaro recovered — the worker posts the converted deal.
    converted = f"https://ekaro.in/ek123456?affExtParam2={PUB}"
    monkeypatch.setattr(
        earnkaro, "convert_links",
        AsyncMock(return_value={deal.split()[-1]: converted}),
    )
    drained = asyncio.run(worker.drain_deferred_deals())
    assert drained == 1
    assert sent and converted in sent[0]
    assert db.count_deferred_deals() == 0


def test_parked_deal_is_posted_anyway_once_the_retry_budget_is_used_up(monkeypatch):
    from influencer_hub import worker

    deal = "🔥 Shoes ₹499 https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    monkeypatch.setattr(pipeline, "DEFERRED_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(pipeline, "DEFERRED_MAX_ATTEMPTS", 1)
    influencer_id, channel_id, sent = _channel(monkeypatch, "retry_budget")
    db.set_global_setting("earnkaro_api_key", "fake-key")
    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value={}))

    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id]))
    assert result[influencer_id][channel_id] == "skipped:no_our_affiliate_after_guard"
    assert db.count_deferred_deals() == 1

    # First drain retries without converting: the budget is spent.
    assert asyncio.run(worker.drain_deferred_deals()) == 0
    assert db.deferred_deal_attempts(deal, "") == 1

    # Second drain uses the last resort: post it rather than lose the deal.
    assert asyncio.run(worker.drain_deferred_deals()) == 1
    assert sent and "flipkart.com" in sent[0]
    assert db.count_deferred_deals() == 0


def test_flow_api_publishes_the_retry_queue(monkeypatch, tmp_path):
    from dashboard.app import app

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "flow-deferred.sqlite3")
    db.init()
    app.config.update(TESTING=True)
    client = app.test_client()

    assert client.get("/api/flow").get_json()["deferred"]["waiting"] == 0
    db.defer_deal("🔥 ₹199 https://www.flipkart.com/p/itm123", source="@src")
    payload = client.get("/api/flow").get_json()["deferred"]
    assert payload["waiting"] == 1
    assert payload["retry_seconds"] == pipeline.DEFERRED_RETRY_DELAY_SECONDS
    assert payload["allow_unconverted_posts"] is False


def test_money_radar_exposes_the_unconverted_switch(monkeypatch, tmp_path):
    from dashboard.app import MONEY_SWITCHES, app

    assert "allow_unconverted_posts" in MONEY_SWITCHES
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "money-switch.sqlite3")
    db.init()
    app.config.update(TESTING=True)
    client = app.test_client()
    response = client.post(
        "/money/switch", data={"setting": "allow_unconverted_posts", "value": "on"}
    )
    assert response.status_code == 302
    assert pipeline.allow_unconverted_posts_enabled() is True
