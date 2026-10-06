"""Attribution on Amazon is decided by the ``tag``, not by the ``/dp/`` path.

The leak this pins: a source deal that linked an Amazon search page or a
storefront carrying OUR Associate tag used to be flagged as "not OUR canonical
Amazon link" and deleted by the commission guard — throwing away commission on
a page that actually earned. Any Amazon URL with exactly one tag equal to ours
must be kept, and only a wrong or missing tag is a real leak.
"""
from __future__ import annotations

from influencer_hub import advanced_shortener, commission_guard, money_radar

TAG = "creator-21"


# --------------------------------------------------------------------------
# The predicate itself
# --------------------------------------------------------------------------
def test_tagged_search_and_storefront_pages_carry_our_attribution():
    for url in (
        f"https://www.amazon.in/s?k=running+shoes&tag={TAG}",
        f"https://www.amazon.in/stores/page/ABC?tag={TAG}",
        f"https://www.amazon.in/gp/bestsellers/kitchen?tag={TAG}",
        f"https://amzn.to/4dnF9lU?tag={TAG}",
    ):
        assert advanced_shortener.is_our_amazon_attribution(url, TAG), url


def test_attribution_requires_exactly_our_tag():
    assert not advanced_shortener.is_our_amazon_attribution(
        "https://www.amazon.in/s?k=shoes", TAG
    )
    assert not advanced_shortener.is_our_amazon_attribution(
        "https://www.amazon.in/s?k=shoes&tag=someone-21", TAG
    )
    assert not advanced_shortener.is_our_amazon_attribution(
        f"https://www.amazon.in/s?k=shoes&tag={TAG}&tag=someone-21", TAG
    )
    assert not advanced_shortener.is_our_amazon_attribution(
        f"https://www.flipkart.com/s?k=shoes&tag={TAG}", TAG
    )


# --------------------------------------------------------------------------
# Commission guard: keep the page instead of deleting it
# --------------------------------------------------------------------------
def test_guard_keeps_a_tagged_search_page():
    rendered = f"Grab it now 🔥 https://www.amazon.in/s?k=loot&tag={TAG}"
    audit = commission_guard.audit_rendered_text(rendered, TAG, "")

    assert audit["ok"] is True
    assert audit["amazon_ok"] is True
    assert audit["issues"] == []
    detail = next(d for d in audit["details"] if d["kind"] == "amazon")
    assert detail["ok"] is True
    assert TAG in detail["reason"]

    kept, _ = commission_guard.sanitize_rendered_text(rendered, TAG, "")
    assert kept == rendered


def test_guard_still_strips_a_search_page_tagged_for_someone_else():
    rendered = "Grab it now 🔥 https://www.amazon.in/s?k=loot&tag=someone-21"
    audit = commission_guard.audit_rendered_text(rendered, TAG, "")

    assert audit["ok"] is False
    assert audit["amazon_ok"] is False
    assert any("someone-21" in issue or "not OUR canonical" in issue
               for issue in audit["issues"])

    kept, _ = commission_guard.sanitize_rendered_text(rendered, TAG, "")
    assert "someone-21" not in kept
    assert "Grab it now" in kept


def test_guard_still_strips_an_untagged_search_page():
    rendered = "Grab it now 🔥 https://www.amazon.in/s?k=loot"
    audit = commission_guard.audit_rendered_text(rendered, TAG, "")

    assert audit["ok"] is False
    kept, _ = commission_guard.sanitize_rendered_text(rendered, TAG, "")
    assert "amazon.in/s" not in kept


def test_guard_keeps_the_product_page_path_working():
    rendered = f"Deal https://www.amazon.in/dp/B0D9P2M1PB?tag={TAG}"
    audit = commission_guard.audit_rendered_text(rendered, TAG, "")

    assert audit["ok"] is True
    assert audit["amazon_ok"] is True


# --------------------------------------------------------------------------
# Money Radar: a tagged search page is earning, not a leak
# --------------------------------------------------------------------------
def test_money_radar_counts_a_tagged_search_page_as_earning():
    entry = money_radar.classify_link(
        f"https://www.amazon.in/s?k=loot&tag={TAG}", TAG
    )
    assert entry["state"] == money_radar.STATE_EARNING
    assert TAG in entry["reason"]


def test_money_radar_still_flags_wrong_and_missing_tags():
    wrong = money_radar.classify_link(
        "https://www.amazon.in/s?k=loot&tag=someone-21", TAG
    )
    missing = money_radar.classify_link("https://www.amazon.in/s?k=loot", TAG)
    assert wrong["state"] == money_radar.STATE_LEAK
    assert missing["state"] == money_radar.STATE_LEAK
