"""Money radar — find exactly where commission leaks out of posted deals.

Routing the link correctly is not the same as earning on it. A post can carry
a link that pays nothing: a raw Meesho URL, an unconverted Flipkart link, or
an Amazon link tagged for somebody else. This module reads the deals we
actually posted and reports, per creator and per reason, how many links were
earning and how many were worth zero.

It never guesses revenue: it only states whether a posted link carried OUR
affiliate attribution. Network approval, cookies and cancellations are outside
what any link inspection can promise.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlparse

from . import advanced_shortener, link_router

STATE_EARNING = "earning"
STATE_LEAK = "leak"
STATE_NEUTRAL = "neutral"

_EARNKARO_SHORT_HOSTS = {
    "fktr.in", "ekaro.in", "ekaro.app", "clnk.in", "clnk.app", "myntr.it",
}

# Generic shorteners: attribution lives behind the redirect, so a link
# inspection cannot prove whose commission it carries.
_GENERIC_SHORT_HOSTS = {
    "bit.ly", "bitly.com", "j.mp", "tinyurl.com", "t.co", "cutt.ly",
    "shorturl.at", "rb.gy", "is.gd", "buff.ly", "ow.ly", "tiny.cc", "s.id",
}


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _publisher_ids(url: str, expected_pubid: str = "") -> list[str]:
    """Publisher ids visible in a link (``affExtParam2`` and plain ``id``)."""
    try:
        return link_router.publisher_ids_in_url(url, expected_pubid)
    except Exception:
        return []


def _is_earnkaro_short(url: str) -> bool:
    host = _host_of(url)
    return host in _EARNKARO_SHORT_HOSTS or (
        host.startswith("www.") and host[4:] in _EARNKARO_SHORT_HOSTS
    )


def effective_earnkaro_publisher_id() -> str:
    """Publisher ID from the vault first, then the environment."""
    from . import config, db

    return str(
        db.get_global_setting("earnkaro_publisher_id")
        or config.EARNKARO_PUBLISHER_ID
        or ""
    ).strip()


def _is_generic_short(url: str) -> bool:
    host = _host_of(url)
    return host in _GENERIC_SHORT_HOSTS or (
        host.startswith("www.") and host[4:] in _GENERIC_SHORT_HOSTS
    )


def classify_link(
    url: str,
    amazon_tag: str = "",
    hypd_store: str = "",
    earnkaro_pubid: str = "",
) -> dict:
    """Return one link's money state: earning, leak, or neutral."""
    tag = str(amazon_tag or "").strip()
    store = str(hypd_store or "").strip()
    pubid = str(earnkaro_pubid or "").strip()

    # First-party short links are only ever minted for OUR links, so they are
    # checked before the host-based classifier sees an unknown domain.
    if "/amazon/" in url and "tag=" in url:
        entry = {"url": url, "kind": "amazon", "state": STATE_NEUTRAL, "reason": ""}
        if tag and f"tag={tag}" in url:
            entry["state"] = STATE_EARNING
            entry["reason"] = f"Our Amazon short link (tag {tag})"
        else:
            entry["state"] = STATE_LEAK
            entry["reason"] = f"Amazon short link tagged for someone else (expected {tag})"
        return entry

    if "/m/" in url and _host_of(url) not in {"hypd.store", "www.hypd.store"}:
        entry = {"url": url, "kind": "hypd", "state": STATE_EARNING, "reason": "Our HYPD short link"}
        return entry

    # EarnKaro short links do not live on a merchant host, so they are checked
    # before the host-based classifier files them under "other".
    if _is_earnkaro_short(url):
        entry = {"url": url, "kind": "merchant", "state": STATE_EARNING, "reason": ""}
        ids = _publisher_ids(url, pubid)
        if pubid and ids and any(value != pubid for value in ids):
            entry["state"] = STATE_LEAK
            entry["reason"] = f"EarnKaro link for another publisher (expected {pubid})"
        elif ids:
            entry["reason"] = "EarnKaro link with our publisher"
        else:
            entry["reason"] = "EarnKaro short link (publisher not encoded in the URL)"
        return entry

    kind = link_router.classify_url(url)
    entry = {"url": url, "kind": kind, "state": STATE_NEUTRAL, "reason": ""}

    if _is_generic_short(url):
        entry["state"] = STATE_NEUTRAL
        entry["reason"] = "Short link — attribution sits behind the redirect"
        return entry

    if kind == "amazon":
        tags = [value for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True)
                if key.casefold() == "tag"]
        if tag and advanced_shortener.is_our_amazon_link(url, tag):
            entry["state"] = STATE_EARNING
            entry["reason"] = f"Amazon with our tag {tag}"
        elif tag and advanced_shortener.is_our_amazon_attribution(url, tag):
            # Search pages and storefronts cannot be canonicalised to /dp/ASIN,
            # but the tag carries the commission, so they still earn.
            entry["state"] = STATE_EARNING
            entry["reason"] = (
                f"Amazon page with our tag {tag} kept as-is "
                "(attribution by tag, not by the /dp/ path)"
            )
        elif not tags:
            entry["state"] = STATE_LEAK
            entry["reason"] = "Amazon link without a creator tag"
        else:
            entry["state"] = STATE_LEAK
            entry["reason"] = f"Amazon link tagged for someone else (expected {tag})"
        return entry

    if kind == "hypd":
        if store and advanced_shortener.is_our_hypd_link(url, store):
            entry["state"] = STATE_EARNING
            entry["reason"] = f"HYPD afflink on store {store}"
        else:
            entry["state"] = STATE_LEAK
            entry["reason"] = f"HYPD link on another store (expected {store or 'n/a'})"
        return entry

    if kind == "merchant":
        if _is_earnkaro_short(url):
            ids = _publisher_ids(url, pubid)
            if pubid and ids and any(value != pubid for value in ids):
                entry["state"] = STATE_LEAK
                entry["reason"] = f"EarnKaro link for another publisher (expected {pubid})"
            else:
                entry["state"] = STATE_EARNING
                entry["reason"] = "EarnKaro link with our publisher"
            return entry
        entry["state"] = STATE_LEAK
        entry["reason"] = (
            "Merchant link posted raw — no EarnKaro conversion, so it earns nothing"
        )
        return entry

    if kind == "meesho":
        entry["state"] = STATE_LEAK
        entry["reason"] = (
            "Raw Meesho link — HYPD cannot mint an affiliate link from it, so it earns nothing"
        )
        return entry

    if kind == "lehlah":
        entry["state"] = STATE_EARNING
        entry["reason"] = "LehLah attributed Meesho link"
        return entry

    entry["reason"] = "Informational link — never earns, never leaks"
    return entry


