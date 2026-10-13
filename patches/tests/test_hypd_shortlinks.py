"""First-party branded redirects preserve existing HYPD attribution."""
from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

import pytest

from influencer_hub import config, db, hypd_shortlinks, link_router, pipeline


HYPD_AFFILIATE_LINKS = (
    "https://hypd.store/93944/afflink/daoli7dtm6mc5h7k1ffg",
    "https://hypd.store/93944/afflink/daol5bac45l0tc0oo5rg",
)


def test_supplied_hypd_links_are_stable_and_redirect_without_losing_attribution(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hypd-shortlinks.sqlite3")
    monkeypatch.setattr(
        config, "MEESHO_SHORT_LINK_BASE_URL", "https://meesho.go.example.test"
    )
    db.init()

    source = "Meesho deals:\n" + "\n".join(HYPD_AFFILIATE_LINKS)
    rendered = hypd_shortlinks.shorten_hypd_links(source)
    short_urls = link_router.find_urls(rendered)
    assert len(short_urls) == 2
    assert all(url.startswith("https://meesho.go.example.test/m/") for url in short_urls)

    code_by_target = {}
    for original, short_url in zip(HYPD_AFFILIATE_LINKS, short_urls):
        code = short_url.rsplit("/", 1)[-1]
        assert re.fullmatch(r"[A-Za-z0-9_-]{8}", code)
        assert db.get_or_create_hypd_short_link(original) == code
        record = db.get_hypd_short_link(code)
        assert record == {"target_url": original, "store_id": "93944"}
        code_by_target[original] = code

    # Codes remain deterministic across restarts and repeated renders.
    assert hypd_shortlinks.shorten_hypd_links(source) == rendered

    from dashboard.app import app

    client = app.test_client()
    for target, code in code_by_target.items():
        response = client.get(f"/m/{code}?next=https://attacker.example")
        assert response.status_code == 302
        assert response.headers["Location"] == target
    assert client.get("/m/not-a-valid-code").status_code == 404
    assert client.get("/m/aaaaaaaa").status_code == 404


def test_pipeline_shortens_rewritten_hypd_links_with_the_configured_domain(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hypd-pipeline.sqlite3")
    monkeypatch.setattr(
        config, "MEESHO_SHORT_LINK_BASE_URL", "https://meesho.go.example.test"
    )
    db.init()
    influencer_id = db.add_influencer(
        "Meesho Branded Link", allow_amazon=False, allow_hypd=True
    )
    channel_id = db.add_channel(
        influencer_id, "telegram", "@meesho_branded_link", status="ready",
        allow_amazon=False, allow_hypd=True,
    )
    sent = []

    async def fake_dispatch(_influencer, _channel, text):
        sent.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    result = asyncio.run(
        pipeline.render_and_dispatch(
            f"Meesho product {HYPD_AFFILIATE_LINKS[0]}",
            influencer_ids=[influencer_id],
        )
    )
    assert result[influencer_id][channel_id] == "posted"
    match = re.search(r"https://meesho\.go\.example\.test/m/([A-Za-z0-9_-]{8})", sent[0])
    assert match
    record = db.get_hypd_short_link(match.group(1))
    assert record == {
        "target_url": HYPD_AFFILIATE_LINKS[0],
        "store_id": "93944",
    }


def test_shortener_requires_owned_https_origin_and_valid_hypd_afflinks(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "hypd-invalid.sqlite3")
    db.init()
    source = f"Deal: {HYPD_AFFILIATE_LINKS[0]}"

    for invalid_base in (
        "http://meesho.example.test",
        "https://meesho.example.test/path",
        "https://user:pass@meesho.example.test",
        "https://meesho.example.test/?redirect=https://attacker.example",
    ):
        assert hypd_shortlinks.shorten_hypd_links(source, invalid_base) == source

    with pytest.raises(ValueError):
        db.get_or_create_hypd_short_link(
            "https://www.meesho.com/product/p/123456"
        )
    with pytest.raises(ValueError):
        db.get_or_create_hypd_short_link(
            "https://hypd.store.evil.example/93944/afflink/fake"
        )


def test_generic_bitly_replacements_do_not_overwrite_hypd_affiliate_links():
    original = HYPD_AFFILIATE_LINKS[0]
    rendered = link_router.render_for_influencer(
        f"Meesho deal {original}",
        amazon_tag="creator-21",
        shortened_links={original: "https://bit.ly/not-our-hypd-domain"},
        clean_promos=False,
    )
    assert original in rendered
    assert "bit.ly/not-our-hypd-domain" not in rendered
