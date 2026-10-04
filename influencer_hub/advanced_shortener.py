"""Advanced first-party + Bitly fallback shortener for ONLY OUR affiliate links.

This module implements the user's request: 
- ONLY HYPD links with OUR store ID (93944) are shortened, via first-party /m/ or Bitly fallback
- ONLY Amazon links with OUR Associate tag are shortened, via first-party /amazon/ or Bitly fallback
- Generic merchant links (Flipkart etc.) are NOT shortened here; they use EarnKaro's own ekaro.in shortener
- This is an ADVANCED system: short codes are stored in DB, redirects are verified, tags are preserved

The shortener is ONLY for links that have already been converted to OUR affiliate IDs.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse, urlencode, urlunparse

from . import config, db, link_router
from . import amazon_shortlinks, hypd_shortlinks

# Short code pattern for first-party links
_SHORT_CODE_RE = re.compile(r"[A-Za-z0-9_-]{8}\Z")

def is_our_amazon_link(url: str, amazon_tag: str) -> bool:
    """Check if URL is OUR Amazon affiliate link with the exact tag."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"amazon.in", "www.amazon.in", "amazon.com", "www.amazon.com"}:
            return False
        # Must be canonical /dp/ASIN with OUR tag
        if not re.fullmatch(r"/dp/[A-Za-z0-9]{10}", parsed.path, re.I):
            return False
        query_tags = [v for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() == "tag"]
        # Must have exactly one tag and it must be OUR tag
        return len(query_tags) == 1 and query_tags[0] == amazon_tag
    except Exception:
        return False

