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


# --------------------------------------------------------------------------
# 5. Wrapper links (bit.ly / tinyurl / Flipkart's own shorts / amzn shorts)
#    hide their destination — and their attribution — until they are resolved.
# --------------------------------------------------------------------------
WRAPPERS = (
    "https://bit.ly/3xyzFlip",
    "https://tinyurl.com/amazdeal",
    "https://dl.flipkart.com/s/abcXYZ",
    "https://fkrt.it/abc123",
    "https://cutt.ly/abc123",
    "https://amzn.to/4dnF9lU",
)


def test_every_wrapper_host_is_classified_as_an_opaque_link():
    for url in WRAPPERS:
        assert link_router.is_opaque_link(url), url
        # A wrapper is never filed away as "info": the guard must be able to see
        # that its destination (and therefore its attribution) is unknown.
        assert link_router.classify_url(url) == "shortener" or link_router.is_amazon_short_host(url), url
        assert link_router.has_opaque_link(f"deal {url}")


def test_wrappers_without_a_scheme_are_seen_too():
    text = "🔥 Loot ₹499 bit.ly/3xyzFlip and amzn.to/4dnF9lU grab fast"
    kinds = sorted(link_router.classify_url(url) for _, _, url in link_router.opaque_link_spans(text))
    assert link_router.has_opaque_link(text)
    assert len(kinds) == 2
    assert link_router.unresolved_opaque_links(text) == [
        "https://bit.ly/3xyzFlip",
        "https://amzn.to/4dnF9lU",
    ]


def _resolve_with(final_urls: dict[str, str | None]):
    """A fake resolver: wrapper -> final destination (or None when unresolved)."""

    async def _fake(url: str, timeout: float = 0.0):
        for wrapper, destination in final_urls.items():
            if wrapper in url:
                return destination
        return None

    return _fake


def test_a_wrapper_around_an_amazon_link_becomes_our_canonical_link():
    text = "🔥 Soundbar ₹1499 https://bit.ly/amazdeal"
    out, unresolved = asyncio.run(
        link_router.resolve_opaque_links_in_text_async(
            text, TAG,
            resolve=_resolve_with(
                {"bit.ly/amazdeal": "https://www.amazon.in/Soundbar/dp/B08XYZ1234?th=1&tag=someone-21"}
            ),
        )
    )
    assert out == "🔥 Soundbar ₹1499 https://www.amazon.in/dp/B08XYZ1234?th=1&tag=creator-21"
    assert unresolved == []


def test_a_wrapper_around_a_merchant_link_becomes_convertible():
    wrapper = "https://tinyurl.com/flipkart-deal"
    target = "https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    out, unresolved = asyncio.run(
        link_router.resolve_opaque_links_in_text_async(
            f"🔥 Shoes ₹499 {wrapper}", TAG, resolve=_resolve_with({"tinyurl.com/flipkart-deal": target})
        )
    )
    assert unresolved == []
    # The real destination is a known merchant, so EarnKaro converts it.
    assert link_router.classify_url(link_router.find_urls(out)[0]) == "merchant"
    converted = {target: f"https://ekaro.in/ek987654?affExtParam2={PUB}"}
    rendered = link_router.render_for_influencer(out, TAG, converted)
    assert f"https://ekaro.in/ek987654?affExtParam2={PUB}" in rendered
    assert commission_guard.our_affiliate_urls(rendered, TAG, STORE, PUB)


def test_an_unresolvable_wrapper_is_reported_instead_of_posted_silently():
    text = "🔥 Loot ₹199 https://tinyurl.com/abc123"
    out, unresolved = asyncio.run(
        link_router.resolve_opaque_links_in_text_async(text, TAG, resolve=_resolve_with({}))
    )
    assert out == text
    assert unresolved == ["https://tinyurl.com/abc123"]
    audit = commission_guard.audit_rendered_text(out, TAG, STORE, PUB, source_text=text)
    wrapped = commission_guard.unmonetised_links(
        audit, {"amazon", "merchant", "hypd", "meesho", "lehlah", "shortener"},
        kinds=commission_guard.WRAPPED_KINDS,
    )
    assert [detail["kind"] for detail in wrapped] == ["shortener"]
    assert "destination unknown" in wrapped[0]["reason"]


