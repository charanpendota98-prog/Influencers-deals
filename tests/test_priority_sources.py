import pytest
from influencer_hub import db
from influencer_hub.puller import _source_list, PRIORITY_SOURCE_SPECS

def test_top_three_priority_sources_order():
    db.init()
    db.migrate()
    for spec in PRIORITY_SOURCE_SPECS:
        db.add_source(name="Priority Source", spec=spec, kind="production")

    sources = _source_list(use_dummy=False)
    assert len(sources) >= 3
    # First preference must be Shopsy Loots Official
    assert sources[0] == "https://t.me/+O3j4ghbtJzhjZjJl"
    # Second preference must be Mega Loot Deals
    assert sources[1] == "https://t.me/+8KzU3P58MJ9jN2M1"
    # Third preference must be Meesho Deals Official
    assert sources[2] == "https://t.me/+6LA1ljXGlbNmMjA1"
