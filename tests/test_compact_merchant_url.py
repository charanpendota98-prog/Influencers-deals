import pytest
from influencer_hub.link_router import compact_merchant_url

def test_compact_merchant_url_shortens_clumsy_flipkart_link():
    clumsy = "https://www.flipkart.com/running-shoes-men/p/itm123456abcdef?pid=SHOE1234&lid=LSTSHOE&marketplace=FLIPKART&q=shoes&store=osp&srno=s_1_1&otracker=search&affid=bad&affextparam1=xyz"
    compact = compact_merchant_url(clumsy)
    assert "https://www.flipkart.com/running-shoes-men/p/itm123456abcdef?pid=SHOE1234" == compact
    assert "affid=bad" not in compact
    assert "otracker" not in compact

def test_compact_merchant_url_shortens_clumsy_myntra_link():
    clumsy = "https://www.myntra.com/kurtas/anouk/women-printed-kurta/1234567/buy?utm_source=dms&utm_medium=perf&utm_campaign=brand&aff=bad"
    compact = compact_merchant_url(clumsy)
    assert "https://www.myntra.com/anouk/women-printed-kurta/1234567/buy" == compact
    assert "utm_source" not in compact
    assert "aff=bad" not in compact
