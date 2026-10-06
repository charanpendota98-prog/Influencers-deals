"""End-to-end conversion test with a mocked EarnKaro HTTP API.

Verifies the real contract: POST with Bearer auth +
{"deal": url, "convert_option": "convert_only"}, and that the returned short
link is substituted. A wrong body makes the live API answer HTTP 200 with no
link, which posted unpaid merchant links — that is pinned here. No network
needed.
"""
import asyncio
from unittest.mock import patch

from influencer_hub import earnkaro as ek, config


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResp(200, '{"success": 1, "data": "https://ekaro.in/AbC123"}')

    def get(self, url, **kw):
        return _FakeResp(200, "", "https://www.flipkart.com/p/itm?affExtParam2=5478322")


def test_convert_links_uses_bearer_and_body():
    ek.CACHE.clear()
    fake = _FakeSession()
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itm1"}))
    assert out == {"https://www.flipkart.com/p/itm1": "https://ekaro.in/AbC123"}
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["url"] == config.EARNKARO_API_URL
    assert call["json"] == {
        "deal": "https://www.flipkart.com/p/itm1",
        "convert_option": "convert_only",
    }
    assert call["headers"]["Authorization"].startswith("Bearer ")
    assert call["headers"]["Content-Type"] == "application/json"


def test_convert_links_failure_falls_back():
    ek.CACHE.clear()
    fake = _FakeSession()
    fake.post = lambda *a, **k: _FakeResp(200, '{"success": 0, "data": "x"}')
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itm2"}))
    # Fallback keeps the original link so the deal still posts.
    assert out == {"https://www.flipkart.com/p/itm2": "https://www.flipkart.com/p/itm2"}


def test_convert_links_accepts_a_direct_result_that_carries_our_publisher_as_id():
    """Converted merchant URLs sometimes carry the publisher as ``id=``."""
    ek.CACHE.clear()

    class IdParamSession:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            body = (
                '{"success":1,"data":"https://www.flipkart.com/p/itm1'
                '?pid=X&id=5478322"}'
            )
            return _FakeResp(200, body)

    fake = IdParamSession()
    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmID"}))

    assert out == {
        "https://www.flipkart.com/p/itmID": "https://www.flipkart.com/p/itm1?pid=X&id=5478322"
    }
    assert fake.calls[0]["json"]["convert_option"] == "convert_only"


def test_convert_links_rejects_a_direct_result_for_another_publisher_id():
    """A result carrying somebody else's ``id=`` must fall back to the raw URL."""
    ek.CACHE.clear()

    class WrongIdSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            body = (
                '{"success":1,"data":"https://www.flipkart.com/p/itm1'
                '?pid=X&id=1111111"}'
            )
            return _FakeResp(200, body)

    with patch("influencer_hub.earnkaro.config.EARNKARO_API_KEY", "dummy-jwt-key"), \
         patch("influencer_hub.earnkaro.config.EARNKARO_PUBLISHER_ID", "5478322"), \
         patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=WrongIdSession()):
        out = asyncio.run(ek.convert_links({"https://www.flipkart.com/p/itmWrongID"}))

    assert out == {
        "https://www.flipkart.com/p/itmWrongID": "https://www.flipkart.com/p/itmWrongID"
    }


def test_live_verifier_uses_dashboard_credentials_and_checks_publisher_provenance(monkeypatch):
    ek.CACHE.clear()

    class VerifySession:
        def __init__(self):
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            body = (
                '{"success":1,"data":"https://www.flipkart.com/p/itm1'
                '?affExtParam2=vault-publisher"}'
            )
            return _FakeResp(200, body)

    fake = VerifySession()
    settings = {
        "earnkaro_api_key": "vault-token",
        "earnkaro_publisher_id": "vault-publisher",
    }
    monkeypatch.setattr(ek.db, "get_global_setting", lambda key: settings[key])
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "environment-token")
    monkeypatch.setattr(config, "EARNKARO_PUBLISHER_ID", "environment-publisher")
    with patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=fake):
        result = asyncio.run(ek.verify_earnkaro("https://www.flipkart.com/p/itm1"))

    assert result["ok"] is True
    assert result["publisher_provenance_verified"] is True
    assert result["publisher_id"] == "vault-publisher"
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer vault-token"


def test_live_verifier_rejects_direct_result_for_a_different_publisher(monkeypatch):
    class VerifySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp(
                200,
                '{"success":1,"data":"https://www.flipkart.com/p/itm1'
                '?affExtParam2=wrong-publisher"}',
            )

    settings = {
        "earnkaro_api_key": "vault-token",
        "earnkaro_publisher_id": "vault-publisher",
    }
    monkeypatch.setattr(ek.db, "get_global_setting", lambda key: settings[key])
    with patch("influencer_hub.earnkaro.aiohttp.ClientSession", return_value=VerifySession()):
        result = asyncio.run(ek.verify_earnkaro("https://www.flipkart.com/p/itm1"))

    assert result["ok"] is False
    assert result["converted_link"] is None
    assert "configured publisher" in result["error"]


