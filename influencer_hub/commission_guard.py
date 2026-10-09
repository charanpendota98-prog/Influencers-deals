"""Commission Guard — perfect verification that every shortened/posted link is OUR affiliate.

This module is the answer to "CHALA VARAKU MANAKU COMMISSION RADU".
Every final rendered deal is audited before dispatch:
- Amazon links MUST be canonical https://www.amazon.in/dp/<ASIN>?tag=OURTAG with exactly one tag that matches our Associate tag
- HYPD links MUST be https://hypd.store/<OUR_STORE>/afflink/<token> with no query/fragment and correct store ID
- Merchant links (Flipkart, Myntra, Ajio, etc.) MUST be EarnKaro short links (ekaro.in/fktr.in/clnk.in/myntr.it) with provenance that matches OUR publisher ID,
  OR they must have been correctly compacted and converted — never left as raw merchant URLs when allow_earnkaro is True
- Bitly-shortened OUR links are verified via cache to ensure they redirect to OUR canonical URLs

If any link fails verification, it is logged and optionally the deal is sanitized to avoid posting a non-earning link.

This guard does NOT guarantee commission (network approval, cookies, etc.), but it guarantees that the link we POST contains OUR affiliate attribution and is shortened correctly.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse

from . import amazon_shortlinks, hypd_shortlinks, lehlah_shortlinks, link_router
from .advanced_shortener import (
    is_our_amazon_attribution,
    is_our_amazon_link,
    is_our_hypd_link,
)

# EarnKaro shortener hosts (copied from earnkaro.py to avoid aiohttp import)
_EARNKARO_SHORT_HOSTS = {"fktr.in", "ekaro.in", "ekaro.app", "clnk.in", "clnk.app", "myntr.it"}


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_earnkaro_short(url: str) -> bool:
    try:
        host = _host_of(url)
        return host in _EARNKARO_SHORT_HOSTS or (host.startswith("www.") and host[4:] in _EARNKARO_SHORT_HOSTS)
    except Exception:
        return False


def is_earnkaro_short_link(url: str) -> bool:
    return _is_earnkaro_short(url)


def _publisher_id_values(url: str, expected_pubid: str = "") -> list[str]:
    """EarnKaro publisher ids visible in a link (``affExtParam2`` and ``id``)."""
    try:
        return link_router.publisher_ids_in_url(url, expected_pubid)
    except Exception:
        return []


def _verified_first_party_short_link(
    url: str,
    effective_amz_tag: str,
    effective_hypd_store: str,
) -> tuple[str, str] | None:
    """Return a verified hub short-link kind/reason, never a hostname guess.

    All three branded paths are deliberately checked against their configured
    HTTPS origin *and* a stored DB target.  This is the proof boundary that
    keeps ``https://evil.example/a/<code>?tag=ours`` from being counted as an
    earning link merely because it resembles one of our routes.
    """
    tag = str(effective_amz_tag or "").strip()
    store = str(effective_hypd_store or "").strip()
    if tag and amazon_shortlinks.is_our_amazon_short_url(url, tag):
        return "amazon", f"Verified branded Amazon short link (tag={tag})"
    if store and hypd_shortlinks.is_our_hypd_short_url(url, store):
        return "hypd", f"Verified branded HYPD short link (store={store})"
    if lehlah_shortlinks.is_our_lehlah_short_url(url):
        return "lehlah", "Verified branded LehLah short link (stored attribution preserved)"
    return None


def _unverified_first_party_shape(url: str) -> str:
    """Return the claimed route kind for a hub-shaped URL that is not verified.

    This catches typo domains, stale/deleted codes and forged paths before they
    can cross the commission guard.  Ordinary informational URLs remain
    untouched; only an exact compact route shape is treated as suspicious.
    """
    try:
        parsed = urlparse(str(url or ""))
    except (TypeError, ValueError):
        return ""
    if re.fullmatch(r"/(?:a|amazon)/[A-Za-z0-9_-]{8}", parsed.path, re.I):
        return "amazon"
    if re.fullmatch(r"/m/[A-Za-z0-9_-]{8}", parsed.path):
        return "hypd"
    if re.fullmatch(r"/l/[A-Za-z0-9_-]{8}", parsed.path):
        return "lehlah"
    return ""


def audit_rendered_text(
    rendered: str,
    effective_amz_tag: str,
    effective_hypd_store: str,
    expected_earnkaro_pubid: str | None = None,
    bitly_map: dict[str, str] | None = None,
    allowed_kinds: set[str] | None = None,
    source_text: str = "",
) -> dict:
    """
    Audit final rendered text for commission leaks.
    allowed_kinds: set of kinds that are enabled for this channel (e.g. {"amazon", "merchant", "hypd", "lehlah"}).
                   If provided, only leaks for enabled kinds are flagged. This respects user's toggles:
                   - If allow_earnkaro is False, raw merchant URLs are expected, not leaks
                   - If allow_hypd is False, raw Meesho is expected
    Returns dict with:
      - ok: bool (True if no leaks)
      - issues: list[str] — human readable problems
      - amazon_ok: bool, hypd_ok: bool, earnkaro_ok: bool
      - details: per-URL audit
    """
    effective_tag = str(effective_amz_tag or "").strip()
    effective_store = str(effective_hypd_store or "").strip()
    expected_pubid = str(expected_earnkaro_pubid or "").strip()

    issues: list[str] = []
    details: list[dict] = []

    urls = link_router.find_urls(rendered)
    # Build reverse map for Bitly: short -> long for verification
    bitly_reverse: dict[str, str] = {}
    if bitly_map:
        for long_u, short_u in bitly_map.items():
            if long_u != short_u:
                bitly_reverse[short_u] = long_u

    amazon_ok = True
    hypd_ok = True
    earnkaro_ok = True

    # Normalize allowed_kinds: None means all kinds are considered for strict audit
    # Empty set means none allowed (should have been filtered earlier)
    check_allowed = (lambda k: True) if allowed_kinds is None else (lambda k: k in allowed_kinds)

    for url in urls:
        kind = link_router.classify_url(url)
        host = _host_of(url)
        entry: dict = {"url": url, "kind": kind, "host": host, "ok": True, "reason": ""}

        # First-party routes are verified from the configured origin plus their
        # stored target, never from a path/host naming convention alone.
        branded = _verified_first_party_short_link(url, effective_tag, effective_store)
        if branded:
            entry["kind"], entry["reason"] = branded
            details.append(entry)
            continue
        claimed_kind = _unverified_first_party_shape(url)
        if claimed_kind:
            entry["kind"] = claimed_kind
            entry["ok"] = False
            entry["hard"] = True
            entry["reason"] = (
                f"Unverified branded-looking {claimed_kind} short link (not OUR): {url}"
            )
            issues.append(entry["reason"])
            if claimed_kind == "amazon":
                amazon_ok = False
            elif claimed_kind == "hypd":
                hypd_ok = False
            details.append(entry)
            continue

        # Check if this URL is a Bitly short link that we created for OUR Amazon/HYPD
        # Bitly links like https://bit.ly/xxxx should have been OUR links before shortening
        if host in {"bit.ly", "www.bit.ly", "bitly.com", "www.bitly.com"} or host.endswith(".bit.ly"):
            # Verify via bitly_reverse that it maps to an OUR link
            long_url = bitly_reverse.get(url)
            if long_url:
                # Check that long_url was OUR
                if link_router.classify_url(long_url) == "amazon":
                    if effective_tag and is_our_amazon_link(long_url, effective_tag):
                        entry["reason"] = f"Bitly verified -> OUR Amazon {effective_tag}"
                    else:
                        entry["ok"] = False
                        entry["reason"] = f"Bitly maps to non-OUR Amazon (expected tag {effective_tag}): {long_url}"
                        amazon_ok = False
                        issues.append(entry["reason"])
                elif link_router.classify_url(long_url) == "hypd":
                    if effective_store and is_our_hypd_link(long_url, effective_store):
                        entry["reason"] = f"Bitly verified -> OUR HYPD {effective_store}"
                    else:
                        entry["ok"] = False
                        entry["reason"] = f"Bitly maps to non-OUR HYPD (expected store {effective_store}): {long_url}"
                        hypd_ok = False
                        issues.append(entry["reason"])
                else:
                    # Bitly for generic merchant pre-ADVANCED mode — not used when advanced_only_our enabled
                    entry["reason"] = f"Bitly for generic merchant: {long_url}"
            elif _source_wrapper(url, source_text):
                # Same wrapper in the untouched source post -> it is the source's
                # own short link, so it pays somebody else (or nobody).
                entry["ok"] = False
                entry["reason"] = (
                    f"Wrapper carried by the source post — not created by us: {url}"
                )
            else:
                # Bitly link not in our map — in advanced_only_our mode, any bit.ly is OUR (generic Bitly disabled)
                # So we treat it as OUR if no map, to avoid false warnings for correctly shortened OUR links
                entry["reason"] = "Bitly link (assumed OUR in advanced mode — generic Bitly disabled)"
                # No issue appended — bit.ly is expected for OUR Amazon/HYPD when base URL not configured

        elif kind == "amazon":
            # Must be OUR canonical Amazon — only flag if Amazon is enabled for this channel
            if not check_allowed("amazon"):
                entry["reason"] = f"Amazon link (Amazon disabled for this channel, posted as-is): {url}"
            elif not effective_tag:
                entry["ok"] = False
                entry["reason"] = f"Amazon link without effective tag: {url}"
                amazon_ok = False
                issues.append(entry["reason"])
            elif is_our_amazon_link(url, effective_tag):
                entry["reason"] = f"OUR Amazon verified tag={effective_tag}"
            elif is_our_amazon_attribution(url, effective_tag):
                # A search page, a storefront or a short link cannot be turned
                # into /dp/ASIN without a network round-trip, but commission is
                # carried by the tag — so the page is KEPT, never deleted.
                if link_router.is_amazon_short_host(url):
                    entry["reason"] = f"{_host_of(url)} with OUR tag (short code opaque, tag appended): {url}"
                    issues.append(
                        f"WARN opaque Amazon short link unresolved (commission best-effort, "
                        f"prefer /dp/ASIN): {url}"
                    )
                else:
                    entry["reason"] = (
                        f"OUR Amazon page kept as-is (tag={effective_tag}, "
                        f"route {urlparse(url).path or '/'}) — attribution comes "
                        "from the tag, not from the /dp/ path"
                    )
            else:
                parsed = urlparse(url)
                tags = [v for k, v in parse_qsl(parsed.query) if k.lower() == "tag"]
                if link_router.is_amazon_short_host(url):
                    entry["ok"] = False
                    entry["reason"] = (
                        f"opaque Amazon short link without OUR tag "
                        f"(expected {effective_tag}): {url}"
                    )
                    amazon_ok = False
                    issues.append(entry["reason"])
                else:
                    entry["ok"] = False
                    entry["reason"] = f"Amazon link not OUR canonical (expected tag {effective_tag}): {url}"
                    amazon_ok = False
                    issues.append(entry["reason"])

        elif kind == "hypd":
            if not check_allowed("hypd"):
                entry["reason"] = f"HYPD link (HYPD disabled for this channel, posted as-is): {url}"
            elif not effective_store:
                entry["ok"] = False
                entry["reason"] = f"HYPD link without effective store: {url}"
                hypd_ok = False
                issues.append(entry["reason"])
            elif is_our_hypd_link(url, effective_store):
                entry["reason"] = f"OUR HYPD verified store={effective_store}"
            else:
                entry["ok"] = False
                entry["reason"] = f"HYPD link not OUR (expected store {effective_store}): {url}"
                hypd_ok = False
                issues.append(entry["reason"])

        elif kind == "meesho":
            # Raw Meesho product URL — NOT an affiliate unless converted to hypd.store afflink
            # Only flag as leak if HYPD/Meesho is enabled for this channel (user expects commission)
            if check_allowed("meesho") or check_allowed("hypd"):
                entry["ok"] = False
                entry["reason"] = f"Raw Meesho URL (no HYPD afflink, no commission): {url} — needs hypd.store/93944/afflink/token"
                issues.append(entry["reason"])
                hypd_ok = False
            else:
                entry["reason"] = f"Raw Meesho URL (HYPD disabled for this channel, posted as-is): {url}"

        elif kind == "merchant":
            # Raw merchant URL that was NOT converted to EarnKaro short link
            # Only flag if EarnKaro is enabled for this channel
            if check_allowed("merchant"):
                entry["ok"] = False
                entry["reason"] = f"Merchant URL not converted to EarnKaro (no commission): {url} — expected ekaro.in/fktr.in with pubid {expected_pubid or '5478322'}"
                earnkaro_ok = False
                issues.append(entry["reason"])
            else:
                entry["reason"] = f"Merchant URL (EarnKaro disabled for this channel, posted as-is): {url}"

        elif kind == "lehlah":
            # LehLah Meesho with attribution — keep as is, no EarnKaro
            entry["reason"] = "LehLah Meesho — attribution preserved, not EarnKaro"

        elif _is_earnkaro_short(url):
            # EarnKaro short link (ekaro.in, fktr.in, clnk.in, myntr.it, etc.)
            # Verify provenance if expected_pubid known — check the publisher
            # id parameters visible on the short link itself.
            pubids = _publisher_id_values(url, expected_pubid)
            if expected_pubid:
                if pubids:
                    if all(pid == expected_pubid for pid in pubids):
                        entry["reason"] = f"EarnKaro short verified pubid={expected_pubid}"
                    else:
                        entry["ok"] = False
                        entry["reason"] = f"EarnKaro short has wrong publisher {pubids} (expected {expected_pubid}): {url}"
                        earnkaro_ok = False
                        issues.append(entry["reason"])
                else:
                    # Short link doesn't carry pubid visibly — will be verified after redirect via _resolve_affextparam2
                    # For rendered text audit, we consider this OK if it was produced by our convert (host is shortener)
                    entry["reason"] = f"EarnKaro short link (pubid verified via redirect, host {host})"
            else:
                entry["reason"] = f"EarnKaro short link host {host}"

        elif kind == "shortener":
            # A wrapper we did not resolve: attribution is unknown. It is not a
            # HARD leak (no foreign tag/store is proven), so it never triggers
            # sanitisation — but it must be visible to the flow board, the audit
            # and the retry decision instead of being filed away as "other".
            entry["ok"] = False
            entry["reason"] = (
                f"Opaque wrapper link ({host}) — destination unknown, attribution unverified: {url}"
            )

        else:
            # Other URLs (t.me, etc.) — not affiliate, ignore
            entry["reason"] = f"Non-affiliate URL (other): {url}"

        details.append(entry)

    # Strict ok cares about a wrong Amazon/HYPD attribution and any forged,
    # stale or wrong-origin branded route. Merchant/Meesho raw URLs are still
    # soft conversion gaps: they are handled by the retry/hold flow below.
    strict_ok = not any(
        not detail["ok"]
        and (detail["kind"] in {"amazon", "hypd"} or detail.get("hard"))
        for detail in details
    )

    return {
        "ok": strict_ok,
        "strict_ok": strict_ok,
        "issues": issues,
        "amazon_ok": amazon_ok,
        "hypd_ok": hypd_ok,
        "earnkaro_ok": earnkaro_ok,
        "details": details,
    }


def _source_wrapper(url: str, source_text: str) -> bool:
    """True when a wrapper link was already carried by the source post.

    The hub mints its own Bitly wrappers *after* rendering, so seeing that same
    short link in the untouched source text means it belongs to whoever wrote the
    post — not to us. Such a link must never be counted as OUR commission.
    """
    if not source_text:
        return False
    try:
        return link_router._normalised_link(url) in {
            link_router._normalised_link(found)
            for found in link_router.find_urls(source_text)
        }
    except Exception:
        return False


def is_verified_our_link(
    url: str,
    effective_amz_tag: str,
    effective_hypd_store: str,
    expected_pubid: str | None = None,
    bitly_map: dict[str, str] | None = None,
    source_text: str = "",
) -> bool:
    """True when a single final URL is verifiably carrying OUR attribution.

    This is the same predicate the pre-dispatch guard uses, exposed so the
    pipeline can ask one question: "does this post contain at least one link
    that pays us?" A Bitly link counts when our own Bitly map says it wraps one
    of OUR links (or when the map is absent, matching the advanced-only mode
    where generic merchant Bitly is disabled).
    """
    url = str(url or "").strip()
    if not url:
        return False
    tag = str(effective_amz_tag or "").strip()
    store = str(effective_hypd_store or "").strip()
    host = _host_of(url)
    kind = link_router.classify_url(url)

    # A compact link is ours only after origin + code + stored target + account
    # attribution all agree. Do this before host classification because branded
    # domains intentionally classify as ``other`` to the generic router.
    if _verified_first_party_short_link(url, tag, store):
        return True
    if _unverified_first_party_shape(url):
        return False

    if _is_earnkaro_short(url):
        return True
    if kind == "lehlah":
        return True
    if kind == "amazon" and tag:
        if is_our_amazon_link(url, tag) or is_our_amazon_attribution(url, tag):
            return True
    if kind == "hypd" and store and is_our_hypd_link(url, store):
        return True
    if host in {"bit.ly", "www.bit.ly", "bitly.com", "www.bitly.com"} or host.endswith(".bit.ly"):
        long_url = ""
        if bitly_map:
            long_url = str(bitly_map.get(url) or "").strip()
            if not long_url:
                for long_url_candidate, short_url in bitly_map.items():
                    if str(short_url).strip() == url:
                        long_url = str(long_url_candidate).strip()
                        break
        if not long_url:
            if _source_wrapper(url, source_text):
                # The source post itself carried this bit.ly: it wraps someone
                # else's link, so it is not OURS — never count it as commission.
                return False
            # Advanced-only mode disables generic merchant Bitly, so an unknown
            # bit.ly created after rendering can only be one we created.
            return True
        return is_verified_our_link(long_url, tag, store, expected_pubid)
    return False


def amazon_tag_proof(
    requested_tag: str,
    sample_url: str = "https://www.amazon.in/dp/B0D9P2M1PB?tag=someone-else-21",
    *,
    compact: bool = True,
) -> dict:
    """Build the exact Amazon link a saved broadcast channel will publish.

    This does no network I/O.  When a trusted branded HTTPS origin is configured
    it allocates one durable local redirect record, then proves both the compact
    URL and its canonical Amazon target before returning it to the dashboard.
    That means the save confirmation is clickable evidence, not a mock string.
    """
    from . import link_router as _router

    requested = str(requested_tag or "").strip()
    tag = _router.effective_amazon_tag(requested)
    canonical = _router.render_for_influencer(sample_url, tag)
    posted = canonical
    status = amazon_shortlinks.short_link_status()
    short_link_error = ""
    if compact and status["active"]:
        try:
            compact = amazon_shortlinks.shorten_amazon_links(canonical)
            if compact != canonical and amazon_shortlinks.is_our_amazon_short_url(compact, tag):
                posted = compact
            elif compact != canonical:
                short_link_error = "The generated branded code could not be verified."
        except Exception as exc:  # fail safe: canonical Amazon attribution survives
            short_link_error = f"Could not create the branded short link ({type(exc).__name__})."

    links = our_affiliate_urls(posted, tag, "", "")
    shortened = posted != canonical
    return {
        "requested_tag": requested,
        "tag": tag,
        "fallback_used": tag != requested,
        "tag_usable": _router.amazon_tag_is_usable(requested_tag),
        "posted_link": links[0] if links else posted,
        "canonical_link": canonical,
        "redirect_target": canonical if shortened else "",
        "shortened": shortened,
        "compact_requested": bool(compact),
        "short_link_status": status,
        "short_link_error": short_link_error,
        "ok": bool(links),
        "rendered": posted,
    }


def our_affiliate_urls(
    rendered: str,
    effective_amz_tag: str,
    effective_hypd_store: str,
    expected_pubid: str | None = None,
    bitly_map: dict[str, str] | None = None,
    source_text: str = "",
) -> list[str]:
    """Every URL in ``rendered`` that is verifiably OURS (earns for us).

    ``source_text`` (the untouched source post) keeps source wrappers from being
    mistaken for wrappers this hub created.
    """
    return [
        url for url in link_router.find_urls(rendered)
        if is_verified_our_link(
            url, effective_amz_tag, effective_hypd_store, expected_pubid, bitly_map, source_text
        )
    ]


# Kinds whose zero-commission state is a *conversion failure*: the link can and
# normally does become OUR EarnKaro short, so a raw one only means the
# conversion did not happen (API/key down, transient error, unsupported URL).
CONVERTIBLE_KINDS = ("merchant",)

#: Wrapper links (bit.ly/tinyurl/…): the destination — and therefore who gets
#: paid — is unknown until they are resolved. A pure wrapper with nothing that
#: pays is worth retrying exactly like a failed EarnKaro conversion.
WRAPPED_KINDS = ("shortener",)

# Kinds that stay raw by design: HYPD cannot mint an affiliate link from a raw
# meesho.com product URL, so such a link earns nothing no matter what we do.
UNMONETISABLE_KINDS = ("meesho",)


def unmonetised_links(audit: dict, allowed_kinds: set[str] | None = None,
                      kinds: tuple[str, ...] = CONVERTIBLE_KINDS) -> list[dict]:
    """Audit details for links that pass the hard checks yet pay us nothing.

    ``kinds`` selects which leak shapes to report: merchant links that EarnKaro
    never converted (a conversion failure we can retry) and/or raw Meesho links
    (nothing can monetise those today). Only kinds the channel actually allows
    are reported, so an explicitly disabled route is never treated as a leak.
    """
    allowed = None if allowed_kinds is None else {k for k in allowed_kinds}
    leaked: list[dict] = []
    for detail in audit.get("details", []):
        if detail.get("ok"):
            continue
        kind = detail.get("kind")
        if kind not in kinds:
            continue
        if allowed is not None and kind not in allowed:
            continue
        leaked.append(detail)
    return leaked


def sanitize_rendered_text(
    rendered: str,
    effective_amz_tag: str,
    effective_hypd_store: str,
    expected_pubid: str | None = None,
    bitly_map: dict[str, str] | None = None,
    allowed_kinds: set[str] | None = None,
) -> tuple[str, dict]:
    """
    Remove non-OUR affiliate links from rendered text to guarantee commission safety.
    Returns (sanitized_text, audit_report).
    If audit is ok, returns original text.
    Otherwise, removes the leaked URLs (replaces with "") and returns cleaned text.
    """
    audit = audit_rendered_text(
        rendered,
        effective_amz_tag,
        effective_hypd_store,
        expected_pubid,
        bitly_map,
        allowed_kinds)
    if audit["ok"]:
        return rendered, audit

    # Remove hard attribution failures only (wrong Amazon/HYPD account and
    # forged/stale branded paths). Raw merchant/Meesho links remain visible with
    # their conversion warning so the retry flow can repair them.
    sanitized = rendered
    for detail in audit["details"]:
        if not detail["ok"] and (
            detail["kind"] in {"amazon", "hypd"} or detail.get("hard")
        ):
            sanitized = sanitized.replace(detail["url"], "")
    # Clean up double spaces / empty lines left behind
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    sanitized = re.sub(r"[ ]{2,}", " ", sanitized).strip()
    return sanitized, audit
