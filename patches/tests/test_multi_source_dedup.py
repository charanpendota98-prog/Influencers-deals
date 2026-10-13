import pytest
from influencer_hub import link_router, db, pipeline

def test_identical_product_across_different_sources_dedup():
    """Verify that when 2 different telegram channels post the same product
    with different wording, different emojis, and different affiliate links,
    the pipeline generates the EXACT same signature and skips duplicate posting.
    """
    # 1. Meesho Deal from Channel A (e.g. Meesho Loots) vs Channel B (e.g. Shopsy Offers)
    post_a = (
        "🔥 Super Loot on Meesho!\n"
        "Women's Printed Cotton Kurti at flat ₹199 only!\n"
        "Direct Link: https://hypd.store/88888/afflink/daol5bac45l0tc0oo5rg?source=channel_a\n"
        "Order now before stock ends!"
    )
    post_b = (
        "⚡ Mega Flash Deal (Meesho) ⚡\n"
        "Cotton Kurti @ ₹199 (Huge discount)\n"
        "Buy: https://hypd.store/99999/afflink/daol5bac45l0tc0oo5rg?utm_source=channel_b&aff=random\n"
        "Hurry guys!"
    )
    sig_a = link_router.deal_signature(post_a)
    sig_b = link_router.deal_signature(post_b)
    assert sig_a == sig_b, f"Meesho signatures must match across channels: {sig_a} vs {sig_b}"

    # 2. Flipkart Deal from Channel X vs Channel Y
    fk_x = (
        "🏷️ Flipkart Steal Deal!\n"
        "Running Shoes at ₹499\n"
        "Link: https://www.flipkart.com/running-shoes/p/itm123456abcdef?pid=SHOE123&affid=firstchan"
    )
    fk_y = (
        "🔥 Flat 70% Off on Flipkart Shoes!\n"
        "Buy at ₹499: https://www.flipkart.com/running-shoes/p/itm123456abcdef?pid=SHOE123&affid=secondchan"
    )
    assert link_router.deal_signature(fk_x) == link_router.deal_signature(fk_y)

    # 3. Amazon Deal with different tags
    amz_1 = "Amazon Boat Headset at ₹999: https://www.amazon.in/dp/B08XYZ9876?tag=channel1-21"
    amz_2 = "Boat Headphones @ ₹999 loot: https://www.amazon.in/dp/B08XYZ9876?tag=channel2-21&ref=test"
    assert link_router.deal_signature(amz_1) == link_router.deal_signature(amz_2)

    # 4. Myntra Deal with different utm
    myn_1 = "Myntra Saree: https://www.myntra.com/saree/brand/9876543/buy?utm=chan1"
    myn_2 = "Myntra Saree Loot: https://www.myntra.com/saree/brand/9876543/buy?utm=chan2"
    assert link_router.deal_signature(myn_1) == link_router.deal_signature(myn_2)
