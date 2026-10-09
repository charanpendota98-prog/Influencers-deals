"""End-to-end proof for compact first-party affiliate links.

The code is intentionally adversarial here: a URL merely *shaped* like our
``/a/``, ``/m/`` or ``/l/`` route is never enough.  It must be on the configured
HTTPS origin and resolve through a durable record carrying the selected account
attribution.
"""
from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock

from dashboard.app import app
from influencer_hub import (
    amazon_shortlinks,
    commission_guard,
    config,
    db,
    hypd_shortlinks,
    lehlah_shortlinks,
    pipeline,
)


TAG = "creator-link-21"
STORE = "93944"
BASE = "https://go.example.test"
AMAZON_TARGET = f"https://www.amazon.in/dp/B0D9P2M1PB?th=1&tag={TAG}"
HYPD_TARGET = f"https://hypd.store/{STORE}/afflink/abc123TOKEN"


def _configure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "branded-links.sqlite3")
    monkeypatch.setattr(config, "AFFILIATE_SHORT_LINK_BASE_URL", BASE)
    monkeypatch.setattr(config, "AMAZON_SHORT_LINK_BASE_URL", "")
    monkeypatch.setattr(config, "MEESHO_SHORT_LINK_BASE_URL", "")
    db.init()


def test_one_trusted_base_compacts_amazon_and_hypd_then_keeps_their_accounts(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    influencer_id = db.add_influencer("Branded Creator", TAG)
    channel_id = db.add_channel(
        influencer_id, "telegram", "@branded_creator", status="ready"
    )
    sent: list[str] = []

    async def fake_dispatch(_influencer, _channel, text):
        sent.append(text)
        return "posted"

    monkeypatch.setattr(pipeline, "dispatch_to_channel", fake_dispatch)
    monkeypatch.setattr(pipeline, "_earnkaro_map_for", AsyncMock(return_value={}))
    result = asyncio.run(pipeline.render_and_dispatch(
        f"Amazon {AMAZON_TARGET}\nHYPD {HYPD_TARGET}",
        influencer_ids=[influencer_id],
    ))

    assert result[influencer_id][channel_id] == "posted"
    amazon_match = re.search(rf"{re.escape(BASE)}/a/([A-Za-z0-9_-]{{8}})\?tag={TAG}", sent[0])
    hypd_match = re.search(rf"{re.escape(BASE)}/m/([A-Za-z0-9_-]{{8}})", sent[0])
    assert amazon_match and hypd_match
    assert db.get_amazon_short_link(amazon_match.group(1))["target_url"] == AMAZON_TARGET
    assert db.get_hypd_short_link(hypd_match.group(1))["target_url"] == HYPD_TARGET
    assert commission_guard.our_affiliate_urls(sent[0], TAG, STORE) == [
        amazon_match.group(0), hypd_match.group(0)
    ]


def test_amazon_compact_redirect_is_tag_bound_and_legacy_path_stays_live(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    compact = amazon_shortlinks.shorten_amazon_links(AMAZON_TARGET)
    match = re.fullmatch(rf"{re.escape(BASE)}/a/([A-Za-z0-9_-]{{8}})\?tag={TAG}", compact)
    assert match
    code = match.group(1)
    assert amazon_shortlinks.is_our_amazon_short_url(compact, TAG)

    client = app.test_client()
    response = client.get(f"/a/{code}?tag={TAG}")
    assert response.status_code == 302
    assert response.headers["Location"] == AMAZON_TARGET
    # Old posts must never break just because new links use the shorter /a path.
    assert client.get(f"/amazon/{code}?tag={TAG}").status_code == 302
    assert client.get(f"/a/{code}?tag=someone-else-21").status_code == 404
    # The stored target is fixed: an arbitrary query never changes where it goes.
    response = client.get(f"/a/{code}?tag={TAG}&next=https://attacker.example")
    assert response.status_code == 302
    assert response.headers["Location"] == AMAZON_TARGET


def test_guard_and_money_proof_refuse_a_forged_or_unknown_compact_url(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    compact = amazon_shortlinks.shorten_amazon_links(AMAZON_TARGET)
    code = re.search(r"/a/([A-Za-z0-9_-]{8})", compact).group(1)
    forged = f"https://attacker.example/a/{code}?tag={TAG}"
    stale = f"{BASE}/a/aaaaaaaa?tag={TAG}"

    for candidate in (forged, stale):
        audit = commission_guard.audit_rendered_text(candidate, TAG, STORE)
        assert audit["ok"] is False
        assert "not OUR" in audit["details"][0]["reason"]
        assert commission_guard.our_affiliate_urls(candidate, TAG, STORE) == []
        sanitized, _ = commission_guard.sanitize_rendered_text(candidate, TAG, STORE)
        assert candidate not in sanitized

    # Exact base + stored code + exact tag is the only accepting combination.
    assert commission_guard.is_verified_our_link(compact, TAG, STORE)
    assert not amazon_shortlinks.is_our_amazon_short_url(
        f"{BASE}/a/{code}?tag={TAG}&extra=1", TAG
    )


def test_shortener_rejects_unsafe_base_and_noncanonical_amazon_targets(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    source = f"Deal {AMAZON_TARGET}"
    for unsafe in (
        "http://go.example.test",
        "https://go.example.test:8443",
        "https://user:pass@go.example.test",
        "https://go.example.test/path",
        "https://go.example.test/?next=https://attacker.example",
    ):
        assert amazon_shortlinks.shorten_amazon_links(source, unsafe) == source

    for unsafe_target in (
        f"https://www.amazon.in/dp/B0D9P2M1PB?tag={TAG}&redirect=https://attacker.example",
        f"https://www.amazon.in/dp/B0D9P2M1PB?tag={TAG}&tag=another-21",
        f"https://www.amazon.in/gp/product/B0D9P2M1PB?tag={TAG}",
    ):
        assert not amazon_shortlinks.is_valid_amazon_target(unsafe_target, TAG)
        try:
            db.get_or_create_amazon_short_link(unsafe_target, TAG)
        except ValueError:
            pass
        else:  # pragma: no cover - explicit failure message is clearer than pytest.raises here
            raise AssertionError(f"unsafe target was stored: {unsafe_target}")


def test_save_proof_and_preview_return_the_real_clickable_compact_url(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    proof = commission_guard.amazon_tag_proof(TAG)
    assert proof["shortened"] is True
    assert proof["ok"] is True
    assert proof["posted_link"].startswith(f"{BASE}/a/")
    assert proof["redirect_target"] == f"https://www.amazon.in/dp/B0D9P2M1PB?tag={TAG}"

    monkeypatch.setitem(app.config, "TESTING", True)
    client = app.test_client()
    response = client.post("/api/test-render-deal", data={
        "sample_text": f"Deal {AMAZON_TARGET}",
        "amazon_tag": TAG,
        "allow_amazon": "1",
        "allow_earnkaro": "0",
        "allow_hypd": "0",
        "role": "broadcast",
    })
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["verdict"] == "our_link_present"
    assert payload["short_links"]["active_for_this_preview"] is True
    assert payload["our_links"] and payload["our_links"][0].startswith(f"{BASE}/a/")

    # Operators get an explicit setup-state explanation instead of silently
    # discovering long links only after a deal goes out.
    setup_page = client.get("/setup").get_data(as_text=True)
    assert "Branded affiliate short links" in setup_page
    assert BASE in setup_page
    assert "only trusted origin" in setup_page

    routing = client.get(
        f"/api/routing-preview?amazon_tag={TAG}&allow_amazon=1"
        "&allow_earnkaro=0&allow_hypd=0"
    ).get_json()["preview"]
    assert routing["short_links"]["active_for_this_channel"] is True
    assert "/a/<code>?tag=" in next(
        row["note"] for row in routing["rows"] if row["kind"] == "amazon"
    )


def test_hypd_and_lehlah_validators_need_a_stored_target_on_the_trusted_origin(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    hypd = hypd_shortlinks.shorten_hypd_links(HYPD_TARGET)
    assert hypd_shortlinks.is_our_hypd_short_url(hypd, STORE)
    assert not hypd_shortlinks.is_our_hypd_short_url(hypd.replace(BASE, "https://attacker.example"), STORE)

    lehlah_target = "https://www.meesho.com/s/p/7amuq5?pid=lehlah-token&mcn=LEHLAH"
    lehlah = lehlah_shortlinks.shorten_lehlah_links(lehlah_target)
    assert lehlah_shortlinks.is_our_lehlah_short_url(lehlah)
    assert not lehlah_shortlinks.is_our_lehlah_short_url(
        lehlah.replace(BASE, "https://attacker.example")
    )
