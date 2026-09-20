import pytest

from influencer_hub import db, link_router as lr


def test_classify_url():
    assert lr.classify_url("https://www.amazon.in/dp/B0ABC123?tag=x-21") == "amazon"
    assert lr.classify_url("https://www.flipkart.com/p/itm") == "merchant"
    assert lr.classify_url("https://example.com/foo") == "other"


def test_apply_amazon_tag_replaces_existing():
    url = "https://www.amazon.in/dp/B0ABC123?tag=mama086-21"
    out = lr.apply_amazon_tag(url, "ravi099-21")
    assert "tag=ravi099-21" in out
    assert "mama086-21" not in out


def test_apply_amazon_tag_adds_when_missing():
    url = "https://www.amazon.in/dp/B0ABC123"
    out = lr.apply_amazon_tag(url, "ravi099-21")
    assert out == "https://www.amazon.in/dp/B0ABC123?tag=ravi099-21"


def test_price_filter_matching():
    deal_cheap = "Deal at ₹89 loot link: https://www.amazon.in/dp/B0123"
    deal_mid = "Portronics Fan Price: ₹399 https://www.amazon.in/dp/B0123"
    deal_expensive = "Sony TV at ₹24,999 https://www.amazon.in/dp/B0123"

    assert lr.matches_price_filter(deal_cheap, max_price=99) is True
    assert lr.matches_price_filter(deal_mid, max_price=99) is False
    assert lr.matches_price_filter(deal_mid, max_price=499) is True
    assert lr.matches_price_filter(deal_expensive, max_price=499) is False


def test_search_influencers_by_phone_and_name():
    iid = db.add_influencer("Suresh Reddy", "suresh099-21", phone_number="919988776655")
    res_phone = db.search_influencers("998877")
    assert any(x["id"] == iid for x in res_phone)
    res_name = db.search_influencers("Suresh")
    assert any(x["id"] == iid for x in res_name)
    db.delete_influencer(iid)


def test_strip_amazon_render():
    text = (
        "🔥 Super Combo Deal\n"
        "Amazon link: https://www.amazon.in/dp/B0H5PTMXV1?tag=old-21\n"
        "Flipkart link: https://www.flipkart.com/p/itm123"
    )
    ek = {"https://www.flipkart.com/p/itm123": "https://ekaro.in/AbC123"}
    rendered = lr.render_for_influencer(text, "mama086-21", ek, strip_amazon=True)
    assert "amazon.in" not in rendered
    assert "https://ekaro.in/AbC123" in rendered
    assert "Flipkart link:" in rendered


def test_delete_influencer_and_channels():
    iid = db.add_influencer("To Delete", "del-21")
    cid = db.add_channel(iid, "telegram", "@del_chan", role="approval")
    assert db.get_channel(iid, "approval") is not None
    db.delete_channel(cid)
    assert db.get_channel(iid, "approval") is None
    db.delete_influencer(iid)
    assert db.get_influencer(iid) is None


def test_compact_amazon_strips_junk():
    url = "https://www.amazon.in/Adidas-Shoes/dp/B0ABCDE1234/ref=sr_1_1?keywords=shoe&qid=1&tag=mama086-21&sr=8-1"
    out = lr.compact_amazon_product_link(url, "ravi099-21")
    assert out == "https://www.amazon.in/dp/B0ABCDE1234?tag=ravi099-21"


def test_render_for_influencer():
    text = (
        "Deal\n"
        "Amazon: https://www.amazon.in/dp/B0A1?tag=mama086-21\n"
        "Flipkart: https://www.flipkart.com/p/itm1\n"
        "Site: https://example.com/x"
    )
    ek = {"https://www.flipkart.com/p/itm1": "https://ekaro.example/abc"}
    out = lr.render_for_influencer(text, "ravi099-21", ek)
    assert "tag=ravi099-21" in out
    assert "https://ekaro.example/abc" in out
    assert "https://www.flipkart.com/p/itm1" not in out
    assert "https://example.com/x" in out  # untouched


def test_render_for_influencer_approval_role():
    # In 'approval' role:
    # 1. Keeps only Amazon links (retagged to influencer tag)
    # 2. Drops non-Amazon merchant links
    # 3. Keeps native amazon.in link (no shorteners)
    # 4. Mandatory disclosure: #ad (paid link) at the bottom
    text = (
        "Portronics Handheld Mini Fan, at Rs.799.\n"
        "https://www.amazon.in/dp/B0H5PTMXV1?tag=old-21\n"
        "Flipkart: https://www.flipkart.com/p/itm1"
    )
    rendered = lr.render_for_influencer(text, "mama086-21", role="approval")
    assert "https://www.amazon.in/dp/B0H5PTMXV1?tag=mama086-21" in rendered
    assert "flipkart.com" not in rendered
    assert "#ad (paid link)" in rendered
    assert "old-21" not in rendered


def test_has_amazon_link():
    assert lr.has_amazon_link("Check https://www.amazon.in/dp/B0123") is True
    assert lr.has_amazon_link("Check https://www.flipkart.com/p/1") is False
    assert lr.has_amazon_link("No links here") is False


def test_deal_signature_stable():
    a = "Sony ₹1999 https://www.amazon.in/dp/B0A1"
    b = "sony  Rs.1999  https://www.amazon.in/dp/B0A1 "
    assert lr.deal_signature(a) == lr.deal_signature(b)
    c = "Sony ₹2999 https://www.amazon.in/dp/B0A1"
    assert lr.deal_signature(a) != lr.deal_signature(c)
