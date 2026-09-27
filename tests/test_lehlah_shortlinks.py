"""Validate LehLah attribution preservation and approval-gated short links."""
from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import pytest

from influencer_hub import config, db, earnkaro, lehlah_shortlinks, link_router, pipeline


LEHLAH_MEESHO_LINKS = (
    "https://www.meesho.com/s/p/7amuq5?host_internal=single_product"
    "&pid=lehlahstyruh_int&af_click_lookback=14d&is_retargeting=true"
    "&click_id=3aGT9KS4Qu4ZrQ1kARwjV1X8LDjAYhI5UzJ0a5q9rjo"
    "&product_name=product&af_force_deeplink=true&af_reengagement_window=14d"
    "&c=3aGT9KS4Qu4ZrQ1kARwjV1X8LDjAYhI5UzJ0a5q9rjo&product_id=441125645"
    "&mcn=LEHLAH&af_dp=supply%3A%2F%2Fopen&af_siteid=lehlah"
    "&utm_medium=3aGT9KS4Qu4ZrQ1kARwjV1X8LDjAYhI5UzJ0a5q9rjo"
    "&external_product_id=7amuq5&utm_source=lehlah",
    "https://www.meesho.com/s/p/8w8g03?utm_source=lehlah&pid=lehlahstyruh_int"
    "&click_id=jZqE3z8I0hjbBYEH6FmNCZTdPVgYn8whZv5BIO1Ai0E"
    "&is_retargeting=true&af_dp=supply%3A%2F%2Fopen&product_name=product"
    "&af_reengagement_window=14d"
    "&utm_medium=jZqE3z8I0hjbBYEH6FmNCZTdPVgYn8whZv5BIO1Ai0E"
    "&external_product_id=8w8g03&host_internal=single_product"
    "&product_id=537871107&af_click_lookback=14d&af_siteid=lehlah"
    "&af_force_deeplink=true"
    "&c=jZqE3z8I0hjbBYEH6FmNCZTdPVgYn8whZv5BIO1Ai0E&mcn=LEHLAH",
)


def test_sample_urls_are_lehlah_attribution_not_hypd_or_earnkaro():
    for url in LEHLAH_MEESHO_LINKS:
        assert link_router.classify_url(url) == "lehlah"
        assert link_router.classify_url("https://hypd.store/93944/afflink/token") == "hypd"

    raw_meesho = "https://www.meesho.com/s/p/7amuq5"
    assert link_router.classify_url(raw_meesho) == "merchant"


def test_existing_lehlah_link_is_never_rewritten_by_earnkaro_or_bitly():
    url = LEHLAH_MEESHO_LINKS[0]
    rendered = link_router.render_for_influencer(
        f"Meesho product {url}",
        amazon_tag="creator-21",
        earnkaro_links={url: "https://ekaro.in/incorrect-rewrite"},
        shortened_links={url: "https://bit.ly/incorrect-rewrite"},
        clean_promos=True,
    )
    assert url in rendered
    assert "ekaro.in" not in rendered
    assert "bit.ly" not in rendered


def test_first_party_shortening_preserves_full_lehlah_url_and_redirects(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "lehlah-shortlinks.sqlite3")
    monkeypatch.setattr(
        config, "MEESHO_SHORT_LINK_BASE_URL", "https://go.example.test"
    )
    db.init()

    source = "LehLah Meesho:\n" + "\n".join(LEHLAH_MEESHO_LINKS)
    rendered = lehlah_shortlinks.shorten_lehlah_links(source)
    short_urls = link_router.find_urls(rendered)
    assert len(short_urls) == 2
    assert all(url.startswith("https://go.example.test/l/") for url in short_urls)

    code_targets = {}
    for original, short_url in zip(LEHLAH_MEESHO_LINKS, short_urls):
        code = short_url.rsplit("/", 1)[-1]
        assert re.fullmatch(r"[A-Za-z0-9_-]{8}", code)
        assert db.get_or_create_lehlah_short_link(original) == code
        assert db.get_lehlah_short_link(code) == {"target_url": original}
        code_targets[code] = original

    # Each stored link redirects verbatim; arbitrary query parameters cannot
    # change its destination or remove AppsFlyer attribution.
    from dashboard.app import app

    client = app.test_client()
    for code, original in code_targets.items():
        response = client.get(f"/l/{code}?next=https://attacker.example")
        assert response.status_code == 302
        assert response.headers["Location"] == original
    assert client.get("/l/not-valid").status_code == 404


def test_pipeline_keeps_lehlah_original_until_approved(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "lehlah-pipeline.sqlite3")
    monkeypatch.setattr(
        config, "MEESHO_SHORT_LINK_BASE_URL", "https://go.example.test"
    )
    monkeypatch.setattr(config, "LEHLAH_SHORTLINKS_ENABLED", False)
    monkeypatch.setattr(config, "BITLY_API_KEY", "global-bitly-test-token")
    db.init()
    influencer_id = db.add_influencer("LehLah Meesho Creator", "creator-21")
    channel_id = db.add_channel(
        influencer_id, "telegram", "@lehlah_meesho_creator", status="ready"
    )
    sent = []

    async def fake_dispatch(_influencer, _channel, text):
        sent.append(text)
        return "posted"

    converter = AsyncMock(return_value={})
    bitly_shortener = AsyncMock(return_value={})
    monkeypatch.setattr(earnkaro, "convert_links", converter)
    monkeypatch.setattr(pipeline.bitly_client, "shorten_urls", bitly_shortener)
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)

    result = asyncio.run(
        pipeline.render_and_dispatch(
            f"Meesho product {LEHLAH_MEESHO_LINKS[0]}",
            influencer_ids=[influencer_id],
        )
    )
    assert result[influencer_id][channel_id] == "posted"
    converter.assert_not_awaited()
    bitly_shortener.assert_not_awaited()
    assert LEHLAH_MEESHO_LINKS[0] in sent[0]
    assert "https://go.example.test/l/" not in sent[0]


def test_unconfigured_or_invalid_base_leaves_lehlah_url_unchanged():
    source = f"Meesho: {LEHLAH_MEESHO_LINKS[0]}"
    assert lehlah_shortlinks.shorten_lehlah_links(source, "") == source
    assert (
        lehlah_shortlinks.shorten_lehlah_links(source, "http://go.example.test")
        == source
    )

    with pytest.raises(ValueError):
        db.get_or_create_lehlah_short_link(
            "https://www.meesho.com/s/p/7amuq5?pid=someone_else"
        )
