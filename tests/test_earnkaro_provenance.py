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
    assert ek.db.verified_earnkaro_link("https://fktr.in/ORooMq5", "5478322")


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


def test_short_link_is_rejected_when_redirect_cannot_be_resolved():
    ek.CACHE.clear()

    class UnresolvableSession(_FakeSessionFull):
        def get(self, url, **kw):
            self.calls.append(("get", url))
            raise OSError("redirect lookup unavailable")

    fake = UnresolvableSession(
        ek_body='{"success":1,"data":"https://fktr.in/ORooMq5"}',
        resolved_url="",
    )
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmA"}))
    assert out == {"https://www.flipkart.com/p/itmA": "https://www.flipkart.com/p/itmA"}
    assert ek.db.verified_earnkaro_link("https://fktr.in/ORooMq5", "5478322") is None


def test_short_link_is_rejected_when_redirect_lands_on_unknown_host():
    ek.CACHE.clear()
    fake = _FakeSessionFull(
        ek_body='{"success":1,"data":"https://fktr.in/ORooMq5"}',
        resolved_url="https://attacker.example/p/1?affExtParam2=5478322",
    )
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmA"}))
    assert out == {"https://www.flipkart.com/p/itmA": "https://www.flipkart.com/p/itmA"}


def test_conversion_requires_a_publisher_id_even_when_api_key_exists(monkeypatch):
    ek.CACHE.clear()
    monkeypatch.setattr(ek.db, "get_global_setting", lambda _key: None)
    monkeypatch.setattr(ek.config, "EARNKARO_API_KEY", "dummy-jwt-key")
    monkeypatch.setattr(ek.config, "EARNKARO_PUBLISHER_ID", "")

    class NeverCalledSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            raise AssertionError("provider called without publisher provenance")

    monkeypatch.setattr(ek.aiohttp, "ClientSession", lambda: NeverCalledSession())
    source = "https://www.flipkart.com/p/itmA"
    assert asyncio.run(ek.convert_links({source})) == {source: source}


def test_meesho_fallback_short_is_verified_only_for_the_meesho_destination():
    ek.CACHE.clear()
    source = "https://www.meesho.com/s/p/itemA"
    short = "https://ekaro.in/meesho123"
    fake = _FakeSessionFull(
        ek_body='{"success":1,"data":"' + short + '"}',
        resolved_url="https://www.meesho.com/s/p/itemA?affExtParam2=5478322",
    )
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        converted = asyncio.run(ek.convert_links({source}, include_meesho=True))
    assert converted == {source: short}
    assert ek.db.verified_earnkaro_link(short, "5478322")


def test_meesho_fallback_rejects_a_non_meesho_destination():
    ek.CACHE.clear()
    source = "https://www.meesho.com/s/p/itemB"
    short = "https://ekaro.in/not-meesho"
    fake = _FakeSessionFull(
        ek_body='{"success":1,"data":"' + short + '"}',
        resolved_url="https://www.flipkart.com/p/itm1?affExtParam2=5478322",
    )
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        converted = asyncio.run(ek.convert_links({source}, include_meesho=True))
    assert converted == {source: source}
    assert ek.db.verified_earnkaro_link(short, "5478322") is None