def _resolve_bitly(text: str, bitly_map: dict | None) -> str:
    """Replace our Bitly short links with the long URL they stand for."""
    if not text or not bitly_map:
        return text
    resolved = text
    for long_url, short_url in bitly_map.items():
        if short_url and long_url and short_url != long_url:
            resolved = resolved.replace(short_url, long_url)
    return resolved


def audit_text(
    text: str,
    amazon_tag: str = "",
    hypd_store: str = "",
    earnkaro_pubid: str = "",
    bitly_map: dict | None = None,
) -> dict:
    """Audit one rendered post: how many of its links actually earn."""
    text = _resolve_bitly(text or "", bitly_map)
    entries = [
        classify_link(url, amazon_tag, hypd_store, earnkaro_pubid)
        for url in link_router.find_urls(text or "")
    ]
    earning = [e for e in entries if e["state"] == STATE_EARNING]
    leaks = [e for e in entries if e["state"] == STATE_LEAK]
    monetisable = len(earning) + len(leaks)
    return {
        "links": len(entries),
        "earning": len(earning),
        "leak": len(leaks),
        "neutral": len(entries) - monetisable,
        "monetisable": monetisable,
        "coverage_pct": round(100 * len(earning) / monetisable) if monetisable else 100,
        "leaks": leaks,
        "earning_links": earning,
    }


def _creator_routing(influencer: dict, channel: dict) -> tuple[str, str]:
    from . import config

    from . import accounts

    # Audit with exactly the accounts the pipeline posts with: the creator's own
    # Amazon tag, and OUR central HYPD store (see influencer_hub/accounts.py).
    return accounts.routing_for(influencer, channel)


