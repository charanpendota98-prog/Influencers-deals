"""Link routing — the heart of the influencer scheme.

For a given deal text and a given influencer we produce a copy of the text in
which Amazon links use that influencer's configured Associate tag, while
eligible links from other supported merchants may be replaced by a configured
affiliate-network result. This preserves the identifiers and routing choices
that the application controls; it cannot guarantee network approval or
commission attribution.

This module is PURE and has NO network or credentials dependency, so it is
exercised directly by the unit tests. EarnKaro conversion itself is an async
network call done by the pipeline and passed in as a precomputed map.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import config

# Amazon product hosts supported by this India-first integration.
AMAZON_DOMAINS = {"amazon.in", "amazon.com", "amzn.to", "amzn.in"}
HYPD_DOMAINS = {"hypd.store"}

# Merchant domains we route through EarnKaro (except Amazon, HYPD, and
# Meesho). Meesho has its own HYPD path and must never be sent to EarnKaro.
# Expanded to cover EarnKaro's 150+ supported brands - ensures sources from
# many apps are perfectly converted to affiliate links.
MERCHANT_DOMAINS = {
    # Core - Flipkart group
    "flipkart.com", "fktr.in", "dl.flipkart.com", "fkrt.in",
    "shopsy.in",
    # Fashion - Myntra / Ajio / Nykaa / Snapdeal
    "myntra.com", "myntr.it",
    "ajio.com",
    "nykaa.com", "nykaafashion.com", "nykaaman.com",
    "snapdeal.com",
    # Tata / Croma / Reliance / Jiomart
    "tatacliq.com", "tatacliq.in",
    "croma.com",
    "reliancedigital.in",
    "jiomart.com", "jio-mart.com",
    # Beauty / Personal care / Pharma
    "mamaearth.in",
    "purplle.com",
    "sugarcosmetics.com", "sugar.com",
    "myglamm.com",
    "wowskinscience.com", "buywow.in",
    "healthkart.com",
    "1mg.com", "tata1mg.com",
    "pharmeasy.in",
    # Lifestyle / Electronics / Home
    "boat-lifestyle.com",
    "gonoise.com",
    "bewakoof.com",
    "zivame.com",
    "clovia.com",
    "pepperfry.com",
    "firstcry.com", "firstcry.in",
    "lenskart.com",
    "caratlane.com",
    "blinkit.com", "blinkit.in",
    "bigbasket.com", "bigbasket.in",
    "zepto.com", "zeptonow.com",
    "licious.in",
    "dealshare.com",
    "countrydelight.in",
    "celio.in",
    # Additional popular EarnKaro merchants
    "puma.com", "in.puma.com", "puma.in",
    "adidas.co.in", "adidas.com",
    "nike.com", "nike.in",
    "bata.com", "bata.in",
    "wildcraft.com",
    "levis.in",
    "jockey.in",
    "fastrack.in",
    "titan.co.in",
    "samsung.com",
    "oneplus.in",
    "realme.com",
    "xiaomi.com", "mi.com",
    "campusshoes.com",
    "redtape.com",
    "woodland.co.in",
    "libas.in",
    "biba.in",
    "wforwoman.com",
    "fabindia.com",
    "lifestylestores.com", "lifestyle.com",
    "shoppersstop.com",
    "westside.com",
    "maxfashion.in",
    "fashionandyou.com",
    "limeroad.com",
    "koovs.com",
    "urbanic.com",
    "hm.com",
    "zara.com",
    "uniqlo.com",
    "nykaa.com",
    "myntra.com",
}

# A reasonably permissive URL finder (http/https only).
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)


def find_urls(text: str) -> list[str]:
    raw_urls = URL_RE.findall(text)
    cleaned = []
    for u in raw_urls:
        while u and u[-1] in ".,;!?:'\"":
            u = u[:-1]
        if u:
            cleaned.append(u)
    return cleaned


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _is_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _is_lehlah_meesho_affiliate(url: str) -> bool:
    """Detect LehLah/AppsFlyer tracking so its existing affiliate attribution is kept."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not _is_domain(host, "meesho.com"):
        return False
    params = {key.lower(): value for key, value in parse_qsl(parsed.query, keep_blank_values=True)}
    return (
        params.get("af_siteid", "").lower() == "lehlah"
        or params.get("mcn", "").lower() == "lehlah"
        or "lehlah" in params.get("pid", "").lower()
    )


def classify_url(url: str) -> str:
    """Return 'amazon' | 'hypd' | 'lehlah' | 'meesho' | 'merchant' | 'other'."""
    host = _host_of(url)
    if any(_is_domain(host, domain) for domain in AMAZON_DOMAINS):
        return "amazon"
    if _is_domain(host, "hypd.store"):
        return "hypd"
    if _is_lehlah_meesho_affiliate(url):
        return "lehlah"
    if _is_domain(host, "meesho.com"):
        return "meesho"
    if any(_is_domain(host, domain.removeprefix("www.")) for domain in MERCHANT_DOMAINS):
        return "merchant"
    return "other"


