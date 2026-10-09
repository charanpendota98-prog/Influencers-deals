"""Optional first-party branded redirects for HYPD affiliate links.

This does not create HYPD affiliate tokens. It gives an existing, valid HYPD
store/afflink URL a stable short code on an operator-owned HTTPS hostname, then
redirects to the stored HYPD URL. With no base domain configured, source links
remain unchanged.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from . import config, db, link_router

logger = logging.getLogger(__name__)
_HYPD_AFFLINK_PATH_RE = re.compile(r"/\d+/afflink/[A-Za-z0-9_-]+\Z")
_SHORT_CODE_RE = re.compile(r"[A-Za-z0-9_-]{8}\Z")


def configured_base_url(value: str | None = None) -> str:
    """Return a safe HTTPS origin, or an empty string to disable shortening."""
    configured = (
        value
        if value is not None
        else (config.MEESHO_SHORT_LINK_BASE_URL or config.AFFILIATE_SHORT_LINK_BASE_URL)
    )
    base = str(configured or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlparse(base)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        logger.warning(
            "MEESHO_SHORT_LINK_BASE_URL must be an origin-only HTTPS URL; "
            "using the original HYPD affiliate link"
        )
        return ""
    return base


def is_valid_hypd_affiliate_url(url: str) -> bool:
    """Accept only HTTPS HYPD store afflinks, never arbitrary redirect targets."""
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in {"hypd.store", "www.hypd.store"}
        and not parsed.username
        and not parsed.password
        and port is None
        and not parsed.query
        and not parsed.fragment
        and _HYPD_AFFLINK_PATH_RE.fullmatch(parsed.path)
    )


def shorten_hypd_links(text: str, base_url: str | None = None) -> str:
    """Shorten valid HYPD afflinks to ``<base>/m/<stable-code>``.

    The pipeline calls this after HYPD store-ID rewriting, so the stored URL
    keeps the influencer/channel's effective HYPD attribution. Text punctuation
    and surrounding copy are left intact. Unsupported HYPD URLs are untouched.
    """
    base = configured_base_url(base_url)
    if not base or not text:
        return text

    rendered = text
    for url in link_router.find_urls(text):
        if not is_valid_hypd_affiliate_url(url):
            continue
        code = db.get_or_create_hypd_short_link(url)
        rendered = rendered.replace(url, f"{base}/m/{code}")
    return rendered


def _same_origin(left, right) -> bool:
    try:
        left_port = left.port
        right_port = right.port
    except ValueError:
        return False
    return (
        left.scheme.lower() == right.scheme.lower() == "https"
        and (left.hostname or "").lower() == (right.hostname or "").lower()
        and left_port == right_port
    )


def is_our_hypd_short_url(
    url: str,
    store_id: str,
    base_url: str | None = None,
) -> bool:
    """Return true only for a stored, trusted-origin HYPD compact link.

    A random ``/m/abcdefgh`` URL is not proof of commission.  It must be on
    this deployment's configured branded origin and map to the requested HYPD
    store's clean affiliate target in our DB.
    """
    base = configured_base_url(base_url)
    store = str(store_id or "").strip()
    if not base or not store:
        return False
    try:
        parsed = urlparse(str(url or ""))
        trusted = urlparse(base)
        if (
            not _same_origin(parsed, trusted)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            return False
    except (TypeError, ValueError):
        return False
    match = re.fullmatch(r"/m/([A-Za-z0-9_-]{8})", parsed.path)
    if not match:
        return False
    try:
        record = db.get_hypd_short_link(match.group(1))
    except Exception:
        return False
    target = str((record or {}).get("target_url") or "")
    return bool(
        record
        and str(record.get("store_id") or "") == store
        and is_valid_hypd_affiliate_url(target)
        and re.fullmatch(rf"/{re.escape(store)}/afflink/[A-Za-z0-9_-]+", urlparse(target).path)
    )


def is_valid_short_code(code: str) -> bool:
    return bool(_SHORT_CODE_RE.fullmatch(str(code or "")))
