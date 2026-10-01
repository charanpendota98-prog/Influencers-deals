"""First-party short links for existing LehLah-attributed Meesho product URLs.

LehLah's AppsFlyer query parameters identify the publisher attribution. This
module stores and redirects the full original URL unchanged; it does not try to
convert the link to HYPD or EarnKaro. No external shortener API key is needed.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl, urlparse

from . import config, db, link_router

logger = logging.getLogger(__name__)
_PRODUCT_PATH_RE = re.compile(r"/s/p/[A-Za-z0-9_-]+\Z")
_SHORT_CODE_RE = re.compile(r"[A-Za-z0-9_-]{8}\Z")


def configured_base_url(value: str | None = None) -> str:
    """Return a safe HTTPS origin, or blank when branded shortening is disabled."""
    base = str(
        value if value is not None else config.MEESHO_SHORT_LINK_BASE_URL
    ).strip().rstrip("/")
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
            "leaving the LehLah affiliate URL unchanged"
        )
        return ""
    return base


def is_valid_lehlah_meesho_url(url: str) -> bool:
    """Allow only Meesho product paths carrying LehLah attribution markers."""
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in {"meesho.com", "www.meesho.com"}
        or parsed.username
        or parsed.password
        or port is not None
        or not _PRODUCT_PATH_RE.fullmatch(parsed.path)
    ):
        return False
    params = {
        key.lower(): value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return (
        params.get("af_siteid", "").lower() == "lehlah"
        or params.get("mcn", "").lower() == "lehlah"
        or "lehlah" in params.get("pid", "").lower()
    )


def shorten_lehlah_links(text: str, base_url: str | None = None) -> str:
    """Wrap LehLah Meesho links as ``<base>/l/<code>`` without editing attribution."""
    base = configured_base_url(base_url)
    if not base or not text:
        return text

    rendered = text
    for url in link_router.find_urls(text):
        if not is_valid_lehlah_meesho_url(url):
            continue
        code = db.get_or_create_lehlah_short_link(url)
        rendered = rendered.replace(url, f"{base}/l/{code}")
    return rendered


def is_valid_short_code(code: str) -> bool:
    return bool(_SHORT_CODE_RE.fullmatch(str(code or "")))