def test_live_verifier_checks_resolved_short_link_publisher_id(monkeypatch):
    class RedirectResponse(_FakeResp):
        def __init__(self, final_url):
            super().__init__(200, "")
            self.url = final_url

    class VerifySession:
        def __init__(self, resolved_publisher):
            self.resolved_publisher = resolved_publisher

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def post(self, url, json=None, headers=None, timeout=None):
            return _FakeResp(200, '{"success":1,"data":"https://ekaro.in/AbC123"}')

        def get(self, url, **kwargs):
            final_url = (
                "https://www.flipkart.com/p/itm1?affExtParam2="
                + self.resolved_publisher
            )
            return RedirectResponse(final_url)

    settings = {
        "earnkaro_api_key": "vault-token",
        "earnkaro_publisher_id": "vault-publisher",
    }
    monkeypatch.setattr(ek.db, "get_global_setting", lambda key: settings[key])

    with patch(
        "influencer_hub.earnkaro.aiohttp.ClientSession",
        return_value=VerifySession("vault-publisher"),
    ):
        matched = asyncio.run(ek.verify_earnkaro("https://www.flipkart.com/p/itm1"))
    assert matched["ok"] is True
    assert matched["publisher_provenance_verified"] is True

    with patch(
        "influencer_hub.earnkaro.aiohttp.ClientSession",
        return_value=VerifySession("wrong-publisher"),
    ):
        mismatched = asyncio.run(ek.verify_earnkaro("https://www.flipkart.com/p/itm1"))
    assert mismatched["ok"] is False
    assert mismatched["publisher_provenance_verified"] is False


def test_conversion_cache_expires_negative_results_and_has_a_hard_bound(monkeypatch):
    ek.CACHE.clear()
    source = "https://www.flipkart.com/p/itm-negative"
    ek._cache_set("negative", source, now=100.0)
    assert ek._cache_get("negative", source, now=101.0) == source
    assert ek._cache_get(
        "negative", source, now=100.0 + ek.NEGATIVE_CACHE_TTL
    ) is None
    assert "negative" not in ek.CACHE

    monkeypatch.setattr(ek, "MAX_CACHE_ENTRIES", 2)
    ek._cache_set("oldest", "https://ekaro.in/one", now=1.0)
    ek._cache_set("middle", "https://ekaro.in/two", now=2.0)
    ek._cache_set("newest", "https://ekaro.in/three", now=3.0)
    assert len(ek.CACHE) == 2
    assert "oldest" not in ek.CACHE


def test_conversion_cache_is_scoped_to_credentials_and_not_cached_without_them(monkeypatch):
    ek.CACHE.clear()
    source = "https://www.flipkart.com/p/itm-cache"
    settings = {"earnkaro_api_key": None, "earnkaro_publisher_id": None}
    monkeypatch.setattr(ek.db, "get_global_setting", lambda key: settings[key])
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "")
    monkeypatch.setattr(config, "EARNKARO_PUBLISHER_ID", "")

    # Missing credentials should not pin the original URL in the cache.
    assert asyncio.run(ek.convert_one(_FakeSession(), source)) == source
    assert ek.CACHE == {}

    class AccountSession:
        def __init__(self, publisher_id):
            self.publisher_id = publisher_id
            self.calls = []

        def post(self, url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            converted = (
                "https://www.flipkart.com/p/itm-cache?affExtParam2="
                + self.publisher_id
            )
            body = '{"success":1,"data":"' + converted + '"}'
            return _FakeResp(200, body)

    settings.update(earnkaro_api_key="token-one", earnkaro_publisher_id="publisher-one")
    first_session = AccountSession("publisher-one")
    first = asyncio.run(ek.convert_one(first_session, source))
    assert "affExtParam2=publisher-one" in first
    assert len(first_session.calls) == 1

    # A new account must trigger a fresh conversion for the same source URL.
    settings.update(earnkaro_api_key="token-two", earnkaro_publisher_id="publisher-two")
    second_session = AccountSession("publisher-two")
    second = asyncio.run(ek.convert_one(second_session, source))
    assert "affExtParam2=publisher-two" in second
    assert len(second_session.calls) == 1

    # The matching account can reuse its own cached conversion.
    cached_session = AccountSession("publisher-two")
    assert asyncio.run(ek.convert_one(cached_session, source)) == second
    assert cached_session.calls == []
