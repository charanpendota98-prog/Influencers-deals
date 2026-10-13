"""Provenance lock for short affiliate links (fktr.in / ekaro.in -> affExtParam2)."""
import asyncio
from unittest.mock import patch

from influencer_hub import earnkaro as ek, config


class _FakeResp:
    def __init__(self, status=200, body="", url="https://fktr.in/x"):
        self.status = status
        self._body = body
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


class _FakeSessionFull:
    """POST returns the EarnKaro JSON; GET (redirect-follow) returns resolved URL."""

    def __init__(self, ek_body, resolved_url):
        self.ek_body = ek_body
        self.resolved_url = resolved_url
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("post", url))
        return _FakeResp(200, self.ek_body)

    def get(self, url, **kw):
        self.calls.append(("get", url))
        return _FakeResp(200, "", self.resolved_url)


def test_resolve_affextparam2():
    out = asyncio.run(ek._resolve_affextparam2(
        _FakeSessionFull("", "https://flipkart.com/p/itm?pid=X&affExtParam2=5478322"),
        "https://fktr.in/xyz"))
    assert out == "5478322"


def test_short_link_accepted_when_pubid_matches():
    ek.CACHE.clear()
    fake = _FakeSessionFull(
        ek_body='{"success": 1, "data": "https://fktr.in/ORooMq5"}',
        resolved_url="https://www.flipkart.com/p/itm?pid=X&affExtParam2=5478322")
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmA"}))
    assert out == {"https://www.flipkart.com/p/itmA": "https://fktr.in/ORooMq5"}


def test_short_link_rejected_when_pubid_mismatch():
    ek.CACHE.clear()
    fake = _FakeSessionFull(
        ek_body='{"success": 1, "data": "https://fktr.in/ORooMq5"}',
        resolved_url="https://www.flipkart.com/p/itm?pid=X&affExtParam2=999")
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmA"}))
    # Mismatch -> refuse the short link, keep the original (no commission leak).
    assert out == {"https://www.flipkart.com/p/itmA": "https://www.flipkart.com/p/itmA"}
