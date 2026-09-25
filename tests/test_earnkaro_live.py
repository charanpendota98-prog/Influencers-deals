"""End-to-end conversion test with a mocked EarnKaro HTTP API.

Verifies the real contract: POST with Bearer auth + {"deal": url}, and that the
returned short link is substituted. No network needed.
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
    assert call["json"] == {"deal": "https://www.flipkart.com/p/itm1"}
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
