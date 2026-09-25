import pytest
from influencer_hub import link_router

def test_url_with_trailing_punctuation_replaces_cleanly():
    text = "🔥 Grab Boat Earbuds at Rs.699: https://www.amazon.in/dp/B08XYZ1234?tag=old-21, before sale ends!"
    rendered = link_router.render_for_influencer(text, amazon_tag="mypartner-21", earnkaro_links={})
    assert "https://www.amazon.in/dp/B08XYZ1234?tag=mypartner-21," in rendered
    assert "old-21" not in rendered

def test_find_urls_strips_trailing_dots_and_commas():
    text = "Check (https://www.flipkart.com/shoes/p/itm123). Also https://amzn.to/3XYZ! And https://hypd.store/93944/afflink/abc?"
    urls = link_router.find_urls(text)
    assert "https://www.flipkart.com/shoes/p/itm123" in urls
    assert "https://amzn.to/3XYZ" in urls
    assert "https://hypd.store/93944/afflink/abc" in urls