def test_an_informational_wrapper_is_not_an_unresolved_one():
    """A wrapper we *did* resolve to a promo page must not park the deal."""
    text = "🔥 Loot ₹199 https://bit.ly/promo"
    out, unresolved = asyncio.run(
        link_router.resolve_opaque_links_in_text_async(
            text, TAG, resolve=_resolve_with({"bit.ly/promo": "https://www.youtube.com/watch?v=x"})
        )
    )
    assert unresolved == []
    assert out == text  # the source's own promo link is left alone


def test_a_wrapper_carried_by_the_source_post_is_never_counted_as_ours():
    """Our own bit.ly wrappers are minted after rendering — a source wrapper is not ours."""
    source = "🔥 Deal ₹499 https://bit.ly/3xyzFlip"
    assert commission_guard.our_affiliate_urls(source, TAG, STORE, PUB, source_text=source) == []
    audit = commission_guard.audit_rendered_text(source, TAG, STORE, PUB, source_text=source)
    detail = next(d for d in audit["details"] if "bit.ly" in d["url"])
    assert detail["ok"] is False and "not created by us" in detail["reason"]
    # Without provenance (a post we rendered ourselves) the wrapper stays OURS.
    assert commission_guard.our_affiliate_urls(source, TAG, STORE, PUB)


def test_two_products_never_borrow_each_others_short_link():
    """Two shorts + two different ASINs: the offline heuristic must not guess."""
    text = (
        "🔥 Combo\n"
        "https://amzn.to/aaa111\n"
        "https://www.amazon.in/dp/B08XYZ1234?tag=old-21\n"
        "https://amzn.to/bbb222\n"
        "https://www.amazon.in/dp/B0D9P2M1PB?tag=old-21"
    )
    assert link_router.expand_amazon_shorts_in_text(text, TAG) == text


def _pipeline_for(monkeypatch, name: str):
    influencer_id, channel_id, sent = _channel(monkeypatch, name)
    db.set_global_setting("earnkaro_api_key", "fake-key")
    db.set_global_setting("earnkaro_publisher_id", PUB)
    return influencer_id, channel_id, sent


def test_pipeline_parks_a_deal_whose_only_link_is_an_unresolved_wrapper(monkeypatch):
    influencer_id, channel_id, sent = _pipeline_for(monkeypatch, "wrapped_only")
    deal = "🔥 Loot ₹199 https://tinyurl.com/abc123"

    async def fake_resolve(text, tag=None, **kwargs):
        return text, ["https://tinyurl.com/abc123"]

    monkeypatch.setattr(link_router, "resolve_opaque_links_in_text_async", fake_resolve)
    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "DEFERRED_RETRY_DELAY_SECONDS", 0)
    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id]))
    assert result[influencer_id][channel_id] == "skipped:no_our_affiliate_after_guard"
    assert sent == []
    assert db.count_deferred_deals() == 1
    assert db.due_deferred_deals()[0]["reason"] == "unresolved_wrapper"


def test_pipeline_posts_our_link_when_a_wrapper_resolves_to_a_merchant(monkeypatch):
    influencer_id, channel_id, sent = _pipeline_for(monkeypatch, "wrapped_ok")
    target = "https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    deal = "🔥 Shoes ₹499 https://bit.ly/3xyzFlip"

    async def fake_resolve(text, tag=None, **kwargs):
        return text.replace("https://bit.ly/3xyzFlip", target), []

    monkeypatch.setattr(link_router, "resolve_opaque_links_in_text_async", fake_resolve)
    monkeypatch.setattr(
        earnkaro, "convert_links",
        AsyncMock(return_value={target: f"https://ekaro.in/ek555555?affExtParam2={PUB}"}),
    )
    result = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id]))
    assert result[influencer_id][channel_id] == "posted"
    assert f"https://ekaro.in/ek555555?affExtParam2={PUB}" in sent[0]
    assert "bit.ly" not in sent[0]
    assert db.count_deferred_deals() == 0


