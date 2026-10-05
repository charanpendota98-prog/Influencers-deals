"""EarnKaro converter client.

The configured integration posts a cleaned merchant URL to the EarnKaro
converter endpoint with a Bearer credential and parses a returned affiliate
link. When a publisher ID is configured, direct results must include a matching
ID; known shorteners receive a best-effort redirect check. If credentials are
absent, conversion fails, or the result cannot be validated, the original
merchant URL is returned so the deal is not dropped. This code cannot guarantee
external merchant support or commission attribution.

Amazon URLs are routed separately by ``link_router`` with the selected
influencer/channel Associate tag.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

import aiohttp

from . import config, db, link_router

# Affiliate shorteners whose resolved URL carries affExtParam2. We accept the
# short link but verify (best-effort) that it redirects to OUR publisher id.
SHORTENER_HOSTS = {"fktr.in", "ekaro.in", "ekaro.app", "clnk.in", "clnk.app", "myntr.it"}


def _is_shortener(link: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(link).hostname or "").lower()
    return host in SHORTENER_HOSTS or (
        host.startswith("www.") and host[4:] in SHORTENER_HOSTS
    )


def _affextparam2_values(link: str) -> list[str]:
    """Extract EarnKaro publisher IDs without assuming query-key casing."""
    from urllib.parse import parse_qsl, urlparse
    return [
        value for key, value in parse_qsl(urlparse(link).query, keep_blank_values=True)
        if key.casefold() == "affextparam2"
    ]


async def _resolve_affextparam2(session: aiohttp.ClientSession, link: str,
                                 timeout: float = 8.0) -> str | None:
    """Follow redirects and return the affExtParam2 of the final URL (best-effort)."""
    try:
        async with session.get(
            link, allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as resp:
            final = str(resp.url)
    except Exception:
        return None
    return next(iter(_affextparam2_values(final)), None)

CACHE: dict[str, tuple[float, str]] = {}
CACHE_TTL = 60 * 60 * 12  # 12h — successful EarnKaro links are stable
NEGATIVE_CACHE_TTL = 5 * 60  # retry unsupported/transient fallbacks soon
MAX_CACHE_ENTRIES = 10_000
HTTP_TOTAL_TIMEOUT = 30.0


def _cache_key(url: str, publisher_id: str = "", api_key: str = "") -> str:
    """Scope cached conversions to both the source URL and affiliate account.

    Only a digest is retained; the API key itself is never stored in the cache
    key or logs. Including credentials prevents a URL converted for an old
    publisher from being reused after the vault's account settings change.
    """
    key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    material = f"{publisher_id.strip()}\0{key_fingerprint}\0{url}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _cache_get(key: str, source_url: str, now: float) -> str | None:
    entry = CACHE.get(key)
    if entry is None:
        return None
    created_at, value = entry
    ttl = CACHE_TTL if value != source_url else NEGATIVE_CACHE_TTL
    if now - created_at >= ttl:
        CACHE.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: str, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    if key not in CACHE and len(CACHE) >= MAX_CACHE_ENTRIES:
        # Opportunistically sweep expired items, then evict the oldest entries
        # if a high-volume worker still reaches the hard cache bound.
        for cached_key, (created_at, cached_value) in list(CACHE.items()):
            ttl = CACHE_TTL
            if timestamp - created_at >= ttl:
                CACHE.pop(cached_key, None)
        while len(CACHE) >= MAX_CACHE_ENTRIES:
            oldest_key = min(CACHE, key=lambda cached_key: CACHE[cached_key][0])
            CACHE.pop(oldest_key, None)
    CACHE[key] = (timestamp, value)


def clean_merchant_url_for_api(url: str) -> str:
    """Remove known third-party referral parameters before sending a URL to EarnKaro.

    This produces a cleaner product URL; it cannot guarantee that the remote API
    supports the merchant or will return a successful affiliate conversion.
    """
    from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
    p = urlparse(url)
    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "affid", "aff_siteid", "affextparam1", "affextparam2", "clickid",
        "is_retargeting", "af_force_deeplink", "af_dp", "product_name", "host_internal",
        "external_product_id", "product_id", "ref", "tag", "cmpid", "mcn", "src",
        "source", "subid", "subid1"
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

    If `expected_pubid` is set, direct merchant URLs must carry the matching
    `affExtParam2`. Known shortener links are accepted here and checked against
    their resolved destination in `convert_one`; mismatches are rejected.
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
    if not isinstance(result, str):
        return None
    from urllib.parse import urlparse
    parsed = urlparse(result)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    result = _clean(result)
    if expected_pubid:
        publisher_ids = _affextparam2_values(result)
        if publisher_ids:
            if any(publisher_id != expected_pubid for publisher_id in publisher_ids):
                # Link explicitly carries a different publisher ID — reject it.
                return None
        elif not _is_shortener(result):
            # A direct merchant URL without the provenance parameter cannot be
            # verified. Shorteners are checked after following their redirect.
            return None
    return result


async def convert_one(session: aiohttp.ClientSession, url: str,
                      include_meesho: bool = False) -> str:
    # Amazon has its own Associates tag, so it never goes to EarnKaro. Raw
    # Meesho normally belongs to HYPD, but HYPD cannot mint an affiliate link
    # from a raw product URL — when the operator enables the fallback we try
    # EarnKaro for it instead of posting a link that earns nothing.
    kind = link_router.classify_url(url)
    if kind == "meesho" and not include_meesho:
        return url
    if kind not in {"merchant", "meesho"}:
        return url

    # Dynamically check global settings from DB first (supports secret admin dashboard update)
    # falling back to config.py / .env. Read these before looking in the cache so
    # changing credentials cannot reuse a conversion from the previous account.
    db_ek_key = db.get_global_setting("earnkaro_api_key")
    db_ek_pubid = db.get_global_setting("earnkaro_publisher_id")
    effective_ek_key = db_ek_key if db_ek_key else config.EARNKARO_API_KEY
    effective_ek_pubid = db_ek_pubid if db_ek_pubid else config.EARNKARO_PUBLISHER_ID

    if not effective_ek_key:
        # Keep the source link, but don't cache this fallback: the operator may
        # configure credentials in the dashboard before the next dispatch.
        return url

    key = _cache_key(url, effective_ek_pubid, effective_ek_key)
    now = time.time()
    cached = _cache_get(key, url, now)
    if cached is not None:
        return cached

    last: Exception | None = None
    # Pre-clean dirty third-party affiliate tracking params (hypd, clickid, appsflyer, etc.)
    api_deal_url = clean_merchant_url_for_api(url)
    for attempt in range(3):
        try:
            async with session.post(
                config.EARNKARO_API_URL,
                json={"deal": api_deal_url},
                headers={
                    "Authorization": f"Bearer {effective_ek_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=max(8.0, HTTP_TOTAL_TIMEOUT * 2)),
            ) as resp:
                body = await resp.text()
                if resp.status in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"EarnKaro HTTP {resp.status}")
                converted = parse_ek_response(body, effective_ek_pubid)
                if not converted:
                    # Not a successful conversion — fall back to the original link
                    # so the deal still posts without an affiliate conversion.
                    _cache_set(key, url)
                    return url
                # Short-link provenance: fktr.in/ekaro.in etc. don't carry
                # affExtParam2 directly, so follow the redirect (best-effort) and
                # confirm it lands on OUR publisher id. A mismatch -> reject.
                if effective_ek_pubid and _is_shortener(converted):
                    resolved_pubid = await _resolve_affextparam2(session, converted)
                    if resolved_pubid and resolved_pubid != effective_ek_pubid:
                        _cache_set(key, url)
                        return url
                if _clean(converted) == _clean(url):
                    # Provenance guard: never accept an echoed source URL.
                    _cache_set(key, url)
                    return url
                _cache_set(key, converted)
                return converted
        except Exception as exc:  # retry with jitter
            last = exc
            await asyncio.sleep(min(2.5, 0.4 * (2 ** attempt) + (time.time() % 1) * 0.3))
    # Give up gracefully — keep the original link rather than drop the deal.
    _cache_set(key, url)
    return url


async def convert_links(urls: set[str], include_meesho: bool = False) -> dict[str, str]:
    """Convert supported EarnKaro merchants; Amazon stays separate.

    Meesho is included only when ``include_meesho`` is set (the Meesho ->
    EarnKaro fallback), otherwise it is left to the HYPD route.
    """
    allowed = {"merchant", "meesho"} if include_meesho else {"merchant"}
    urls = {
        url for url in urls
        if url and link_router.classify_url(url) in allowed
    }
    if not urls:
        return {}
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(convert_one(session, u, include_meesho=include_meesho) for u in urls)
        )
    return dict(zip(urls, results))


def convert_links_sync(urls: set[str], include_meesho: bool = False) -> dict[str, str]:
    return asyncio.run(convert_links(urls, include_meesho=include_meesho))


async def verify_earnkaro(test_url: str = "https://www.flipkart.com/p/itmEXAMPLE12345") -> dict:
    """Live-test an eligible EarnKaro merchant using dashboard settings first.

    A conversion is reported as verified only when the returned link is
    parseable and its configured publisher provenance can be checked. Short-link
    provenance requires a successful redirect resolution. This tests a link
    response, not a sale or commission attribution.
    """
    if link_router.classify_url(test_url) != "merchant":
        return {
            "ok": False,
            "error": "URL is not an eligible EarnKaro merchant; Amazon and Meesho use separate routes",
        }

    db_ek_key = db.get_global_setting("earnkaro_api_key")
    db_ek_pubid = db.get_global_setting("earnkaro_publisher_id")
    effective_ek_key = db_ek_key if db_ek_key else config.EARNKARO_API_KEY
    effective_ek_pubid = db_ek_pubid if db_ek_pubid else config.EARNKARO_PUBLISHER_ID
    if not effective_ek_key:
        return {"ok": False, "error": "EarnKaro API credential is not configured"}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                config.EARNKARO_API_URL,
                json={"deal": test_url},
                headers={
                    "Authorization": f"Bearer {effective_ek_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                status = resp.status
                body = await resp.text()
        except Exception as exc:  # network/DNS/TLS failure
            return {"ok": False, "error": f"request failed: {exc}"}

        if not 200 <= status < 300:
            return {
                "ok": False,
                "error": f"EarnKaro returned HTTP {status}",
                "http_status": status,
                "input": test_url,
                "raw_response": body[:500],
            }

        expected_pubid = str(effective_ek_pubid or "").strip()
        link = parse_ek_response(body, expected_pubid or None)
        provenance_verified: bool | None = None
        if link and expected_pubid:
            if _is_shortener(link):
                resolved_pubid = await _resolve_affextparam2(session, link)
                if resolved_pubid is not None:
                    provenance_verified = resolved_pubid == expected_pubid
            else:
                publisher_ids = _affextparam2_values(link)
                provenance_verified = bool(publisher_ids) and all(
                    publisher_id == expected_pubid for publisher_id in publisher_ids
                )

    verified = bool(link) and (
        not expected_pubid or provenance_verified is True
    )
    if not link:
        error = "response did not contain a valid link for the configured publisher"
    elif expected_pubid and provenance_verified is None:
        error = "publisher provenance could not be verified from the short link"
    elif expected_pubid and provenance_verified is False:
        error = "returned link does not match the configured publisher ID"
    else:
        error = None
    return {
        "ok": verified,
        "error": error,
        "http_status": status,
        "input": test_url,
        "converted_link": link,
        "publisher_id": expected_pubid or None,
        "publisher_provenance_verified": provenance_verified,
        "raw_response": body[:500],
    }
