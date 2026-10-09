"""Money radar: find and close commission leaks, and never post a free deal."""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import AsyncMock

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, earnkaro, link_router, money_radar, pipeline


@pytest.fixture
def dashboard_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "money-radar.sqlite3")
    db.init()
    db.migrate()
    app.config.update(TESTING=True)
    return app.test_client()


# --------------------------------------------------------------------------
# classify_link — one link's money state
# --------------------------------------------------------------------------

def test_our_amazon_link_earns_and_another_tag_leaks():
    ours = money_radar.classify_link(
        "https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21", "creator-21"
    )
    theirs = money_radar.classify_link(
        "https://www.amazon.in/dp/B0D9P2M1PB?tag=someone-21", "creator-21"
    )
    assert ours["state"] == money_radar.STATE_EARNING
    assert theirs["state"] == money_radar.STATE_LEAK
    assert "someone else" in theirs["reason"]


def test_untagged_amazon_link_leaks():
    entry = money_radar.classify_link(
        "https://www.amazon.in/dp/B0D9P2M1PB", "creator-21"
    )
    assert entry["state"] == money_radar.STATE_LEAK
    assert "without a creator tag" in entry["reason"]


def test_hypd_link_earns_only_on_our_store():
    ours = money_radar.classify_link(
        "https://hypd.store/93944/afflink/abc123", "", "93944"
    )
    theirs = money_radar.classify_link(
        "https://hypd.store/11111/afflink/abc123", "", "93944"
    )
    assert ours["state"] == money_radar.STATE_EARNING
    assert theirs["state"] == money_radar.STATE_LEAK
    assert "another store" in theirs["reason"]


def test_earnkaro_links_are_checked_against_our_publisher_id():
    ours = money_radar.classify_link(
        "https://ekaro.in/abc123?affextparam2=99999", earnkaro_pubid="99999"
    )
    theirs = money_radar.classify_link(
        "https://ekaro.in/abc123?affextparam2=11111", earnkaro_pubid="99999"
    )
    assert ours["state"] == money_radar.STATE_EARNING
    assert theirs["state"] == money_radar.STATE_LEAK
    assert "another publisher" in theirs["reason"]


def test_raw_merchant_and_raw_meesho_links_leak():
    merchant = money_radar.classify_link("https://www.flipkart.com/p/itm123")
    meesho = money_radar.classify_link("https://www.meesho.com/s/p/7amuq5")
    assert merchant["state"] == money_radar.STATE_LEAK
    assert meesho["state"] == money_radar.STATE_LEAK
    assert "earns nothing" in merchant["reason"]
    assert "earns nothing" in meesho["reason"]


def test_lehlah_and_only_stored_first_party_short_links_earn(monkeypatch, tmp_path):
    lehlah = money_radar.classify_link("https://meesho.com/s/p/x?mcn=lehlah")
    assert lehlah["state"] == money_radar.STATE_EARNING
    assert lehlah["kind"] == "lehlah"

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "radar-branded.sqlite3")
    monkeypatch.setattr(config, "AFFILIATE_SHORT_LINK_BASE_URL", "https://hub.example")
    db.init()
    amazon_target = "https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21"
    amazon_code = db.get_or_create_amazon_short_link(amazon_target, "creator-21")
    hypd_target = "https://hypd.store/93944/afflink/abc123"
    hypd_code = db.get_or_create_hypd_short_link(hypd_target)

    amazon_short = money_radar.classify_link(
        f"https://hub.example/a/{amazon_code}?tag=creator-21", "creator-21"
    )
    hypd_short = money_radar.classify_link(
        f"https://hub.example/m/{hypd_code}", "", "93944"
    )
    assert amazon_short["state"] == money_radar.STATE_EARNING
    assert hypd_short["state"] == money_radar.STATE_EARNING

    # A familiar-looking path on a different host must never inflate earnings.
    forged = money_radar.classify_link(
        f"https://attacker.example/a/{amazon_code}?tag=creator-21", "creator-21"
    )
    assert forged["state"] == money_radar.STATE_LEAK
    assert "not counted" in forged["reason"]