def test_audit_links_cli_reports_the_verdict_and_exits_on_our_link(capsys):
    from influencer_hub import cli

    code = cli.main([
        "audit-links", "--offline",
        "--text", "🔥 Shoes ₹499 https://www.amazon.in/dp/B08XYZ1234?tag=old-21",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "[OUR] amazon" in out
    assert f"tag={TAG}" not in out  # the configured tag is used, not the test one
    assert "POST: 1 of OUR affiliate link(s) present" in out


def test_audit_links_cli_holds_a_deal_with_no_our_link(capsys):
    from influencer_hub import cli

    code = cli.main([
        "audit-links", "--offline",
        "--text", "🔥 Loot ₹199 https://tinystore.example/p/itm123",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "HOLD: no link in this post pays us" in out


# --------------------------------------------------------------------------
# 6. Links written without http:// must be seen too — otherwise they travel
#    through the post untouched (untagged Amazon, unconverted merchant).
# --------------------------------------------------------------------------
def test_scheme_less_known_links_are_promoted_and_then_converted():
    promoted = link_router.promote_scheme_less_links(
        "🔥 Loot ₹499 flipkart.com/nike-shoes/p/itmABC123?pid=SHO123 and amazon.in/dp/B08XYZ1234?tag=old-21"
    )
    urls = link_router.find_urls(promoted)
    assert urls == [
        "https://flipkart.com/nike-shoes/p/itmABC123?pid=SHO123",
        "https://amazon.in/dp/B08XYZ1234?tag=old-21",
    ]
    assert {link_router.classify_url(url) for url in urls} == {"merchant", "amazon"}
    rendered = link_router.render_for_influencer(promoted, TAG)
    assert f"https://www.amazon.in/dp/B08XYZ1234?tag={TAG}" in rendered
    assert "tag=old-21" not in rendered


def test_scheme_less_promotion_leaves_prose_addresses_and_unknown_hosts_alone():
    prose = "available on amazon.in today, mail deals@flipkart.com, see myflipkart.com.evil.com/x"
    assert link_router.promote_scheme_less_links(prose) == prose
    untouched = "already fine https://www.flipkart.com/x/p/itm1"
    assert link_router.promote_scheme_less_links(untouched) == untouched


def test_pipeline_converts_a_scheme_less_merchant_link(monkeypatch):
    influencer_id, channel_id, sent = _pipeline_for(monkeypatch, "bare_flipkart")
    target = "https://flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    monkeypatch.setattr(
        earnkaro, "convert_links",
        AsyncMock(return_value={target: f"https://ekaro.in/ek424242?affExtParam2={PUB}"}),
    )
    result = asyncio.run(pipeline.render_and_dispatch(
        "🔥 Shoes ₹499 flipkart.com/nike-shoes/p/itmABC123?pid=SHO123",
        influencer_ids=[influencer_id],
    ))
    assert result[influencer_id][channel_id] == "posted"
    assert f"https://ekaro.in/ek424242?affExtParam2={PUB}" in sent[0]
    assert "tag=old" not in sent[0]


def test_a_wrapper_parked_deal_is_retried_and_published_converted(monkeypatch):
    """The worker drain must rescue a deal that was held for an unresolved wrapper."""
    from influencer_hub import worker

    influencer_id, channel_id, sent = _pipeline_for(monkeypatch, "wrapped_retry")
    target = "https://www.flipkart.com/nike-shoes/p/itmABC123?pid=SHO123"
    deal = "🔥 Shoes ₹499 https://bit.ly/3xyzFlip"
    monkeypatch.setattr(pipeline, "DEFERRED_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(earnkaro, "credentials_configured", lambda: True)
    monkeypatch.setattr(
        earnkaro, "convert_links",
        AsyncMock(return_value={target: f"https://ekaro.in/ek777777?affExtParam2={PUB}"}),
    )

    async def unresolved_resolver(text, tag=None, **kwargs):
        return text, ["https://bit.ly/3xyzFlip"]

    monkeypatch.setattr(link_router, "resolve_opaque_links_in_text_async", unresolved_resolver)
    first = asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[influencer_id]))
    assert first[influencer_id][channel_id] == "skipped:no_our_affiliate_after_guard"
    assert db.count_deferred_deals() == 1
    assert sent == []

    async def resolving(text, tag=None, **kwargs):
        return text.replace("https://bit.ly/3xyzFlip", target), []

    monkeypatch.setattr(link_router, "resolve_opaque_links_in_text_async", resolving)
    drained = asyncio.run(worker.drain_deferred_deals(limit=5))
    assert drained == 1
    assert db.count_deferred_deals() == 0
    assert f"https://ekaro.in/ek777777?affExtParam2={PUB}" in sent[0]


def test_a_hypd_store_page_without_an_afflink_is_never_posted_as_ours(monkeypatch):
    """Nothing can mint an affiliate token from a plain store page — skip it."""
    status, sent = _run(
        monkeypatch, "hypd_page", "🔥 Loot ₹499 https://hypd.store/products/abc123"
    )
    assert status == "skipped:no_our_affiliate_after_guard"
    assert sent == []


# --------------------------------------------------------------------------
# 7. "I add my Telegram channel, hit save, and it must post with MY tag"
# --------------------------------------------------------------------------
ADMIN_PASSWORD = "test-admin-password-123"


def _csrf_token(html: str) -> str:
    import re as _re

    match = _re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "rendered page is missing its CSRF token"
    return match.group(1)


def _dash_client(monkeypatch, tmp_path, name: str):
    """A logged-in dashboard client (the real path: password + CSRF token)."""
    import dashboard.app as dashboard_module

    monkeypatch.setitem(dashboard_module.app.config, "TESTING", False)
    monkeypatch.setattr(config, "HUB_ENV", "development")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "ADMIN_DELETE_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / f"{name}.sqlite3")
    db.init()
    db.migrate()
    client = dashboard_module.app.test_client()
    login_token = _csrf_token(client.get("/login").get_data(as_text=True))
    assert client.post(
        "/login", data={"password": ADMIN_PASSWORD, "_csrf_token": login_token}
    ).status_code == 302
    page = client.get("/influencer/1").get_data(as_text=True)
    if "name=\"_csrf_token\"" not in page:
        page = client.get("/easy-setup").get_data(as_text=True)
    return client, _csrf_token(page)


def _fake_dispatch(monkeypatch):
    sent: list[str] = []

    async def fake(_influencer, _channel, rendered):
        sent.append(rendered)
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake)
    return sent


def test_tag_validation_refuses_a_value_the_renderer_would_replace():
    assert link_router.normalize_amazon_tag("mydealtag-21") == ("mydealtag-21", "", False)
    value, note, blocked = link_router.normalize_amazon_tag("my tag")
    assert (value, blocked) == ("", True) and "not a usable Amazon" in note
    # Usable but non-standard: accepted, with a warning the operator can see.
    value, note, blocked = link_router.normalize_amazon_tag("mytag2024")
    assert (value, blocked) == ("mytag2024", False) and "does not look like" in note
    assert link_router.normalize_amazon_tag("") == ("", "", False)


def test_tag_proof_shows_the_exact_amazon_link_and_flags_a_broken_tag():
    proof = commission_guard.amazon_tag_proof("mydealtag-21")
    assert proof["ok"] is True
    assert proof["posted_link"] == "https://www.amazon.in/dp/B0D9P2M1PB?tag=mydealtag-21"
    assert proof["fallback_used"] is False
    broken = commission_guard.amazon_tag_proof("my tag")
    assert broken["ok"] is True  # the fallback tag still produces a paying link
    assert broken["fallback_used"] is True and broken["tag_usable"] is False
    assert broken["tag"] == config.AMAZON_ASSOCIATE_TAG


def test_new_channel_posts_with_the_creators_tag_after_saving(monkeypatch, tmp_path):
    """Identical to the operator's flow: type the channel, press Save."""
    client, token = _dash_client(monkeypatch, tmp_path, "save-channel")
    influencer_id = db.add_influencer("Save Creator", "mydealtag-21")

    response = client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={
            "platform": "telegram", "identifier": "@save_loots", "role": "broadcast",
            "_csrf_token": token,
        },
    )
    assert response.status_code == 302
    channel = db.list_channels(influencer_id)[0]
    # Every network the creator has on stays on: saving must not switch them off.
    assert channel["status"] == "ready"
    assert (channel["allow_amazon"], channel["allow_earnkaro"], channel["allow_hypd"]) == (1, 1, 1)

    sent = _fake_dispatch(monkeypatch)
    monkeypatch.setattr(
        earnkaro, "convert_links",
        AsyncMock(return_value={
            "https://www.flipkart.com/nike/p/itmABC123": f"https://ekaro.in/ek123?affExtParam2={PUB}"
        }),
    )
    asyncio.run(pipeline.render_and_dispatch(
        "🔥 Loot\nAmazon: https://www.amazon.in/dp/B08XYZ1234?tag=old-21\n"
        "Flipkart: https://www.flipkart.com/nike/p/itmABC123",
        influencer_ids=[influencer_id],
    ))
    assert sent, "nothing was posted to the freshly saved channel"
    assert f"tag=mydealtag-21" in sent[0]
    assert "tag=old-21" not in sent[0]
    assert f"https://ekaro.in/ek123?affExtParam2={PUB}" in sent[0]