def is_our_hypd_link(url: str, hypd_store_id: str) -> bool:
    """Check if URL is OUR HYPD affiliate link with the exact store ID."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"hypd.store", "www.hypd.store"}:
            return False
        # Must be /<store_id>/afflink/<token> with OUR store ID and no query/fragment
        if parsed.query or parsed.fragment:
            return False
        m = re.fullmatch(r"/(\d+)/afflink/([A-Za-z0-9_-]+)", parsed.path)
        if not m:
            return False
        return m.group(1) == str(hypd_store_id).strip()
    except Exception:
        return False

def get_our_amazon_links(text: str, amazon_tag: str) -> list[str]:
    """Extract OUR Amazon links from text."""
    return [url for url in link_router.find_urls(text) if is_our_amazon_link(url, amazon_tag)]

def get_our_hypd_links(text: str, hypd_store_id: str) -> list[str]:
    """Extract OUR HYPD links from text."""
    return [url for url in link_router.find_urls(text) if is_our_hypd_link(url, hypd_store_id)]

async def shorten_our_links_advanced(text: str, amazon_tag: str, hypd_store_id: str, bitly_token: str | None = None) -> str:
    """
    Advanced shortener that ONLY shortens OUR affiliate links.
    
    Priority:
    1. For Amazon OUR links: try first-party /amazon/<code>?tag=... if AMAZON_SHORT_LINK_BASE_URL configured,
       else fallback to Bitly if bitly_token available and link is long or user wants all OUR links shortened
    2. For HYPD OUR links: try first-party /m/<code> if MEESHO_SHORT_LINK_BASE_URL configured,
       else fallback to Bitly if bitly_token available
    
    This ensures ONLY OUR links are shortened, never source links with old tags.
    """
    if not text:
        return text
    
    rendered = text
    effective_tag = str(amazon_tag or "").strip()
    effective_store = str(hypd_store_id or "").strip()
    bitly_token = str(bitly_token or "").strip()
    
    # Check if advanced shortener is enabled via DB or config
    # Default: enabled for HYPD and Amazon (user requested ONLY OUR links)
    # Check if advanced shortener is enabled via config or DB
    # Default enabled (user requested ONLY MANA LINK shortening)
    try:
        db_enabled = str(db.get_global_setting("advanced_shortener_enabled", "")).strip().lower()
        if db_enabled:
            advanced_enabled = db_enabled in {"1", "true", "yes", "on"}
        else:
            advanced_enabled = bool(config.ADVANCED_SHORTENER_ENABLED)
    except Exception:
        advanced_enabled = bool(config.ADVANCED_SHORTENER_ENABLED)
    
    if not advanced_enabled:
        return rendered
    
    # Check per-network toggles
    try:
        db_amazon = str(db.get_global_setting("amazon_advanced_shortener_enabled", "")).strip().lower()
        db_hypd = str(db.get_global_setting("hypd_advanced_shortener_enabled", "")).strip().lower()
        if db_amazon:
            amazon_advanced = db_amazon in {"1", "true", "yes", "on"}
        else:
            amazon_advanced = bool(config.AMAZON_ADVANCED_SHORTENER_ENABLED)
        if db_hypd:
            hypd_advanced = db_hypd in {"1", "true", "yes", "on"}
        else:
            hypd_advanced = bool(config.HYPD_ADVANCED_SHORTENER_ENABLED)
    except Exception:
        amazon_advanced = bool(config.AMAZON_ADVANCED_SHORTENER_ENABLED)
        hypd_advanced = bool(config.HYPD_ADVANCED_SHORTENER_ENABLED)
    
    # Step 1: Try first-party shortening for OUR links (always, if base configured)
    # This is the ADVANCED first-party system with DB storage and verified redirects
    # Check if Bitly fallback is enabled for OUR links when first-party base not configured
    # This is opt-in for backward compat with existing tests; user can enable via dashboard vault
    try:
        db_bitly_fallback = str(db.get_global_setting("advanced_bitly_fallback_enabled", "")).strip().lower()
        if db_bitly_fallback:
            bitly_fallback_enabled = db_bitly_fallback in {"1", "true", "yes", "on"}
        else:
            bitly_fallback_enabled = bool(getattr(config, "ADVANCED_BITLY_FALLBACK_ENABLED", False))
    except Exception:
        bitly_fallback_enabled = bool(getattr(config, "ADVANCED_BITLY_FALLBACK_ENABLED", False))

    if amazon_advanced and effective_tag:
        # Use first-party if base URL is configured, otherwise it will be no-op
        before = rendered
        rendered = amazon_shortlinks.shorten_amazon_links(rendered)
        # If first-party didn't shorten (no base URL) and Bitly fallback enabled + token available, use Bitly for OUR Amazon links
        if rendered == before and bitly_fallback_enabled and bitly_token:
            # Find OUR Amazon links that are still present
            our_amazon_urls = get_our_amazon_links(rendered, effective_tag)
            if our_amazon_urls:
                # For advanced system, shorten ALL OUR Amazon links via Bitly (not just long ones)
                # But respect approval channel: this function is only called for broadcast/whatsapp, not approval
                from . import bitly_client
                bitly_map = await bitly_client.shorten_urls(our_amazon_urls, token=bitly_token)
                for long_url, short_url in bitly_map.items():
                    if long_url != short_url and short_url:
                        # Only replace if Bitly succeeded and short is different
                        rendered = rendered.replace(long_url, short_url)
    
    if hypd_advanced and effective_store:
        before = rendered
        rendered = hypd_shortlinks.shorten_hypd_links(rendered)
        if rendered == before and bitly_fallback_enabled and bitly_token:
            our_hypd_urls = get_our_hypd_links(rendered, effective_store)
            if our_hypd_urls:
                from . import bitly_client
                bitly_map = await bitly_client.shorten_urls(our_hypd_urls, token=bitly_token)
                for long_url, short_url in bitly_map.items():
                    if long_url != short_url and short_url:
                        rendered = rendered.replace(long_url, short_url)
    
    return rendered

def sync_shorten_our_links_advanced(text: str, amazon_tag: str, hypd_store_id: str, bitly_token: str | None = None) -> str:
    """Sync wrapper for testing."""
    import asyncio
    return asyncio.run(shorten_our_links_advanced(text, amazon_tag, hypd_store_id, bitly_token))
