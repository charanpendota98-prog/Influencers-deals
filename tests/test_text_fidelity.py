"""Source text fidelity: nothing goes missing, nothing appears from nowhere.

The operator's rule, in their words: "konni sarlu source lo unna text miss
cheyatam and unwanted text add avvatam jaragakudadu … text miss cheyaku" — the
deal text in a source post must reach the creator's channel, and text that was
never in the source must never appear there.

This module pins:

* :mod:`influencer_hub.fidelity` — which lines are product copy (losing them
  changes the deal) and which are channel promotion (meant to go);
* the block-aware network filter — a switched-off network takes its product's
  copy with it (title, price, sizes), while a block with an allowed link keeps
  its copy and loses only the disabled URLs and their dangling labels;
* the pipeline end to end — a rich render that would lose a line is replaced by
  the conservative render (every source line kept, same links, same shortener),
  and the delivery record says exactly what changed.
"""
from __future__ import annotations

import asyncio
import re

import pytest

from influencer_hub import config, db, fidelity, link_router, pipeline

MIXED_SOURCE = (
    "🔥 BOAT AIRDOPES 141\n"
    "MRP: ₹2990 | Deal Price: ₹1299 (57% OFF)\n"
    "Buy Now: https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21\n"
    "\n"
    "NIRLON Shirt at ₹399 — Sizes: M, L, XL\n"
    "https://www.flipkart.com/nirlon-shirt/p/itm1234abcd\n"
    "Join @LootZoneIndia11 for more loots\n"
)
AMAZON_ONLY_SOURCE = (
    "🔥 BOAT AIRDOPES 141 at ₹1299\n"
    "https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21\n"
)


def _creator(name="Fidelity Creator", tag="fidelity-21", **kwargs) -> int:
    return db.add_influencer(name, tag, **kwargs)


def _wired(monkeypatch):
    posted: list[tuple[str, str]] = []

    async def fake_dispatch(_influencer, _channel, text):
        posted.append((str(_channel.get("identifier")), text))
        return "posted"

    from unittest.mock import AsyncMock

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    return posted


# --------------------------------------------------------------------------
# 1. The comparison itself
# --------------------------------------------------------------------------
def test_compare_accepts_a_render_that_keeps_the_deal_text():
    rendered = link_router.render_for_influencer(AMAZON_ONLY_SOURCE, "fidelity-21")
    report = fidelity.compare(AMAZON_ONLY_SOURCE, rendered)
    assert report["ok"] is True
    assert report["missing"] == [] and report["added"] == []
    assert report["links"]["dropped"] == []       # the URL survives, retagged
    assert report["links"]["kept"] == 1
    assert "preserved" in report["summary"]


def test_compare_catches_a_missing_price_line():
    rendered = ("🔥 BOAT AIRDOPES 141\n"
                "https://www.amazon.in/dp/B09N3ZNHTY?tag=fidelity-21")
    report = fidelity.compare(AMAZON_ONLY_SOURCE, rendered)
    assert report["ok"] is False
    assert report["missing"] == ["🔥 BOAT AIRDOPES 141 at ₹1299"]
    assert report["counts"]["missing"] == 1
    assert "missing" in report["summary"]


def test_compare_catches_a_dropped_product():
    """A whole second product vanishing is the worst case: it is reported."""
    rendered = link_router.render_for_influencer(AMAZON_ONLY_SOURCE, "fidelity-21")
    report = fidelity.compare(MIXED_SOURCE, rendered)
    assert report["ok"] is False
    assert any("NIRLON" in line for line in report["missing"])
    assert report["links"]["dropped"] == ["www.flipkart.com/nirlon-shirt/p/itm1234abcd"]


def test_compare_catches_an_invented_line():
    rendered = (link_router.render_for_influencer(AMAZON_ONLY_SOURCE, "fidelity-21")
                + "\nGrab it fast — limited stock!")
    report = fidelity.compare(AMAZON_ONLY_SOURCE, rendered)
    assert report["ok"] is False
    assert report["added"] == ["Grab it fast — limited stock!"]


def test_a_rewritten_link_is_not_a_missing_product():
    """EarnKaro conversions, own tags and shortened links are our own rewrites."""
    source = ("🔥 BOAT AIRDOPES at ₹1299\n"
              "https://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21\n"
              "NIRLON Shirt at ₹399\n"
              "https://www.flipkart.com/nirlon/p/itm1234abcd")
    rendered = ("🔥 BOAT AIRDOPES at ₹1299\n"
                "https://www.amazon.in/dp/B09N3ZNHTY?tag=fidelity-21\n"
                "NIRLON Shirt at ₹399\n"
                "https://ekaro.in/our-conversion")
    report = fidelity.compare(source, rendered)
    assert report["ok"] is True, report["summary"]
    assert report["missing"] == [] and report["added"] == []
    assert report["links"]["rewritten"] == 1        # reported, not a loss
    assert report["links"]["dropped"] == []

    # …but a link that really vanished is a loss.
    loss = fidelity.compare(source, rendered.replace("https://ekaro.in/our-conversion", ""))
    assert loss["ok"] is False and loss["link_loss"] is True
    assert "fewer links" in loss["summary"]