def test_new_channel_honours_an_explicit_off(monkeypatch, tmp_path):
    """A ticked/unticked box in the form is still an explicit choice."""
    client, token = _dash_client(monkeypatch, tmp_path, "save-channel-off")
    influencer_id = db.add_influencer("Off Creator", "offtag-21")

    client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={
            "platform": "telegram", "identifier": "@off_loots", "role": "broadcast",
            "allow_amazon": "0", "allow_earnkaro": "0", "allow_hypd": "0",
            "_csrf_token": token,
        },
    )
    channel = db.list_channels(influencer_id)[0]
    assert (channel["allow_amazon"], channel["allow_earnkaro"]) == (0, 0)


def test_new_channel_with_an_unusable_tag_is_refused_with_a_message(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "save-bad-tag")
    influencer_id = db.add_influencer("Typo Creator", "typo-21")

    response = client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={
            "platform": "telegram", "identifier": "@typo_loots", "role": "broadcast",
            "amazon_override_tag": "my tag", "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"not a usable Amazon" in response.data
    assert db.list_channels(influencer_id) == []  # nothing was saved


def test_channel_override_tag_wins_over_the_creator_tag(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "override-tag")
    influencer_id = db.add_influencer("Override Creator", "creatortag-21")

    client.post(
        f"/influencer/{influencer_id}/add-manual-channel",
        data={
            "platform": "telegram", "identifier": "@override_loots", "role": "broadcast",
            "amazon_override_tag": "channeltag-21", "_csrf_token": token,
        },
    )
    channel = db.list_channels(influencer_id)[0]
    assert channel["amazon_override_tag"] == "channeltag-21"

    sent = _fake_dispatch(monkeypatch)
    asyncio.run(pipeline.render_and_dispatch(
        "🔥 Deal https://www.amazon.in/dp/B08XYZ1234?tag=old-21",
        influencer_ids=[influencer_id],
    ))
    assert "tag=channeltag-21" in sent[0]


def test_easy_setup_screen_posts_with_the_tag_typed_on_it(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "easy-setup")
    response = client.post(
        "/easy-setup",
        data={
            "name": "Easy Creator",
            "amazon_tag": "easydeal-21",
            "main_channel": "@easy_loots",
            "allow_amazon": "1", "allow_earnkaro": "1", "allow_hypd": "1",
            "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"Amazon deals will post as" in response.data
    assert b"tag=easydeal-21" in response.data

    influencer = db.list_influencers()[0]
    assert influencer["amazon_tag"] == "easydeal-21"
    sent = _fake_dispatch(monkeypatch)
    asyncio.run(pipeline.render_and_dispatch(
        "🔥 Deal https://www.amazon.in/dp/B08XYZ1234?tag=old-21",
        influencer_ids=[influencer["id"]],
    ))
    assert sent and "tag=easydeal-21" in sent[0]


def test_easy_setup_without_the_switches_still_posts_every_network(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "easy-defaults")
    client.post(
        "/easy-setup",
        data={
            "name": "Default Creator", "amazon_tag": "deftag-21",
            "main_channel": "@deftag_loots", "_csrf_token": token,
        },
    )
    influencer = db.list_influencers()[0]
    assert (influencer["allow_amazon"], influencer["allow_earnkaro"], influencer["allow_hypd"]) == (1, 1, 1)
    channel = db.list_channels(influencer["id"])[0]
    assert (channel["allow_amazon"], channel["allow_earnkaro"], channel["allow_hypd"]) == (1, 1, 1)
    assert channel["status"] == "ready"


def test_easy_setup_refuses_a_tag_that_cannot_be_used(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "easy-bad-tag")
    response = client.post(
        "/easy-setup",
        data={
            "name": "Bad Tag Creator", "amazon_tag": "my tag",
            "main_channel": "@bad_tag_loots", "_csrf_token": token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"not a usable Amazon" in response.data
    assert db.list_influencers() == []  # no creator was created


def test_pipeline_warns_once_when_a_broken_tag_falls_back(monkeypatch, tmp_path, caplog):
    _dash_client(monkeypatch, tmp_path, "fallback-warn")
    influencer_id = db.add_influencer("Legacy Creator", "my tag")
    db.add_channel(influencer_id, "telegram", "@legacy_loots", status="ready", role="broadcast")
    _fake_dispatch(monkeypatch)
    pipeline._TAG_FALLBACK_WARNED.clear()

    with caplog.at_level("WARNING", logger="influencer_hub.pipeline"):
        asyncio.run(pipeline.render_and_dispatch(
            "🔥 Deal https://www.amazon.in/dp/B08XYZ1234", influencer_ids=[influencer_id]
        ))
        asyncio.run(pipeline.render_and_dispatch(
            "🔥 Deal 2 https://www.amazon.in/dp/B0ABCDEFGH", influencer_ids=[influencer_id]
        ))
    warnings = [r.message for r in caplog.records if "AMAZON TAG UNUSABLE" in str(r.message)]
    assert len(warnings) == 1, warnings


def test_preview_endpoint_reports_our_links_wrappers_and_verdict(monkeypatch, tmp_path):
    client, token = _dash_client(monkeypatch, tmp_path, "preview-links")
    response = client.post(
        "/api/test-render-deal",
        data={
            "sample_text": "🔥 Deal https://www.amazon.in/dp/B08XYZ1234?tag=old-21 and bit.ly/3xyzFlip",
            "amazon_tag": "previewtag-21",
            "role": "broadcast",
            "_csrf_token": token,
        },
    )
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["verdict"] == "our_link_present"
    assert payload["our_links"] == ["https://www.amazon.in/dp/B08XYZ1234?tag=previewtag-21"]
    # The wrapper stays visible until it can be resolved (no network in tests).
    assert "https://bit.ly/3xyzFlip" in payload["unresolved_wrappers"]