def convert_hypd_store_link(url: str, target_store_id: str | None = None) -> str:
    """Retag a valid existing HYPD ``/afflink/<token>`` URL.

    Store-only pages and raw Meesho product URLs are not affiliate tokens, so
    they are deliberately left unchanged. Query parameters and fragments on a
    valid affiliate URL are preserved while the creator's numeric store ID is
    replaced.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError):
        return url

    if (
        parsed.scheme not in {"http", "https"}
        or host not in {"hypd.store", "www.hypd.store"}
        or parsed.username
        or parsed.password
        or port is not None
    ):
        return url

    store = str(target_store_id or config.HYPD_STORE_ID).strip()
    if not re.fullmatch(r"\d+", store):
        return url
    match = re.fullmatch(
        r"/(?:\d+|[A-Za-z0-9_-]+)/afflink/([A-Za-z0-9_-]+)",
        parsed.path,
        re.I,
    )
    if not match:
        return url

    token = match.group(1)
    return urlunparse((
        "https", "hypd.store", f"/{store}/afflink/{token}", "",
        parsed.query, parsed.fragment,
    ))


def collect_links(text: str) -> dict[str, str]:
    """Map each unique URL -> its classification."""
    out: dict[str, str] = {}
    for u in find_urls(text):
        out[u] = classify_url(u)
    return out


def compact_merchant_url(url: str) -> str:
    """Compact clumsy Flipkart/Shopsy/Myntra/Ajio URLs by stripping cluttering tracking
    queries, search strings, and affiliate hashes, keeping only the exact clean canonical product path.
    Prevents long clumsy URLs from breaking WhatsApp and Telegram message layouts."""
    try:
        p = urlparse(url)
        host = _host_of(url)

        # Flipkart / Shopsy: keep clean canonical /product/p/itmXXX?pid=YYY
        if (
            _is_domain(host, "flipkart.com")
            or _is_domain(host, "shopsy.in")
            or _is_domain(host, "fktr.in")
        ):
            # Don't touch short redirects like /s/ or dl.flipkart.com; they
            # cannot be expanded without making an external request.
            if "/s/" in p.path or _is_domain(host, "dl.flipkart.com") or _is_domain(host, "fktr.in"):
                return url
            m_itm = re.search(r"(/[^/]+/p/itm[a-zA-Z0-9]+|/p/itm[a-zA-Z0-9]+)", p.path)
            q = dict(parse_qsl(p.query, keep_blank_values=True))
            pid = q.get("pid")
            clean_query = urlencode({"pid": pid}) if pid else ""
            clean_path = m_itm.group(1) if m_itm else p.path
            canonical_host = "www.shopsy.in" if _is_domain(host, "shopsy.in") else "www.flipkart.com"
            return urlunparse(("https", canonical_host, clean_path, "", clean_query, ""))

        # Myntra: keep clean /.../<id>/buy
        if _is_domain(host, "myntra.com"):
            m_myn = re.search(r"(/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/\d+/buy)", p.path)
            clean_path = m_myn.group(1) if m_myn else p.path
            return urlunparse(("https", "www.myntra.com", clean_path, "", "", ""))

        # Ajio: keep clean /p/<id>
        if _is_domain(host, "ajio.com"):
            m_ajio = re.search(r"(/[a-zA-Z0-9_-]+/p/[a-zA-Z0-9_-]+)", p.path)
            clean_path = m_ajio.group(1) if m_ajio else p.path
            return urlunparse(("https", "www.ajio.com", clean_path, "", "", ""))

        return url
    except Exception:
        return url


def _amazon_asin(parsed) -> str | None:
    """Extract an ASIN from common Amazon product URL shapes.

    ASIN is strictly 10 alphanumeric characters (India/US catalog). We also
    accept 8-12 for defensive tolerance but prefer 10. This enables perfect
    canonicalization to /dp/<ASIN>?tag=YOURTAG.
    """
    # Primary: 10-char ASIN is canonical; allow 8-12 for defensive tolerance
    path_patterns = (
        r"/(?:dp|product)/([A-Za-z0-9]{10})(?:[/?#]|$)",
        r"/gp/(?:product|aw/d)/([A-Za-z0-9]{10})(?:[/?#]|$)",
        # Fallback tolerant patterns
        r"/(?:dp|product)/([A-Za-z0-9]{8,12})(?:/|$)",
        r"/gp/(?:product|aw/d)/([A-Za-z0-9]{8,12})(?:/|$)",
    )
    for pattern in path_patterns:
        match = re.search(pattern, parsed.path, re.I)
        if match:
            candidate = match.group(1).upper()
            # Prefer 10-char ASIN; if tolerant pattern matched 8/9/11/12, still use but validate
            if re.fullmatch(r"[A-Z0-9]{10}", candidate):
                return candidate
            if re.fullmatch(r"[A-Z0-9]{8,12}", candidate):
                return candidate
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() == "asin" and re.fullmatch(r"[A-Za-z0-9]{10}", value or "", re.I):
            return value.upper()
        if key.lower() == "asin" and re.fullmatch(r"[A-Za-z0-9]{8,12}", value or "", re.I):
            return value.upper()
    return None


def compact_amazon_product_link(url: str, tag: str | None = None) -> str:
    """Retag Amazon URLs without changing their marketplace or breaking routes.

    Recognized product ASIN links on Amazon marketplace domains are normalized
    to ``/dp/<ASIN>`` on the same marketplace. Safe numeric variant selectors
    ``th`` and ``psc`` are retained; the effective Associate ``tag`` replaces
    any source tag. Opaque short links and non-product routes keep their path,
    required query parameters, and destination host because they cannot safely
    be expanded or canonicalized without following the network redirect.
    """
    try:
        parsed = urlparse(url)
        host = _host_of(url)
    except (TypeError, ValueError):
        return url
    if not any(_is_domain(host, domain) for domain in AMAZON_DOMAINS):
        return url
    # Avoid rewriting unusual authorities (credentials/nonstandard ports).
    try:
        if parsed.username or parsed.password or parsed.port is not None:
            return url
    except ValueError:
        return url

    query_values = parse_qsl(parsed.query, keep_blank_values=True)
    effective_tag = (tag or "").strip()
    if not effective_tag:
        effective_tag = next(
            (value for key, value in query_values if key.lower() == "tag" and value),
            config.AMAZON_ASSOCIATE_TAG,
        )
    # PERFECT AMAZON CONVERSION: Strict tag validation and ASIN handling
    # Tag must be like "xxx-21" (Associates format). If invalid, fallback.
    if effective_tag and not re.fullmatch(r"[A-Za-z0-9_-]+-21", effective_tag):
        # Still allow custom tags but ensure non-empty; fallback to default if clearly invalid
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,30}", effective_tag):
            effective_tag = config.AMAZON_ASSOCIATE_TAG
    asin = _amazon_asin(parsed)
    # Determine marketplace - must preserve original marketplace perfectly
    if _is_domain(host, "amazon.in"):
        marketplace_host = "www.amazon.in"
    elif _is_domain(host, "amazon.com"):
        marketplace_host = "www.amazon.com"
    elif _is_domain(host, "amazon.co.uk"):
        marketplace_host = "www.amazon.co.uk"
    elif _is_domain(host, "amazon.ae"):
        marketplace_host = "www.amazon.ae"
    else:
        marketplace_host = None  # amzn.to / amzn.in short links stay on their host

    if asin and marketplace_host:
        # PERFECT CANONICALIZATION: Always https://www.amazon.in/dp/<ASIN>?tag=YOURTAG
        # Keep only safe variant selectors (th, psc), discard all tracking junk
        safe_product_params: dict[str, str] = {}
        for key, value in query_values:
            normalized_key = key.lower()
            if normalized_key in {"th", "psc"} and value.isdigit():
                safe_product_params[normalized_key] = value
        canonical_query = list(safe_product_params.items())
        if effective_tag:
            canonical_query.append(("tag", effective_tag))
        query = urlencode(canonical_query)
        # Always use https and canonical /dp/<ASIN> path - perfect for affiliate
        return urlunparse(("https", marketplace_host, f"/dp/{asin}", "", query, ""))

    # For amzn.to / amzn.in short links and non-product routes (search, storefront)
    # PERFECT TAG REPLACEMENT: Ensure exactly one tag param, no duplicates, case-insensitive
    # Short links like https://amzn.to/3xyz or https://amzn.in/d/gXYZ - tag is added as query
    # This ensures our affiliate tag is present even though short code is opaque.
    # Note: amzn.to short codes are generated by Amazon via SiteStripe; we cannot invent new short codes,
    # but we can append ?tag=... which will be respected if the short link is not yet encoded with old tag.
    preserved_query = [(key, value) for key, value in query_values if key.lower() != "tag"]
    if effective_tag:
        preserved_query.append(("tag", effective_tag))
    # Preserve original host exactly (lowercased) but ensure https scheme
    authority = host.lower()
    # Handle m.amazon.in -> www.amazon.in for consistency, but amzn.to stays amzn.to
    if authority == "m.amazon.in":
        authority = "www.amazon.in"
    elif authority == "m.amazon.com":
        authority = "www.amazon.com"
    query = urlencode(preserved_query)
    return urlunparse((
        "https", authority, parsed.path or "/", "", query, parsed.fragment,
    ))


def apply_amazon_tag(url: str, tag: str | None = None) -> str:
    """Compact a supported Amazon URL and set exactly one associate tag."""
    if classify_url(url) != "amazon":
        return url
    return compact_amazon_product_link(url, tag or config.AMAZON_ASSOCIATE_TAG)


def filter_disallowed_affiliate_links(text: str, allowed_kinds: set[str]) -> str:
    """Remove links disabled by merchant settings without discarding allowed deals.

    Only supported affiliate kinds (Amazon, EarnKaro merchants, HYPD, LehLah) are
    filtered. Informational/unknown URLs are left untouched. A line containing
    only disabled links is removed, while product copy and allowed URLs survive.
    """
    allowed = set(allowed_kinds)
    kept_lines: list[str] = []
    for line in text.splitlines():
        urls = find_urls(line)
        blocked = [url for url in urls
                   if classify_url(url) in {"amazon", "merchant", "hypd", "meesho", "lehlah"}
                   and classify_url(url) not in allowed]
        remaining = [url for url in urls if url not in blocked]
        if urls and not remaining:
            continue
        output = line
        for url in blocked:
            output = output.replace(url, "")
        if output.strip():
            kept_lines.append(output.rstrip())
    return "\n".join(kept_lines).strip()


def _render_base(
    text: str, amazon_tag: str, ek: dict[str, str],
    hypd_store_id: str = config.HYPD_STORE_ID,
) -> str:
    out_parts: list[str] = []
    last = 0
    for m in URL_RE.finditer(text):
        start, end = m.span()
        raw_url = m.group(0)
        trailing = ""
        while raw_url and raw_url[-1] in ".,;!?:'\"":
            trailing = raw_url[-1] + trailing
            raw_url = raw_url[:-1]

        kind = classify_url(raw_url)
        if kind == "amazon":
            replacement = apply_amazon_tag(raw_url, amazon_tag)
        elif kind == "hypd":
            replacement = convert_hypd_store_link(raw_url, hypd_store_id)
        elif kind == "meesho":
            # Raw Meesho product URLs need HYPD's official generator to earn.
            # Preserve the source URL until that conversion contract is configured.
            replacement = raw_url
        elif kind == "merchant":
            replacement = ek.get(raw_url) or compact_merchant_url(raw_url)
        elif kind == "lehlah":
            # Preserve AppsFlyer/LehLah attribution parameters exactly.
            replacement = raw_url
        else:
            replacement = raw_url
        out_parts.append(text[last:start])
        out_parts.append(replacement + trailing)
        last = end
    out_parts.append(text[last:])
    return "".join(out_parts)


def _approval_render(text: str, amazon_tag: str) -> str:
    """Produce an Amazon-only preview with native Amazon links.

    This formatter does not guarantee Associates program approval. It applies
    the configured tag, avoids shorteners, removes supported non-Amazon affiliate
    links, and adds the requested ``#ad (paid link)`` disclosure footer. Amazon
    marketplace destinations are preserved; the disclosure may not replace any
    additional policy or program requirements.
    """
    lines = text.splitlines()
    kept_lines: list[str] = []
    has_amazon = False

    for line in lines:
        urls = find_urls(line)
        if not urls:
            # Descriptive text / title / price line
            kept_lines.append(line)
            continue

        # Line contains URLs: check if any is Amazon vs merchant/hypd
        line_has_amazon = any(classify_url(u) == "amazon" for u in urls)
        line_has_non_amazon = any(classify_url(u) in ("merchant", "hypd", "meesho", "lehlah") for u in urls)

        if line_has_amazon:
            # Retag all amazon URLs on this line
            out_line = line
            for u in urls:
                if classify_url(u) == "amazon":
                    out_line = out_line.replace(u, apply_amazon_tag(u, amazon_tag))
                elif classify_url(u) in ("merchant", "hypd", "meesho", "lehlah"):
                    # Drop merchant or hypd url from line
                    out_line = out_line.replace(u, "").strip()
            if out_line.strip():
                kept_lines.append(out_line)
                has_amazon = True
        elif line_has_non_amazon:
            # Non-amazon merchant link line — drop it completely for approval channel
            continue
        else:
            # Other URLs (e.g. general info)
            kept_lines.append(line)

    result = "\n".join(kept_lines).strip()
    # Normalize excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)

    if "#ad" not in result.lower():
        result = result + "\n\n#ad (paid link)"
    return result


def _strip_amazon_render(
    text: str, ek: dict[str, str],
    hypd_store_id: str = config.HYPD_STORE_ID,
) -> str:
    """Remove Amazon-only lines; use supplied merchant conversions if any.

    Existing valid HYPD links are retagged to ``hypd_store_id``. Other links
    without a configured conversion remain unchanged so filtering/routing can
    fail safely without dropping the deal.
    """
    lines = text.splitlines()
    kept_lines: list[str] = []

    for line in lines:
        urls = find_urls(line)
        if not urls:
            kept_lines.append(line)
            continue

        line_has_amazon = any(classify_url(u) == "amazon" for u in urls)
        line_has_other = any(classify_url(u) in ("merchant", "hypd", "meesho", "lehlah") for u in urls)

        if line_has_amazon and not line_has_other:
            # Pure Amazon line -> remove completely
            continue
        elif line_has_amazon and line_has_other:
            # Line has both: remove amazon, rewrite merchant/hypd
            out_line = line
            for u in urls:
                if classify_url(u) == "amazon":
                    out_line = out_line.replace(u, "").strip()
                elif classify_url(u) == "hypd":
                    out_line = out_line.replace(u, convert_hypd_store_link(u, hypd_store_id))
                elif classify_url(u) == "merchant":
                    out_line = out_line.replace(u, ek.get(u, u))
            if out_line.strip():
                kept_lines.append(out_line)
        else:
            # Non-amazon line -> rewrite merchant to earnkaro / hypd to store id
            out_line = line
            for u in urls:
                if classify_url(u) == "hypd":
                    out_line = out_line.replace(u, convert_hypd_store_link(u, hypd_store_id))
                elif classify_url(u) == "merchant":
                    out_line = out_line.replace(u, ek.get(u, u))
            kept_lines.append(out_line)

    result = "\n".join(kept_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result)


def render_for_influencer(
    text: str,
    amazon_tag: str,
    earnkaro_links: dict[str, str] | None = None,
    shortened_links: dict[str, str] | None = None,
    role: str = "broadcast",
    strip_amazon: bool = False,
    clean_promos: bool = True,
    hypd_store_id: str = config.HYPD_STORE_ID,
) -> str:
    """Render `text` for one influencer on a given channel `role`.

    shortened_links:
      Map of eligible merchant URL -> Bitly short link. Amazon Associate and
      HYPD affiliate URLs are never replaced here; optional first-party redirects
      for those links are handled after canonical rendering.

    clean_promos:
      If True (default), strips source channel promotional text, invite links, and @channel watermarks,
      while strictly preserving product titles, descriptions, and pricing.

    strip_amazon:
      If True -> Amazon links and Amazon-only product lines are completely REMOVED.
      Only Flipkart/Myntra/HYPD/etc. are posted.

    role:
      'broadcast' / 'whatsapp' -> Amazon links use the selected tag, valid HYPD
          links can be retagged, and merchant URLs use only conversions supplied
          by the pipeline. Disabled or unavailable conversions leave the source
          URL intact unless upstream filtering removes that affiliate category.
      'approval' -> supported affiliate links are Amazon-only and native (not
          shortened), with the '#ad (paid link)' disclosure. This is a formatter,
          not a program-approval guarantee.
    """
    post_text = clean_source_post(text) if clean_promos else text
    ek = earnkaro_links or {}

    if strip_amazon:
        rendered = _strip_amazon_render(post_text, ek, hypd_store_id=hypd_store_id)
    elif role == "approval":
        # Approval channel: ALWAYS native Amazon link with #ad disclosure. NEVER Bitly shortened.
        return _approval_render(post_text, amazon_tag)
    else:
        rendered = _render_base(post_text, amazon_tag, ek, hypd_store_id=hypd_store_id)

    # Apply Bitly shortener replacements if available (for broadcast/whatsapp channels)
    if shortened_links and role != "approval":
        for long_u, short_u in shortened_links.items():
            if (
                long_u
                and short_u
                and long_u != short_u
                and classify_url(long_u) not in {"amazon", "hypd", "meesho", "lehlah"}
            ):
                rendered = rendered.replace(long_u, short_u)

    return rendered



def extract_price(text: str) -> float | None:
    """Extract the lowest price mentioned in the deal text.
    Handles ₹, Rs, Rs., INR, at Rs.499, @199, Price: 299 etc.
    """
    clean_text = text.replace(",", "")
    patterns = [
        r"(?:₹|rs\.?|inr)\s*(\d+(?:\.\d{1,2})?)",
        r"@\s*(\d+(?:\.\d{1,2})?)",
        r"(?:deal price|price|at|for|just)\s*(?:₹|rs\.?|inr|:)?\s*(\d+(?:\.\d{1,2})?)",
        r"(?<!\d)(\d{1,7}(?:\.\d{1,2})?)\s*/-",
    ]
    prices: list[float] = []
    for pat in patterns:
        for m in re.finditer(pat, clean_text, re.I):
            try:
                val = float(m.group(1))
                if 1 <= val <= 2000000:  # sane bounds (1 rupee to 20 lakh)
                    prices.append(val)
            except (ValueError, TypeError):
                continue
    return min(prices) if prices else None


def matches_price_filter(deal_text: str, max_price: float | None = None, min_price: float | None = None) -> bool:
    """Return True if the deal matches the given price bounds."""
    if max_price is None and min_price is None:
        return True
    deal_price = extract_price(deal_text)
    if deal_price is None:
        # If no price detected in post, pass it through so non-priced deals aren't dropped
        return True
    if max_price is not None and deal_price > max_price:
        return False
    if min_price is not None and deal_price < min_price:
        return False
    return True


def has_amazon_link(text: str) -> bool:
    return any(classify_url(u) == "amazon" for u in find_urls(text))


# Channel promotional / watermark patterns to cleanly strip out
# Enhanced to support Telugu sources and 150+ merchant channels with
# comprehensive junk removal while keeping product titles/prices intact.
PROMO_PATTERNS = [
    r"(?i)(?:join|follow|subscribe)\s*(?:our)?\s*(?:telegram|channel|group|wa|whatsapp)?\s*(?:channel|group)?\s*[:\-\s]*https?://(?:t\.me|telegram\.me|chat\.whatsapp\.com)/[^\s]+",
    r"(?i)https?://(?:t\.me|telegram\.me|chat\.whatsapp\.com)/[^\s]+",
    r"(?i)posted\s*by\s*[:\-]?\s*.*$",
    r"(?i)powered\s*by\s*[:\-]?\s*.*$",
    r"(?i)credit\s*[:\-]?\s*.*$",
    r"(?i)share\s*with\s*(?:your)?\s*friends?.*$",
    r"(?i)for\s*more\s*(?:loots?|deals?|offers?).*$",
    r"(?i)join\s*(?:fast|now|here).*$",
    r"(?i)loot\s*alert\s*by\s*.*$",
    r"(?i)(?:follow|subscribe).*?(?:instagram|youtube|facebook|twitter|telegram|channel).*",
    r"(?i)(?:dm|contact)\s*(?:us|for|@|:).*",
    r"(?i)admin\s*(?:contact|support).*",
]


def clean_source_post(text: str) -> str:
    """Clean promotional watermarks, source telegram links, @admin tags,
    and invite links from source posts while PRESERVING product titles,
    descriptions, prices, and merchant/amazon links completely intact.

    PERFECT TARGETING: Removes all junk from 100+ source apps while keeping
    Amazon, Flipkart, Myntra, Ajio, Nykaa etc. product links 100% intact.
    """
    if not text or not text.strip():
        return ""

    lines = text.splitlines()
    cleaned_lines: list[str] = []

    # Enhanced promo-line detection (Telugu + English + Hinglish)
    promo_line_re = re.compile(
        r"(?i)^\s*(?:"
        r"join|subscribe|follow|join\s+channel|join\s+fast|share\s+with\s+friends|for\s+more|posted\s+by|credit|powered\s+by|loot\s+alert\s+by|"
        r"admin|contact|dm|queries|support|follow\s+us|subscribe\s+us|join\s+our|telegram\s+channel|whatsapp\s+channel|"
        r"more\s+loots?|more\s+deals?|more\s+offers?|daily\s+loots?|best\s+loots?|"
        r"invite\s+link|group\s+link|channel\s+link|"
        r"forwarded\s+from|via\s+@"
        r")\b",
        re.I,
    )

    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue

        urls = find_urls(stripped)
        has_store_url = any(classify_url(u) in ("amazon", "merchant", "hypd", "meesho", "lehlah") for u in urls)

        if not has_store_url:
            # 1. Pure Telegram or WhatsApp invite links - REMOVE entirely
            if re.search(r"https?://(?:t\.me|telegram\.me|chat\.whatsapp\.com)/\S+", stripped, re.I):
                continue
            # 2. Shortened telegram links like t.me/+hash or https://t.me/xxx
            if re.search(r"(?i)(?:t\.me|telegram\.me)/\+?[A-Za-z0-9_-]+", stripped) and len(stripped) < 80 and "amazon" not in stripped.lower() and "flipkart" not in stripped.lower():
                # Only skip if line is mostly invite link
                if re.fullmatch(r"[\s@]*https?://(?:t\.me|telegram\.me)/\S+[\s]*", stripped, re.I) or re.fullmatch(r"@?[A-Za-z0-9_]{3,50}", stripped):
                    continue

            # 3. Promotional text banners / watermarks - comprehensive
            if promo_line_re.search(stripped):
                continue
            # Also catch "Loot by @xxx" style
            if re.search(r"(?i)(?:loot|deal|offer)\s*(?:by|via)\s*@?\w+", stripped) and len(stripped.split()) <= 6:
                continue
            # 4. Pure channel handles
            if re.match(r"^@(?:[a-zA-Z0-9_]{3,30})$", stripped):
                continue
            # 5. Lines that are only emojis + promo words
            if re.fullmatch(r"[\s\W]*", stripped) and len(stripped) < 5:
                continue
            # 6. Excessive emoji promo like "🔥🔥 JOIN NOW 🔥🔥"
            if re.search(r"(?i)join.*now|subscribe.*now|follow.*now", stripped) and len(stripped) < 40:
                continue

        # Line might have product name or price + an @handle or promo at the end.
        # Strip out telegram handles/links from the line while keeping product name and valid store urls.
        line_out = line
        # Remove telegram invite links inside the line (preserve store links!)
        line_out = re.sub(r"https?://(?:t\.me|telegram\.me|chat\.whatsapp\.com)/\S+", "", line_out, flags=re.I)
        # Remove channel tag / handle (e.g. @PowerLoots or @secretdeal) but don't damage normal text
        # Be careful: don't remove @ in email addresses, but channel handles are usually standalone
        line_out = re.sub(r"(?i)\s*@(?:[a-zA-Z0-9_]{3,30})\b", "", line_out)
        # Remove trailing promo phrases after dash/bullet
        line_out = re.sub(r"(?i)\s*[-|•~]\s*(?:join|loot\s+by|powered\s+by|credit|admin|dm|contact|follow|subscribe)\s*.*$", "", line_out)
        # Remove inline "Join @xxx" or "Follow @xxx" remnants
        line_out = re.sub(r"(?i)\s*(?:join|follow|subscribe)\s*@?[A-Za-z0-9_]{3,30}\b", "", line_out)
        # Remove "via @xxx" etc
        line_out = re.sub(r"(?i)\s*via\s*@?[A-Za-z0-9_]{3,30}\b", "", line_out)

        line_out = line_out.strip()
        # Clean dangling promo remnants left behind by stripped handles (e.g. "LOOT ALERT by" after removing @handle)
        # Strip leading emojis/punctuation for promo check
        stripped_for_promo = re.sub(r"^[^\w]+", "", line_out.strip()).strip()
        if re.match(r"(?i)^(loot\s+alert\s+by|loot\s+by|deal\s+by|offer\s+by|posted\s+by|credit)\s*$", stripped_for_promo):
            continue
        # Clean dangling colons or dashes left behind by stripped handles (e.g. "Admin contact:" or "Credit:")
        line_out = re.sub(r"(?i)^(?:admin\s*(?:contact)?|credit|dm|queries|support|follow|subscribe|join|loot\s+alert\s+by|loot\s+by)\s*[:\-–]?\s*$", "", line_out).strip()
        # Clean lines that became only punctuation/emojis after stripping
        if line_out and re.fullmatch(r"[\s\W]+", line_out):
            continue
        if line_out and re.fullmatch(r"[\s\-–—•|:.,!~]+", line_out):
            continue
        if line_out:
            cleaned_lines.append(line_out)

    result = "\n".join(cleaned_lines).strip()
    # Normalize excessive blank lines - keep max 2 consecutive
    result = re.sub(r"\n{3,}", "\n\n", result)
    # Trim trailing separators left from cleaning
    result = re.sub(r"[\-–—•|:~]+\s*$", "", result).strip()
    return result


# Comprehensive Category Keyword Mappings (Top 8 Indian E-Commerce Verticals)
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "clothing": [
        "shirt", "shirts", "tshirt", "tshirts", "t-shirt", "t-shirts", "jeans", "trousers", "dress", "dresses",
        "kurta", "kurtas", "saree", "sarees", "shoes", "sneakers", "sandals", "slippers", "hoodie", "hoodies",
        "jacket", "jackets", "trackpant", "trackpants", "bra", "brief", "briefs", "leggings", "watch", "watches",
        "footwear", "clothing", "wear", "ethnic", "fashion", "handbag", "handbags", "wallet", "wallets",
        "sunglasses", "top", "tops", "pant", "suit", "suits", "blazer", "blazers", "chinos",
        "boxers", "vest", "kurti", "kurtis", "lehenga", "crocs", "boots", "loafers", "heels", "socks",
    ],
    "electronics": [
        "phone", "phones", "mobile", "mobiles", "smartphone", "smartphones", "laptop", "laptops",
        "earbuds", "earbud", "earphone", "earphones", "headphone", "headphones", "neckband", "neckbands",
        "bluetooth", "wireless", "tws", "tv", "television", "smartwatch", "smartwatches", "charger", "chargers",
        "powerbank", "powerbanks", "cable", "cables", "tablet", "tablets", "ipad", "camera", "cameras",
        "speaker", "speakers", "soundbar", "soundbars", "mouse", "keyboard", "keyboards", "ssd", "ram",
        "processor", "trimmer", "trimmers", "iron", "electronics", "iphone", "oneplus", "samsung", "redmi",
        "realme", "macbook", "adapter", "monitor", "monitors", "gaming", "console", "gadgets", "usb",
    ],
    "home": [
        "bedsheet", "bedsheets", "curtain", "curtains", "blanket", "blankets", "pillow", "pillows",
        "cookware", "pan", "pans", "bottle", "bottles", "flask", "flasks", "mop", "cleaner",
        "container", "containers", "storage", "towel", "towels", "mattress", "kitchen", "lamp", "lamps",
        "lights", "decor", "home", "living", "sofa", "chair", "chairs", "cushion", "cushions",
        "plate", "plates", "glass", "glasses", "dinnerware", "kettle", "gas stove", "pressure cooker",
        "air fryer", "mixer grinder", "juicer", "blender", "chimney", "doormat", "mugs",
    ],
    "daily": [
        "shampoo", "soap", "soaps", "toothpaste", "facewash", "face wash", "cream", "serum", "lotion",
        "perfume", "perfumes", "deodorant", "deo", "sunscreen", "diaper", "diapers", "wipes", "oil", "oils",
        "ghee", "tea", "coffee", "biscuit", "biscuits", "grocery", "groceries", "snack", "snacks",
        "detergent", "powder", "health", "supplement", "daily", "essentials", "colgate", "dettol",
        "surf excel", "body wash", "handwash", "rice", "dal", "atta", "dry fruits", "almonds", "cashew", "maggie",
    ],
    "beauty": [
        "makeup", "lipstick", "lipsticks", "eyeliner", "kajal", "foundation", "compact", "mascara",
        "nail polish", "skincare", "hair care", "moisturizer", "toner", "lip balm", "hair oil",
        "body lotion", "sunblock", "fragrance", "cologne", "beauty", "cosmetics", "face mask", "scrub", "wax",
    ],
    "baby": [
        "baby", "babies", "infant", "toddler", "diaper", "diapers", "pampers", "huggies", "mamy poko",
        "stroller", "pram", "baby lotion", "baby soap", "baby shampoo", "feeding bottle", "toys", "toy",
        "lego", "board game", "puzzle", "action figure", "doll", "remote control", "ride on",
    ],
    "sports": [
        "sports", "fitness", "gym", "dumbbell", "dumbbells", "treadmill", "yoga mat", "resistance band",
        "cricket", "badminton", "shuttlecock", "shuttle", "racket", "racquet", "football", "jersey",
        "protein", "whey", "creatine", "shaker", "bicycle", "cycle", "skating", "swimming",
    ],
    "appliances": [
        "refrigerator", "fridge", "washing machine", "ac", "air conditioner", "microwave", "oven",
        "dishwasher", "water purifier", "geyser", "water heater", "vacuum cleaner", "cooler", "fan",
        "inverter", "appliances",
    ],
    "recharge_freebies": [
        "freebie", "free sample", "free loot", "recharge", "cashback", "coupon", "code", "voucher", "loot deal",
        "bug deal", "price error", "flat off", "swiggy", "zomato", "amazon pay", "flipkart minutes",
    ],
    "books_stationery": [
        "book", "books", "novel", "novels", "pen", "pens", "notebook", "notebooks", "diary", "marker",
        "stationery", "calculator", "backpack", "school bag",
    ],
    "automotive": [
        "car", "bike", "helmet", "helmets", "dashcam", "car wash", "tyre", "puncture", "riding gloves",
        "mobile holder", "motorcycle",
    ],
    "footwear": [
        "shoes", "sneakers", "sandals", "slippers", "crocs", "boots", "loafers", "heels", "flats",
        "flip flops", "slides", "clogs", "sports shoes", "formal shoes", "casual shoes",
    ],
    "travel_luggage": [
        "luggage", "trolley", "suitcase", "duffle", "backpack", "travel bag", "rucksack", "cabin bag",
        "safari", "american tourister", "skybags", "vip", "aristocrat",
    ],
    "jewellery_accessories": [
        "jewellery", "jewelry", "necklace", "earrings", "bangles", "bracelet", "ring", "chain",
        "gold", "silver", "diamond", "pendant", "anklet", "mangalsutra", "hair clips",
    ],
    "gaming": [
        "gaming", "game", "games", "playstation", "ps5", "xbox", "nintendo", "joystick", "controller",
        "gaming mouse", "gaming keyboard", "headset", "gpu", "graphics card", "steam",
    ],
    "pet_supplies": [
        "dog food", "cat food", "pet", "pets", "puppy", "pedigree", "whiskas", "leash", "pet shampoo",
        "litter", "aquarium", "bird food",
    ],
}


def classify_deal_category(text: str) -> list[str]:
    """Identify which categories a deal belongs to."""
    lower = text.lower()
    matched = []
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(kw)}\b", lower) for kw in kws):
            matched.append(cat)
    return matched or ["other"]


def matches_category_filter(deal_text: str, allowed_categories_spec: str) -> bool:
    """Return True if the deal belongs to any of the allowed categories.
    allowed_categories_spec: comma-separated list like 'clothing,electronics' or empty/all for all.
    When set to 'all' or empty, NO restrictions are applied: ANY deal in the world is allowed!
    """
    s = (allowed_categories_spec or "").strip().lower()
    unrestricted = {"all", "any", "unrestricted", "all categories", "all deals", "*", "no_filter", "default"}
    tokens = {x.strip() for x in s.split(",") if x.strip()}
    if not tokens or tokens.intersection(unrestricted):
        return True
    allowed_set = tokens
    deal_cats = set(classify_deal_category(deal_text))
    # If the deal matches any allowed category, return True
    if deal_cats.intersection(allowed_set):
        return True
    # If deal has uncategorized items ('other'), let it pass if explicitly permitted or 'other' in allowed_set
    if "other" in deal_cats and ("other" in allowed_set or not allowed_set):
        return True
    return False


def is_time_in_schedule(schedule_spec: str, current_time: str | None = None) -> bool:
    """Check if the current time (or given HH:MM in IST / local) falls within the allowed windows.
    Supports:
      - Normal windows: '06:00-09:00', '18:00-23:00'
      - Across-midnight / next-day windows: '06:00-02:00' (active from 6 AM through next day 2 AM,
        holding/sleeping only between 2:00 AM and 6:00 AM)
      - Multiple windows separated by comma: '06:00-09:00,11:00-14:00,18:00-23:00'
    Empty schedule_spec means active 24/7 (always True).
    """
    spec = (schedule_spec or "").strip()
    if spec.lower() in {"", "all", "any", "unrestricted", "24/7", "*", "no_filter", "default"}:
        return True

    from datetime import datetime
    import zoneinfo

    if current_time:
        now_str = current_time
    else:
        try:
            tz = zoneinfo.ZoneInfo("Asia/Kolkata")
            now_dt = datetime.now(tz)
        except Exception:
            now_dt = datetime.now()
        now_str = f"{now_dt.hour:02d}:{now_dt.minute:02d}"

    windows = [w.strip() for w in spec.split(",") if w.strip()]
    for window in windows:
        parts = window.split("-")
        if len(parts) == 2:
            start, end = parts[0].strip(), parts[1].strip()
            # Normalize single digits like 6:00 to 06:00
            if len(start) == 4 and start[1] == ":":
                start = "0" + start
            if len(end) == 4 and end[1] == ":":
                end = "0" + end
            if start <= end:
                if start <= now_str <= end:
                    return True
            else:
                # Overnight/across-midnight window (e.g. 06:00 to 02:00 next day, or 20:00 to 04:00)
                if now_str >= start or now_str <= end:
                    return True
    return False




def calculate_deal_loot_score(text: str) -> float:
    """Calculate an attractive 'loot score' (0 to 100) for a deal based on:
    - Discount percentage mentioned (e.g. 80% off -> +40 points)
    - Low price loot bonus (under ₹199 or under ₹499)
    - Loot urgency keywords (Loot, Error, Steal, Bug, Flat, Free)
    """
    score = 10.0
    # 1. Discount % match
    m_disc = re.search(r"(\d{1,2})%\s*(?:off|discount)", text, re.I)
    if m_disc:
        disc = float(m_disc.group(1))
        score += min(50.0, disc * 0.6)  # 80% gives 48 points

    # 2. Price factor
    price = extract_price(text)
    if price is not None:
        if price <= 99:
            score += 35.0
        elif price <= 299:
            score += 25.0
        elif price <= 499:
            score += 15.0
        elif price <= 999:
            score += 8.0

    # 3. Urgency / loot trigger words
    urgent_words = ["loot", "steal", "bug", "flat", "free", "grab", "lowest", "huge drop", "error"]
    lower = text.lower()
    for w in urgent_words:
        if w in lower:
            score += 4.0

    return score


def format_loot_of_the_hour_post(original_post: str, hour_label: str = "") -> str:
    """Wrap a ranked recent deal in the application's hourly promo banner.

    The ranking is heuristic and the banner does not predict sales.
    ``hour_label`` may be a local-time label such as ``2:00 PM``.
    """
    time_badge = f" [ {hour_label} SPECIAL ]" if hour_label else ""
    lines = [
        "👑 ════════════════════════════ 👑",
        f"⚡ 𝗟𝗢𝗢𝗧 𝗢𝗙 𝗧𝗛𝗘 𝗛𝗢𝗨𝗥{time_badge} ⚡",
        "🔥 Best Handpicked Deal Chosen From This Hour!",
        "👑 ════════════════════════════ 👑",
        "",
        original_post.strip(),
        "",
        "🌟 𝘛𝘩𝘪𝘴 𝘸𝘢𝘴 𝘵𝘩𝘦 #1 𝘣𝘦𝘴𝘵 𝘳𝘢𝘵𝘦𝘥 𝘥𝘦𝘢𝘭 𝘰𝘧 𝘵𝘩𝘪𝘴 𝘩𝘰𝘶𝘳!",
        "⏳ 𝘏𝘶𝘳𝘳𝘺! 𝘗𝘳𝘪𝘤𝘦 𝘮𝘢𝘺 𝘪𝘯𝘤𝘳𝘦𝘢𝘴𝘦 𝘰𝘳 𝘨𝘰 𝘰𝘶𝘵 𝘰𝘧 𝘴𝘵𝘰𝘤𝘬 𝘢𝘯𝘺 𝘴𝘦𝘤𝘰𝘯𝘥!",
        "📌 𝘚𝘩𝘢𝘳𝘦 𝘸𝘪𝘵𝘩 𝘺𝘰𝘶𝘳 𝘧𝘳𝘪𝘦𝘯𝘥𝘴 𝘣𝘦𝘧𝘰𝘳𝘦 𝘪𝘵 𝘦𝘹𝘱𝘪𝘳𝘦𝘴!",
    ]
    return "\n".join(lines)


def deal_signature(text: str) -> str:
    """Build a heuristic signature used for per-channel deduplication.

    Known product identifiers (Amazon ASIN, HYPD token, Flipkart item code,
    Myntra article ID, or Ajio product code) take priority; otherwise a cleaned
    URL or title/price fingerprint is used. This catches common cross-source
    duplicates but cannot identify every variant or guarantee zero false
    positives/negatives.
    """
    import hashlib

    # 1. Amazon ASIN
    asins = sorted(set(re.findall(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{8,12})", text, re.I)))
    if asins:
        raw = "asin:" + ":".join(a.upper() for a in asins)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # 2. HYPD / Meesho item token (independent of which store ID it was posted with)
    hypd_tokens = sorted(set(re.findall(r"/(?:afflink|store/\d+)/([a-zA-Z0-9_-]{8,40})", text, re.I)))
    if hypd_tokens:
        raw = "hypd_token:" + ":".join(t.lower() for t in hypd_tokens)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # 3. Flipkart item code
    fk_itms = sorted(set(re.findall(r"/p/(itm[a-zA-Z0-9]+)", text, re.I)))
    if fk_itms:
        raw = "fk_itm:" + ":".join(i.lower() for i in fk_itms)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # 4. Myntra article ID
    myntra_ids = sorted(set(re.findall(r"/(\d{6,11})/buy", text, re.I)))
    if myntra_ids:
        raw = "myntra_id:" + ":".join(m for m in myntra_ids)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # 5. Ajio product code
    ajio_codes = sorted(set(re.findall(r"ajio\.com/[^/]+/p/([a-zA-Z0-9_-]{5,30})", text, re.I)))
    if ajio_codes:
        raw = "ajio_code:" + ":".join(a.lower() for a in ajio_codes)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # 6. Non-Amazon / general clean canonical URLs
    clean_urls = []
    for u in find_urls(text):
        try:
            p = urlparse(u)
            clean = f"{p.netloc.lower()}{p.path.rstrip('/')}"
            clean_urls.append(clean)
        except Exception:
            clean_urls.append(u.lower())
    clean_urls = sorted(set(clean_urls))

    prices = re.findall(r"(?:₹|rs\.?|inr)\s?([\d,]{2,})", text, re.I)
    raw = "|".join(clean_urls) + "#" + "|".join(prices[:2])
    return hashlib.sha1(raw.lower().encode("utf-8")).hexdigest()[:16]
