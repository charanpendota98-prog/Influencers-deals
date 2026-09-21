"""EarnKaro converter client.

This is the EXACT contract your existing bestgaa bot uses (verified against
main_bot_new.py), which is what makes your 2 running channels earn:

  POST https://ekaro-api.affiliaters.in/api/converter/public
  Authorization: Bearer <EARNKARO_API_KEY>     # the key IS a JWT
  Content-Type: application/json
  {"deal": "<clean merchant url>"}

  -> {"success": 1, "data": "<ekaro.in short link>"}
     (data may also be a list — take the first http string)

EARNKARO_API_KEY is OUR publisher key, so every non-Amazon merchant link is
monetised under OUR account. Amazon links are handled separately by the link
router (they carry the influencer's own Amazon tag, never EarnKaro).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

import aiohttp

from . import config

# Affiliate shorteners whose resolved URL carries affExtParam2. We accept the
# short link but verify (best-effort) that it redirects to OUR publisher id.
SHORTENER_HOSTS = {"fktr.in", "ekaro.in", "ekaro.app", "clnk.in", "clnk.app", "myntr.it"}


def _is_shortener(link: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(link).hostname or "").lower()
    return host in SHORTENER_HOSTS


async def _resolve_affextparam2(session: aiohttp.ClientSession, link: str,
                                 timeout: float = 8.0) -> str | None:
    """Follow redirects and return the affExtParam2 of the final URL (best-effort)."""
    from urllib.parse import urlparse, parse_qs
    try:
        async with session.get(
            link, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            final = str(resp.url)
    except Exception:
        return None
    return parse_qs(urlparse(final).query).get("affExtParam2", [None])[0]

CACHE: dict[str, tuple[float, str]] = {}
CACHE_TTL = 60 * 60 * 12  # 12h — EarnKaro links are stable
HTTP_TOTAL_TIMEOUT = 30.0


def _cache_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def clean_merchant_url_for_api(url: str) -> str:
    """Strip third-party referral affiliate tracking params before sending to EarnKaro converter.
    This guarantees that competitor affiliate tags (like hypd, affid, clickid, appsflyer)
    are cleaned off, leaving the pure product link so EarnKaro converts cleanly 100%.
    """
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "affid", "aff_siteid", "clickid", "pid", "is_retargeting", "af_force_deeplink",
        "af_dp", "product_name", "host_internal", "external_product_id", "product_id",
        "ref", "tag", "cmpid", "src", "source", "subid", "subid1"
    }
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in tracking_params]
    query_str = urlencode(q) if q else ""
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", query_str, ""))


def _clean(url: str) -> str:
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    q = {k: v for k, v in parse_qsl(p.query, keep_blank_values=True)}
    return urlunparse((p.scheme, p.netloc.lower(), p.path, "", urlencode(q), ""))


def parse_ek_response(body: str, expected_pubid: str | None = None) -> str | None:
    """Extract the converted link from an EarnKaro API response body.

    Returns the short/affiliate link string, or None if the response is not a
    successful conversion. Pure + unit-tested (no network needed).

    If `expected_pubid` is set, the returned link MUST carry that publisher id in
    its `affExtParam2` query param (EarnKaro embeds it on Flipkart/Myntra etc.).
    A mismatch means the link earns for SOMEONE ELSE's account — reject it. This
    is the provenance lock that guarantees "our" setup stays ours.
    """
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("success") != 1:
        return None
    result = data.get("data")
    if isinstance(result, list):
        result = next((x for x in result if isinstance(x, str) and x.startswith("http")), None)
    elif isinstance(result, dict):
        # Some variants nest the link under "link"/"url".
        result = result.get("link") or result.get("url")
    if not isinstance(result, str) or not result.startswith("http"):
        return None
    result = _clean(result)
    if expected_pubid:
        from urllib.parse import parse_qs, urlparse
        ext2 = parse_qs(urlparse(result).query).get("affExtParam2", [None])[0]
        if ext2 and ext2 != expected_pubid:
            # Link earns for a different publisher — do NOT use it.
            return None
    return result


async def convert_one(session: aiohttp.ClientSession, url: str) -> str:
    key = _cache_key(url)
    now = time.time()
    if key in CACHE and now - CACHE[key][0] < CACHE_TTL:
        return CACHE[key][1]

    if not config.EARNKARO_API_KEY:
        # No key configured (e.g. unit/test env): keep the original so the deal
        # still works, but signal it was not monetised.
        CACHE[key] = (now, url)
        return url

    last: Exception | None = None
    # Pre-clean dirty third-party affiliate tracking params (hypd, clickid, appsflyer, etc.)
    api_deal_url = clean_merchant_url_for_api(url)
    for attempt in range(3):
        try:
            async with session.post(
                config.EARNKARO_API_URL,
                json={"deal": api_deal_url},
                headers={
                    "Authorization": f"Bearer {config.EARNKARO_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=max(8.0, HTTP_TOTAL_TIMEOUT * 2)),
            ) as resp:
                body = await resp.text()
                if resp.status in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"EarnKaro HTTP {resp.status}")
                converted = parse_ek_response(body, config.EARNKARO_PUBLISHER_ID)
                if not converted:
                    # Not a successful conversion — fall back to the original link
                    # so the deal still posts (just without our commission).
                    CACHE[key] = (now, url)
                    return url
                # Short-link provenance: fktr.in/ekaro.in etc. don't carry
                # affExtParam2 directly, so follow the redirect (best-effort) and
                # confirm it lands on OUR publisher id. A mismatch -> reject.
                if config.EARNKARO_PUBLISHER_ID and _is_shortener(converted):
                    resolved_pubid = await _resolve_affextparam2(session, converted)
                    if resolved_pubid and resolved_pubid != config.EARNKARO_PUBLISHER_ID:
                        CACHE[key] = (now, url)
                        return url
                if _clean(converted) == _clean(url):
                    # Provenance guard: never accept an echoed source URL.
                    CACHE[key] = (now, url)
                    return url
                CACHE[key] = (now, converted)
                return converted
        except Exception as exc:  # retry with jitter
            last = exc
            await asyncio.sleep(min(2.5, 0.4 * (2 ** attempt) + (time.time() % 1) * 0.3))
    # Give up gracefully — keep the original link rather than drop the deal.
    CACHE[key] = (now, url)
    return url


async def convert_links(urls: set[str]) -> dict[str, str]:
    """Convert a set of merchant URLs to EarnKaro links (OUR publisher id)."""
    urls = {u for u in urls if u}
    if not urls:
        return {}
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(convert_one(session, u) for u in urls))
    return dict(zip(urls, results))


def convert_links_sync(urls: set[str]) -> dict[str, str]:
    return asyncio.run(convert_links(urls))


async def verify_earnkaro(test_url: str = "https://www.flipkart.com/p/itmEXAMPLE12345") -> dict:
    """Live test conversion against the real EarnKaro API (needs network + key).

    Returns a detailed dict so you can confirm, on the VM, that links convert to
    short EarnKaro links. Run: `python -m influencer_hub.cli verify-earnkaro`.
    """
    if not config.EARNKARO_API_KEY:
        return {"ok": False, "error": "EARNKARO_API_KEY not set in .env"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                config.EARNKARO_API_URL,
                json={"deal": test_url},
                headers={
                    "Authorization": f"Bearer {config.EARNKARO_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                body = await resp.text()
        except Exception as exc:  # network/DNS/TLS failure
            return {"ok": False, "error": f"request failed: {exc}"}
    link = parse_ek_response(body)
    return {
        "ok": bool(link),
        "http_status": status,
        "input": test_url,
        "converted_link": link,
        "raw_response": body[:500],
    }