def test_compare_allows_the_disclosures_the_hub_adds_on_purpose():
    base = link_router.render_for_influencer(AMAZON_ONLY_SOURCE, "fidelity-21")
    for extra in ("\n\n#ad (paid link)", "\n👉 Shop: https://t.me/creator"):
        report = fidelity.compare(AMAZON_ONLY_SOURCE, base + extra)
        assert report["ok"] is True, report["added"]


def test_promotion_lines_are_reported_as_removed_not_missing():
    rendered = link_router.render_for_influencer(MIXED_SOURCE, "fidelity-21")
    report = fidelity.compare(MIXED_SOURCE, rendered)
    assert any("Join @LootZoneIndia11" in line for line in report["promo_dropped"])
    assert not any("Join @LootZoneIndia11" in line for line in report["missing"])
    assert fidelity.is_meaningful("Join @LootZoneIndia11 for more loots") is False
    assert fidelity.is_meaningful("MRP: ₹2990 | Deal Price: ₹1299") is True
    assert fidelity.is_meaningful("BOAT AIRDOPES 141") is True
    assert fidelity.is_meaningful("🔥🔥🔥") is False


def test_enforce_swaps_in_the_conservative_render_when_text_would_be_lost():
    source = AMAZON_ONLY_SOURCE
    lossy = "🔥 BOAT AIRDOPES 141\nhttps://www.amazon.in/dp/B09N3ZNHTY?tag=fidelity-21"

    def conservative() -> str:
        return link_router.render_for_influencer(source, "fidelity-21", clean_promos=False)

    rendered, report = fidelity.enforce(source, lossy, fallback_render=conservative)
    assert report["recovered"] is True
    assert "₹1299" in rendered
    assert report["previous"]["missing"] == ["🔥 BOAT AIRDOPES 141 at ₹1299"]
    assert report["ok"] is True

    # A fallback that is not better is refused: the original render stays.
    rendered, report = fidelity.enforce(
        source, lossy, fallback_render=lambda: "🔥 BOAT AIRDOPES 141"
    )
    assert rendered == lossy and report["ok"] is False

    # No fallback offered: the render is returned with its report, never dropped.
    rendered, report = fidelity.enforce(source, lossy)
    assert rendered == lossy and report["ok"] is False


# --------------------------------------------------------------------------
# 2. The network filter: a switched-off network takes its product with it
# --------------------------------------------------------------------------
def test_a_product_block_of_a_disabled_network_goes_whole():
    kept, dropped = link_router.partition_disallowed_blocks(MIXED_SOURCE, {"amazon"})
    assert "BOAT AIRDOPES" in kept and "tag=" not in kept.split("BOAT")[0]
    assert "NIRLON" not in kept and "Sizes" not in kept      # copy went with the link
    assert "flipkart" not in kept.lower()
    assert len(dropped) == 1
    assert dropped[0]["kinds"] == ["merchant"]
    assert dropped[0]["lines"][0].startswith("NIRLON Shirt at ₹399")


def test_a_block_with_an_allowed_link_keeps_its_copy():
    source = (
        "SHOES ₹299 Sizes 6-10\n"
        "https://www.flipkart.com/shoes/p/itm9999\n"
        "https://www.amazon.in/dp/B0ABCDEFGH?tag=x-21\n"
        "Flipkart: https://www.flipkart.com/other/p/itm8888\n"
    )
    kept, dropped = link_router.partition_disallowed_blocks(source, {"amazon"})
    assert "SHOES ₹299 Sizes 6-10" in kept
    assert "B0ABCDEFGH" in kept
    assert "flipkart.com" not in kept
    # The dangling label of the removed link ("Flipkart:") is gone too.
    assert not re.search(r"(?i)^\s*flipkart\s*:?\s*$", kept, re.M)
    assert dropped == []


def test_dangling_network_labels_are_not_left_behind():
    source = "Amazon https://www.amazon.in/dp/B0ABCDEFGH?tag=x-21\nMeesho:\n"
    kept, _dropped = link_router.partition_disallowed_blocks(source, {"amazon", "hypd"})
    assert "Meesho" not in kept


def test_the_filter_keeps_informational_links_untouched():
    source = "Details at https://example.com/info\nhttps://www.amazon.in/dp/B0ABCDEFGH?tag=x-21"
    kept, _dropped = link_router.partition_disallowed_blocks(source, {"amazon"})
    assert "https://example.com/info" in kept


