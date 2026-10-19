"""One multi-link deal, three creators, three different network switches.

The operator's rule, in their words: "Amazon on + EarnKaro/HYPD off aithe aa
creator channels ki only Amazon deals vellali; EarnKaro/HYPD/Amazon on aithe
aa posts valla channels ki vellali" — and, for a deal that carries several links
at once, every one of them has to come out monetised the creator's way with the
source text intact.

This module pins the whole matrix on a single deal:

* a creator with everything on receives **every** product, Amazon signed with
  their own tag, Flipkart and Meesho converted through our central accounts,
  LehLah left on its own attribution;
* a creator with only Amazon on receives the Amazon and LehLah products, and the
  switched-off networks' blocks leave **with their copy** (no orphan title);
* a creator with Amazon off (EarnKaro + HYPD on) loses the Amazon block and keeps
  the rest converted;
* in every case the delivered text is the source minus exactly those blocks — and
  the same deal a moment later is skipped, not repeated.
"""
from __future__ import annotations

import asyncio
import pytest

from influencer_hub import config, db, earnkaro, fidelity, link_router, pipeline

DEAL = (
    "🔥 Today's Loot List\n"
    "\n"
    "1) BOAT AIRDOPES 141 at ₹1299 (MRP ₹2999)\n"
    "https://www.amazon.in/dp/B09N3ZNHTY\n"
    "\n"
    "2) NIRLON Checked Shirt at ₹399 · Sizes M, L, XL\n"
    "https://www.flipkart.com/nirlon-checked-shirt/p/itm1234abcd?pid=SHI123\n"
    "\n"
    "3) Meesho Cotton Kurti at ₹249 (Free Delivery)\n"
    "https://www.meesho.com/cotton-kurti/p/abc123\n"
    "\n"
    "4) LehLah Kitchen Combo at ₹499\n"
    "https://lehlah.club/product/kitchen-combo-499\n"
)
FLIPKART = "https://www.flipkart.com/nirlon-checked-shirt/p/itm1234abcd?pid=SHI123"
MEESHO = "https://www.meesho.com/cotton-kurti/p/abc123"
EK_FLIPKART = "https://ekaro.in/our-flipkart-conversion"
EK_MEESHO = "https://ekaro.in/our-meesho-conversion"


@pytest.fixture()
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "network-routing.sqlite3")
    monkeypatch.setattr(config, "MEESHO_EARNKARO_FALLBACK", True)
    db.init()
    # This matrix verifies category isolation and text fidelity, including a
    # raw LehLah example; strict commission behavior is tested separately.
    db.set_global_setting("strict_commission_mode", "off")
    # EarnKaro is the only external call in this path; the CRM answers with our
    # own conversion links, the same shape a real key returns.
    async def fake_convert(urls, include_meesho=False):
        result = {}
        publisher_id = str(config.EARNKARO_PUBLISHER_ID or "").strip()
        for source, short in ((FLIPKART, EK_FLIPKART), (MEESHO, EK_MEESHO)):
            if source in urls:
                db.record_verified_earnkaro_link(short, publisher_id, source)
                result[source] = short
        return result

    monkeypatch.setattr(earnkaro, "convert_links", fake_convert)
    sent: dict[str, str] = {}

    async def fake_dispatch(_inf, channel, text):
        sent[channel["identifier"]] = text
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    return sent


def _creator(name: str, tag: str, handle: str, **switches):
    inf_id = db.add_influencer(name, tag, categories="", posting_schedule="", **switches)
    db.add_channel(inf_id, "telegram", handle, status="ready", role="broadcast")
    return inf_id, handle


def test_the_whole_matrix_on_one_deal(hub):
    everything, everything_handle = _creator(
        "All Networks", "allnets-21", "@allnets_loots",
        allow_amazon=True, allow_earnkaro=True, allow_hypd=True)
    amazon_only, amazon_handle = _creator(
        "Amazon Only", "amzonly-21", "@amzonly_loots",
        allow_amazon=True, allow_earnkaro=False, allow_hypd=False, only_amazon=True)
    no_amazon, no_amazon_handle = _creator(
        "EarnKaro Only", "ekonly-21", "@ekonly_loots",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True)

    result = asyncio.run(pipeline.render_and_dispatch(
        DEAL, influencer_ids=[everything, amazon_only, no_amazon],
        source_channel="LootZoneIndia11"))
    assert set(result[everything].values()) == {"posted"}
    assert set(result[amazon_only].values()) == {"posted"}
    assert set(result[no_amazon].values()) == {"posted"}

    # 1. everything on: all four products, each monetised our way
    all_text = hub[everything_handle]
    assert "tag=allnets-21" in all_text                        # own Amazon tag
    assert EK_FLIPKART in all_text and EK_MEESHO in all_text    # central accounts
    assert "lehlah.club/product/kitchen-combo-499" in all_text  # own attribution
    for product in ("BOAT AIRDOPES", "NIRLON Checked Shirt",
                    "Meesho Cotton Kurti", "LehLah Kitchen Combo"):
        assert product in all_text, product

    # 2. Amazon only: the switched-off networks' blocks left with their copy
    amazon_text = hub[amazon_handle]
    assert "tag=amzonly-21" in amazon_text
    assert "BOAT AIRDOPES" in amazon_text and "LehLah Kitchen Combo" in amazon_text
    for gone in ("NIRLON", "Meesho Cotton Kurti", EK_FLIPKART, EK_MEESHO):
        assert gone not in amazon_text, f"{gone} should have left with its block"

    # 3. Amazon off: the Amazon block is gone, the rest still post
    ek_text = hub[no_amazon_handle]
    assert "BOAT AIRDOPES" not in ek_text and "amazon.in" not in ek_text
    assert "NIRLON" in ek_text and EK_FLIPKART in ek_text and EK_MEESHO in ek_text

    # 4. text fidelity, judged against the source minus exactly those blocks
    for inf_id, handle in ((everything, everything_handle),
                           (amazon_only, amazon_handle),
                           (no_amazon, no_amazon_handle)):
        profile = db.get_influencer(inf_id)
        channel = [c for c in db.list_channels(inf_id)
                   if c["identifier"] == handle][0]
        allowed_kinds, _ek, _only = pipeline._destination_allowed_kinds(
            profile, channel, "broadcast", strip_amz=False)
        expected, dropped = link_router.partition_disallowed_blocks(DEAL, allowed_kinds)
        report = fidelity.compare(expected, hub[handle])
        assert report["ok"] is True, f"{handle}: {report['summary']}"
        assert report["added"] == [], report["added"]
        # and the delivery log says which network left, for the operator
        note = [row for row in db.list_recent_posts(hours=1)
                if row["identifier"] == handle][0]["note"]
        if dropped:
            assert "network" in note, note
        else:
            assert note == "", note


def test_the_same_deal_is_never_delivered_twice(hub):
    inf_id, handle = _creator("Repeat Reader", "repeat-21", "@repeat_loots")
    first = asyncio.run(pipeline.render_and_dispatch(
        DEAL, influencer_ids=[inf_id], source_channel="LootZoneIndia11"))
    second = asyncio.run(pipeline.render_and_dispatch(
        DEAL, influencer_ids=[inf_id], source_channel="LootZoneIndia11"))
    channel_id = db.list_channels(inf_id)[0]["id"]
    assert first[inf_id][channel_id] == "posted"
    assert second[inf_id][channel_id] == "skipped:duplicate_same_product"
    # the skip is visible, not swallowed
    assert db.skip_reasons(hours=1) == [
        {"status": "skipped:duplicate_same_product", "count": 1}]