def test_generic_short_links_are_never_counted_as_earnings():
    entry = money_radar.classify_link("https://bit.ly/3xY7abc", "creator-21")
    assert entry["state"] == money_radar.STATE_NEUTRAL
    assert "redirect" in entry["reason"]


def test_audit_text_measures_coverage_and_resolves_our_bitly_links():
    text = (
        "Amazon https://www.amazon.in/dp/B0D9P2M1PB?tag=creator-21 "
        "Flipkart https://www.flipkart.com/p/itm123"
    )
    audit = money_radar.audit_text(text, "creator-21", "93944", "99999")
    assert audit["monetisable"] == 2
    assert audit["earning"] == 1
    assert audit["leak"] == 1
    assert audit["coverage_pct"] == 50

    # Our own Bitly links are resolved back to the long URL they stand for.
    shortened = text.replace("https://www.flipkart.com/p/itm123", "https://bit.ly/abc123")
    audit = money_radar.audit_text(
        shortened, "creator-21", "93944", "99999",
        bitly_map={"https://www.flipkart.com/p/itm123": "https://bit.ly/abc123"},
    )
    assert audit["leak"] == 1


# --------------------------------------------------------------------------
# report() — the last N days of posted deals
# --------------------------------------------------------------------------

def test_report_counts_leaks_per_creator_and_reason(dashboard_client):
    influencer_id = db.add_influencer("Money Creator", "money-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@money_creator", status="ready"
    )
    db.record_post(
        influencer_id, channel_id, "sig-earning", status="posted",
        deal_text="Buy https://www.amazon.in/dp/B0D9P2M1PB?tag=money-21",
    )
    db.record_post(
        influencer_id, channel_id, "sig-leaking", status="posted",
        deal_text="Buy https://www.meesho.com/s/p/7amuq5",
    )

    data = money_radar.report(days=7)
    assert data["totals"]["posts"] == 2
    assert data["totals"]["earning"] == 1
    assert data["totals"]["leak"] == 1
    assert data["totals"]["coverage_pct"] == 50
    assert data["totals"]["zero_commission_posts"] == 1
    assert data["creators"][0]["name"] == "Money Creator"
    assert any("Raw Meesho" in reason for reason, _count in data["by_reason"])

    titles = [action["title"] for action in data["suggestions"]]
    assert any("Meesho" in title for title in titles)


def test_report_with_no_posts_reports_full_coverage(dashboard_client):
    data = money_radar.report(days=7)
    assert data["totals"]["posts"] == 0
    assert data["totals"]["coverage_pct"] == 100
    assert data["suggestions"][0]["severity"] == "ok"


# --------------------------------------------------------------------------
# Dashboard: the Money page and its switches
# --------------------------------------------------------------------------

def test_money_page_renders_the_report(dashboard_client):
    influencer_id = db.add_influencer("Page Creator", "page-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@page_creator", status="ready"
    )
    db.record_post(
        influencer_id, channel_id, "sig-1", status="posted",
        deal_text="Buy https://www.flipkart.com/p/itm123",
    )

    response = dashboard_client.get("/money")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Money Radar" in body
    assert "Attribution coverage" in body
    assert "0%" in body


def test_money_page_is_reachable_from_the_dashboard_nav(dashboard_client):
    with dashboard_client.session_transaction() as session:
        session["dashboard_authenticated"] = True
    body = dashboard_client.get("/").get_data(as_text=True)
    assert "/money" in body


def test_money_switch_persists_and_is_read_back_by_the_pipeline(dashboard_client):
    assert pipeline.meesho_earnkaro_fallback_enabled() is True
    assert pipeline.only_earning_deals_enabled() is False

    response = dashboard_client.post(
        "/money/switch",
        data={"setting": "meesho_earnkaro_fallback", "value": "off"},
    )
    assert response.status_code == 302
    assert db.get_global_setting("meesho_earnkaro_fallback") == "off"
    assert pipeline.meesho_earnkaro_fallback_enabled() is False

    dashboard_client.post(
        "/money/switch", data={"setting": "only_earning_deals", "value": "on"}
    )
    assert pipeline.only_earning_deals_enabled() is True