def report(days: int = 7, limit: int = 2000) -> dict:
    """Aggregate the last `days` of posted deals into one money report.

    `limit` caps how many recent posts are scanned, so a dashboard card can
    ask for a cheap summary without walking the whole history.
    """
    from . import config, db

    pubid = effective_earnkaro_publisher_id()
    creators = {profile["id"]: profile for profile in db.list_influencers()}
    channels = {}
    for profile in creators.values():
        for channel in db.list_channels(profile["id"]):
            channels[channel["id"]] = channel

    posts = db.recent_posted_texts(days=days, limit=limit)
    by_reason: dict[str, int] = {}
    by_creator: dict[int, dict] = {}
    totals = {"posts": 0, "links": 0, "earning": 0, "leak": 0, "monetisable": 0, "clean_posts": 0}

    for post in posts:
        influencer = creators.get(int(post.get("influencer_id") or 0))
        channel = channels.get(int(post.get("channel_id") or 0))
        if not influencer:
            continue
        channel = channel or {}
        tag, store = _creator_routing(influencer, channel)
        audit = audit_text(post.get("deal_text") or "", tag, store, pubid)
        totals["posts"] += 1
        totals["links"] += audit["monetisable"]
        totals["earning"] += audit["earning"]
        totals["leak"] += audit["leak"]
        totals["monetisable"] += audit["monetisable"]
        if audit["leak"] == 0 and audit["earning"] > 0:
            totals["clean_posts"] += 1

        for leak in audit["leaks"]:
            by_reason[leak["reason"]] = by_reason.get(leak["reason"], 0) + 1

        row = by_creator.setdefault(int(influencer["id"]), {
            "id": int(influencer["id"]),
            "name": influencer.get("name") or f"#{influencer['id']}",
            "posts": 0,
            "earning": 0,
            "leak": 0,
            "monetisable": 0,
        })
        row["posts"] += 1
        row["earning"] += audit["earning"]
        row["leak"] += audit["leak"]
        row["monetisable"] += audit["monetisable"]

    for row in by_creator.values():
        row["coverage_pct"] = (
            round(100 * row["earning"] / row["monetisable"]) if row["monetisable"] else 100
        )
    totals["coverage_pct"] = (
        round(100 * totals["earning"] / totals["monetisable"]) if totals["monetisable"] else 100
    )
    totals["zero_commission_posts"] = totals["posts"] - totals["clean_posts"]

    return {
        "days": days,
        "totals": totals,
        "by_reason": sorted(by_reason.items(), key=lambda item: -item[1]),
        "creators": sorted(by_creator.values(), key=lambda row: (-row["leak"], row["name"])),
        "suggestions": suggestions(totals, by_reason, config=config),
    }


def _global_flag(key: str, default: bool) -> bool:
    from . import db

    value = db.get_global_setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def suggestions(totals: dict, by_reason: dict, *, config) -> list[dict]:
    """Turn the measured leaks into concrete, ordered actions."""
    from . import db

    actions: list[dict] = []
    meesho_fallback_on = _global_flag(
        "meesho_earnkaro_fallback", bool(config.MEESHO_EARNKARO_FALLBACK)
    )
    only_earning_on = _global_flag(
        "only_earning_deals", bool(config.ONLY_EARNING_DEALS)
    )
    earnkaro_ready = bool(
        db.get_global_setting("earnkaro_api_key") or config.EARNKARO_API_KEY
    )
    raw_merchant = sum(count for reason, count in by_reason.items() if "raw" in reason.lower() and "Merchant" in reason)
    raw_meesho = sum(count for reason, count in by_reason.items() if "Raw Meesho" in reason)
    wrong_tag = sum(count for reason, count in by_reason.items() if "someone else" in reason)

    if raw_merchant and not earnkaro_ready:
        actions.append({
            "severity": "high",
            "title": f"{raw_merchant} merchant links earned nothing",
            "detail": "EarnKaro is on but its API key is missing. Add the key in Vault & Sources and those Flipkart/Myntra/Ajio links start converting.",
            "action": "Add EarnKaro key",
            "href": "/admin/secret-vault",
            "setting": None,
        })
    if raw_meesho and not meesho_fallback_on:
        actions.append({
            "severity": "high",
            "title": f"{raw_meesho} Meesho links earned nothing",
            "detail": "HYPD cannot turn a raw meesho.com link into an affiliate link. Enable the Meesho → EarnKaro fallback so those deals convert instead of posting for free.",
            "action": "Enable Meesho fallback",
            "href": "/money",
            "setting": "meesho_earnkaro_fallback",
        })
    elif raw_meesho:
        actions.append({
            "severity": "high",
            "title": f"{raw_meesho} Meesho links still posted raw",
            "detail": (
                "The Meesho → EarnKaro fallback is already on, so EarnKaro did not return a "
                "link for these. Check the EarnKaro key and publisher ID in the vault — "
                "without a conversion the deal keeps posting for free."
            ),
            "action": "Check EarnKaro key",
            "href": "/admin/secret-vault",
            "setting": None,
        })
    if wrong_tag:
        actions.append({
            "severity": "high",
            "title": f"{wrong_tag} Amazon links carried another tag",
            "detail": "Those commissions pay somebody else. Check the creator's Amazon tag and the channel's override tag.",
            "action": "Review tags",
            "href": "/setup",
            "setting": None,
        })
    if totals.get("zero_commission_posts") and not only_earning_on:
        actions.append({
            "severity": "medium",
            "title": f"{totals['zero_commission_posts']} posts had no earning link",
            "detail": "Turn on “Only post deals that earn” to hold back deals that would post for free and keep the channel's attention for paid ones.",
            "action": "Enable only-earning deals",
            "href": "/money",
            "setting": "only_earning_deals",
        })
    if not actions:
        actions.append({
            "severity": "ok",
            "title": "No commission leaks found",
            "detail": f"Every monetisable link in the last {totals.get('posts', 0)} posts carried our attribution.",
            "action": "",
            "href": "",
            "setting": None,
        })
    return actions
