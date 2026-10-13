import pytest
from influencer_hub.earnkaro import clean_merchant_url_for_api

def test_flipkart_pid_variant_is_preserved_while_stripping_affid():
    url = "https://www.flipkart.com/running-shoes/p/itm123?pid=SHOE1234&affid=competitor&affextparam1=bad"
    cleaned = clean_merchant_url_for_api(url)
    assert "pid=SHOE1234" in cleaned, "Critical product variant pid must be preserved!"
    assert "affid" not in cleaned, "Competitor affid must be stripped!"
    assert "affextparam1" not in cleaned, "Competitor subid must be stripped!"