def test_money_switch_rejects_unknown_settings(dashboard_client):
    response = dashboard_client.post(
        "/money/switch", data={"setting": "wipe_the_database", "value": "on"}
    )
    assert response.status_code == 302
    assert not db.get_global_setting("wipe_the_database")


# --------------------------------------------------------------------------
# Pipeline: never spend a post on a deal that pays nothing
# --------------------------------------------------------------------------

def _run_pipeline(monkeypatch, tmp_path, name: str, text: str, tag: str = "money-21",
                  settings: dict | None = None):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / f"{name}.sqlite3")
    db.init()
    for key, value in (settings or {}).items():
        db.set_global_setting(key, value)
    influencer_id = db.add_influencer(
        "Filter Creator", tag, allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", f"@{name}", status="ready",
        allow_amazon=True, allow_earnkaro=True, allow_hypd=True,
    )
    sent = []

    async def fake_dispatch(_influencer, _channel, rendered):
        sent.append(rendered)
        return "posted"

    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    result = asyncio.run(
        pipeline.render_and_dispatch(text, influencer_ids=[influencer_id])
    )
    return result, influencer_id, channel_id, sent


def test_only_earning_deals_skips_a_deal_with_no_earning_link(monkeypatch, tmp_path):
    result, influencer_id, channel_id, sent = _run_pipeline(
        monkeypatch, tmp_path, "money_filter", "Deal https://www.flipkart.com/p/itm123",
        settings={"only_earning_deals": "on"},
    )
    assert result[influencer_id][channel_id] == "skipped:no_commission_link"
    assert sent == []


def test_only_earning_deals_still_posts_when_a_link_earns(monkeypatch, tmp_path):
    result, influencer_id, channel_id, sent = _run_pipeline(
        monkeypatch,
        tmp_path,
        "money_filter_ok",
        "Deal https://www.amazon.in/dp/B0D9P2M1PB?tag=money-21 "
        "and https://www.flipkart.com/p/itm123",
        settings={"only_earning_deals": "on"},
    )
    assert result[influencer_id][channel_id] == "posted"
    assert sent


def test_free_posts_are_still_allowed_by_default(monkeypatch, tmp_path):
    """Volume stays untouched until the operator turns the switch on."""
    result, influencer_id, channel_id, sent = _run_pipeline(
        monkeypatch, tmp_path, "money_default", "Deal https://www.flipkart.com/p/itm123"
    )
    assert result[influencer_id][channel_id] == "posted"
    assert sent