# --------------------------------------------------------------------------
# 3. The pipeline: every post is checked before it goes out
# --------------------------------------------------------------------------
def test_a_post_that_would_lose_a_line_is_sent_conservatively(monkeypatch):
    posted = _wired(monkeypatch)
    inf_id = _creator()
    db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready", role="broadcast")

    # The rich renderer drops this line only if it looks like a watermark; the
    # point of the test is the comparison, so drive it through a source whose
    # price line a *different* cleaner would eat.
    deal = "🔥 BOAT AIRDOPES 141\nBuy now at just ₹1299 only\nhttps://www.amazon.in/dp/B09N3ZNHTY?tag=someoneelse-21"
    asyncio.run(pipeline.render_and_dispatch(deal, influencer_ids=[inf_id]))

    assert posted, "the deal was posted"
    text = posted[0][1]
    assert "₹1299" in text                       # the price line survived
    assert "tag=fidelity-21" in text             # tagged with the creator's own id
    note_rows = db.list_recent_posts(hours=1)
    assert note_rows
    # Nothing went missing in the delivered copy.
    report = fidelity.compare(deal, text)
    assert not report["missing"], report["missing"]


def test_the_record_says_which_network_removed_which_product(monkeypatch):
    posted = _wired(monkeypatch)
    inf_id = _creator(tag="fidelity-21", allow_hypd=False, allow_earnkaro=False,
                      allow_amazon=True, only_amazon=True)
    db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready", role="broadcast")

    asyncio.run(pipeline.render_and_dispatch(MIXED_SOURCE, influencer_ids=[inf_id]))

    assert posted, "the Amazon product still posts"
    text = posted[0][1]
    assert "BOAT AIRDOPES" in text and "NIRLON" not in text
    rows = db.list_recent_posts(hours=1)
    assert rows and "network merchant" in rows[0]["note"]
    assert "NIRLON" in rows[0]["note"]


def test_a_clean_post_records_no_note(monkeypatch):
    posted = _wired(monkeypatch)
    inf_id = _creator()
    db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready", role="broadcast")

    asyncio.run(pipeline.render_and_dispatch(AMAZON_ONLY_SOURCE, influencer_ids=[inf_id]))
    rows = db.list_recent_posts(hours=1)
    assert posted and rows
    assert rows[0]["note"] == ""


def test_the_delivery_csv_and_selfcheck_surface_the_notes():
    from influencer_hub import ops, selfcheck

    inf_id = _creator()
    channel = db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready",
                             role="broadcast")
    db.record_post(inf_id, channel, "sig", status="posted", deal_text="x",
                   note="network merchant: NIRLON Shirt at ₹399")
    db.record_post(inf_id, channel, "sig2", status="skipped",
                   error="skipped:near_duplicate_12h")

    csv_text = ops.posts_csv(hours=24)
    header, *rows = csv_text.strip().splitlines()
    assert header.endswith("note")
    assert "network merchant: NIRLON" in csv_text
    assert len(rows) == 2 and rows[0].endswith("")

    reasons = db.skip_reasons()
    assert reasons and reasons[0]["status"] == "skipped:near_duplicate_12h"
    item = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "text")
    assert item["state"] == "warn"
    assert "NIRLON" in item["detail"] and "1 duplicate(s) blocked" in item["detail"]


def test_the_selfcheck_is_green_when_nothing_changed():
    from influencer_hub import selfcheck

    inf_id = _creator()
    channel = db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready",
                             role="broadcast")
    db.record_post(inf_id, channel, "sig", status="posted", deal_text="x", note="")
    db.record_post(inf_id, channel, "sig2", status="skipped", error="skipped:content_dedup_24h")

    item = next(c for c in selfcheck.run_checks()["checks"] if c["id"] == "text")
    assert item["state"] == "ok"
    assert "Every post kept its source text" in item["detail"]
    assert "1 duplicate(s) were blocked" in item["detail"]


def test_approval_copy_keeps_the_amazon_product_and_drops_the_other(monkeypatch):
    """The approval preview is Amazon-only: the other product goes with its copy."""
    posted = _wired(monkeypatch)
    inf_id = _creator(tag="fidelity-21")
    approval = db.add_channel(inf_id, "telegram", "@fidelity_approve", status="ready",
                              role="approval")
    main = db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready",
                          role="broadcast")

    asyncio.run(pipeline.render_and_dispatch(MIXED_SOURCE, influencer_ids=[inf_id]))

    text = dict(posted)[("@fidelity_approve")]
    assert "BOAT AIRDOPES" in text and "tag=fidelity-21" in text and "#ad" in text
    assert "NIRLON" not in text and "flipkart" not in text.lower()
    # The main channel posts the Amazon product; the Flipkart one is switched off
    # here, so its block is reported in the delivery note rather than half-posted.
    assert "BOAT AIRDOPES" in dict(posted)[("@fidelity_loots")]
    assert db.list_recent_posts(hours=1)
    assert any("network merchant" in row["note"] for row in db.list_recent_posts(hours=1))
    assert approval and main


def test_promotion_lines_do_not_break_the_post(monkeypatch):
    posted = _wired(monkeypatch)
    inf_id = _creator()
    db.add_channel(inf_id, "telegram", "@fidelity_loots", status="ready", role="broadcast")

    asyncio.run(pipeline.render_and_dispatch(MIXED_SOURCE, influencer_ids=[inf_id]))
    text = posted[0][1]
    # The source channel's own advertisement never reaches the creator's channel…
    assert "LootZoneIndia11" not in text
    # …and the product copy it sat next to does.
    assert "BOAT AIRDOPES 141" in text and "MRP" in text