def test_meesho_fallback_offers_the_raw_url_to_earnkaro(monkeypatch, tmp_path):
    """A raw Meesho link is the one deal HYPD cannot monetise."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "money-meesho.sqlite3")
    db.init()
    db.set_global_setting("meesho_earnkaro_fallback", "on")
    influencer_id = db.add_influencer(
        "Meesho Money", "meesho-21", allow_amazon=False,
        allow_earnkaro=True, allow_hypd=True,
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@meesho_money", status="ready",
        allow_amazon=False, allow_earnkaro=True, allow_hypd=True,
    )
    raw_meesho = "https://www.meesho.com/s/p/7amuq5"
    sent = []

    async def fake_dispatch(_influencer, _channel, rendered):
        sent.append(rendered)
        return "posted"

    converter = AsyncMock(return_value={raw_meesho: "https://ekaro.in/meesho123"})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    result = asyncio.run(
        pipeline.render_and_dispatch(
            f"Meesho deal {raw_meesho}", influencer_ids=[influencer_id]
        )
    )

    assert result[influencer_id][channel_id] == "posted"
    assert "https://ekaro.in/meesho123" in sent[0]
    assert raw_meesho not in sent[0]
    for call in converter.await_args_list:
        assert raw_meesho in call.args[0]
        assert call.kwargs["include_meesho"] is True


# --------------------------------------------------------------------------
# Deal quality floor — channel beats creator beats global
# --------------------------------------------------------------------------

def test_suggestions_change_once_the_switch_is_already_on(dashboard_client):
    influencer_id = db.add_influencer("Fixed Creator", "fixed-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@fixed_creator", status="ready"
    )
    db.record_post(
        influencer_id, channel_id, "sig-meesho", status="posted",
        deal_text="Buy https://www.meesho.com/s/p/7amuq5",
    )

    db.set_global_setting("meesho_earnkaro_fallback", "on")
    db.set_global_setting("only_earning_deals", "on")
    titles = [action["title"] for action in money_radar.report(days=7)["suggestions"]]
    assert any("still posted raw" in title for title in titles)
    assert not any("Enable Meesho fallback" in str(titles) for title in titles)
    assert not any("no earning link" in title for title in titles)


def test_creator_and_channel_quality_floors_are_saved(dashboard_client):
    influencer_id = db.add_influencer("Tier Creator", "tier-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@tier_creator", status="ready"
    )

    response = dashboard_client.post(
        f"/influencer/{influencer_id}/update-profile",
        data={"name": "Tier Creator", "amazon_tag": "tier-21", "min_deal_tier": "A"},
    )
    assert response.status_code == 302
    assert db.get_influencer(influencer_id)["min_deal_tier"] == "A"

    response = dashboard_client.post(
        f"/channel/{channel_id}/update",
        data={"identifier": "@tier_creator", "min_deal_tier": "S"},
    )
    assert response.status_code == 302
    assert db.list_channels(influencer_id)[0]["min_deal_tier"] == "S"

    # An unknown tier is rejected rather than silently changing the filter.
    dashboard_client.post(
        f"/channel/{channel_id}/update",
        data={"identifier": "@tier_creator", "min_deal_tier": "ZZZ"},
    )
    assert db.list_channels(influencer_id)[0]["min_deal_tier"] == ""


def _tier_run(monkeypatch, tmp_path, name, *, channel_tier="", creator_tier="",
              global_tier=None):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / f"{name}.sqlite3")
    db.init()
    if global_tier:
        db.set_global_setting("min_deal_tier", global_tier)
    influencer_id = db.add_influencer("Tiered", "tiered-21", min_deal_tier=creator_tier)
    channel_id = db.add_channel(
        influencer_id, "telegram", f"@{name}", status="ready",
        min_deal_tier=channel_tier,
    )
    monkeypatch.setattr(link_router, "is_high_quality_deal", lambda *_a, **_k: False)
    monkeypatch.setattr(
        link_router, "calculate_advanced_loot_score", lambda *_a, **_k: {"tier": "C"}
    )
    monkeypatch.setattr(link_router, "extract_price", lambda *_a, **_k: None)
    monkeypatch.setattr(earnkaro, "convert_links", AsyncMock(return_value={}))
    monkeypatch.setattr(
        pipeline, "dispatch_to_channel",
        lambda *_a, **_k: asyncio.sleep(0, result="posted"),
    )
    result = asyncio.run(
        pipeline.render_and_dispatch(
            "Weak deal https://www.flipkart.com/p/itm123",
            influencer_ids=[influencer_id],
        )
    )
    return result[influencer_id][channel_id]


def test_channel_quality_floor_holds_back_weak_deals(monkeypatch, tmp_path):
    assert _tier_run(
        monkeypatch, tmp_path, "tier_channel", channel_tier="S", creator_tier="C"
    ) == "skipped:quality_tier_C_lt_S"


def test_creator_quality_floor_applies_when_the_channel_inherits(monkeypatch, tmp_path):
    assert _tier_run(
        monkeypatch, tmp_path, "tier_creator", creator_tier="A"
    ) == "skipped:quality_tier_C_lt_A"


def test_global_quality_floor_applies_when_nothing_else_is_set(monkeypatch, tmp_path):
    assert _tier_run(
        monkeypatch, tmp_path, "tier_global", global_tier="B"
    ) == "skipped:quality_tier_C_lt_B"


def test_without_a_quality_floor_every_deal_still_posts(monkeypatch, tmp_path):
    assert _tier_run(monkeypatch, tmp_path, "tier_none") == "posted"
