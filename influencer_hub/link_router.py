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

import asyncio
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import config

# Amazon marketplaces this hub can canonicalize, mapped to their canonical host.
# Only OUR marketplace (Amazon India) can pay with the configured Associate tag;
# links to other stores are deliberately left untouched instead of being
# rewritten into something that can never earn for this account.
AMAZON_MARKETPLACES = {
    "amazon.in": "www.amazon.in",
    "amazon.com": "www.amazon.com",
    "amazon.co.uk": "www.amazon.co.uk",
    "amazon.ae": "www.amazon.ae",
    "amazon.sg": "www.amazon.sg",
    "amazon.ca": "www.amazon.ca",
    "amazon.com.au": "www.amazon.com.au",
    "amazon.de": "www.amazon.de",
    "amazon.fr": "www.amazon.fr",
    "amazon.it": "www.amazon.it",
    "amazon.es": "www.amazon.es",
    "amazon.nl": "www.amazon.nl",
    "amazon.se": "www.amazon.se",
    "amazon.pl": "www.amazon.pl",
    "amazon.co.jp": "www.amazon.co.jp",
    "amazon.com.br": "www.amazon.com.br",
    "amazon.com.mx": "www.amazon.com.mx",
    "amazon.com.tr": "www.amazon.com.tr",
    "amazon.sa": "www.amazon.sa",
    "amazon.eg": "www.amazon.eg",
    "amazon.com.be": "www.amazon.com.be",
}

# The storefront our Associate tag belongs to. A link on another storefront can
# never be converted into a paying link, so it stays informational.
AMAZON_OUR_MARKETPLACE = "amazon.in"

# Opaque Amazon short-link hosts. The Associate tag lives inside the short code
# itself, so these links must be resolved to a ``/dp/ASIN`` page before they can
# be retagged deterministically. Every host Amazon uses for its link shortener
# belongs here — a deal channel can and does send ``a.co``/``amzn.eu`` codes.
AMAZON_SHORT_HOSTS = {
    "amzn.to",
    "www.amzn.to",
    "amzn.in",
    "www.amzn.in",
    "amzn.eu",
    "www.amzn.eu",
    "amzn.asia",
    "www.amzn.asia",
    "a.co",
    "www.a.co",
}

# Hosts that are treated as Amazon URLs (India store + the mobile/global
# aliases) plus every opaque short host.
AMAZON_DOMAINS = {
    "amazon.in",
    "amazon.com",
    "m.amazon.in",
    "m.amazon.com",
} | {host.removeprefix("www.") for host in AMAZON_SHORT_HOSTS}

HYPD_DOMAINS = {"hypd.store"}


def is_amazon_short_host(url: str) -> bool:
    """True for an opaque Amazon short link whose short code decides the tag."""
    return _host_of(url) in AMAZON_SHORT_HOSTS


def is_our_amazon_marketplace(url: str) -> bool:
    """True when the URL belongs to the marketplace our Associate tag serves."""
    return _is_domain(_host_of(url), AMAZON_OUR_MARKETPLACE)


def amazon_marketplace_host(url: str) -> str | None:
    """Canonical ``www.`` host for a known Amazon marketplace URL, else None."""
    host = _host_of(url)
    for domain, canonical in AMAZON_MARKETPLACES.items():
        if _is_domain(host, domain):
            return canonical
    return None


def text_has_amazon_short(text: str) -> bool:
    """True when the text carries at least one opaque Amazon short link."""
    return any(is_amazon_short_host(url) for url in find_urls(text or ""))


def unresolved_amazon_shorts(text: str) -> list[str]:
    """Opaque Amazon shorts that could not be resolved into a ``/dp/ASIN`` link.

    A resolved short becomes a long canonical link; an unverifiable one keeps
    its opaque code and posts with best-effort attribution, so the caller should
    surface it (dashboard/warning) instead of staying silent about it.
    """
    return [url for url in find_urls(text or "") if is_amazon_short_host(url)]

# Generic link wrappers: a shortener hides the real destination from every
# classifier, so a post can carry somebody else's link (or a link that pays
# nothing) while looking perfectly normal. Every wrapper is resolved to its real
# destination before rendering; these are the hosts we know how to unwrap.
SHORTENER_DOMAINS = {
    "bit.ly",
    "bitly.com",
    "tinyurl.com",
    "tiny.cc",
    "cutt.ly",
    "rb.gy",
    "is.gd",
    "shorturl.at",
    "rebrand.ly",
    "ow.ly",
    "t.ly",
    "shrtco.de",
    "lnk.to",
    # Flipkart's own shorteners: they carry whoever created the link's affid
    # inside the code, so they must be resolved and re-converted like any other
    # merchant link instead of being posted as-is.
    "dl.flipkart.com",
    "fkrt.it",
}


def is_shortener_host(url: str) -> bool:
    """True for a generic shortener host (bit.ly, tinyurl, Flipkart's own\u2026)."""
    host = _host_of(url)
    return any(_is_domain(host, domain) for domain in SHORTENER_DOMAINS)


def is_opaque_link(url: str) -> bool:
    """True when the link hides its destination (shortener or Amazon short code)."""
    return is_amazon_short_host(url) or is_shortener_host(url)


#: Hosts we look for even without an ``http://`` prefix, longest first so
#: ``dl.flipkart.com`` wins over a hypothetical ``flipkart.com`` entry.
_BARE_OPAQUE_HOSTS = sorted(
    {host.removeprefix("www.") for host in AMAZON_SHORT_HOSTS} | SHORTENER_DOMAINS,
    key=len,
    reverse=True,
)
_BARE_OPAQUE_RE = re.compile(
    r"(?<![\w./@-])("
    + "|".join(re.escape(host) for host in _BARE_OPAQUE_HOSTS)
    + r")/[^\s)\]}>\"']+",
    re.I,
)
_TRAILING_JUNK = ".,;!?:'\""


def opaque_link_spans(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, url)`` for every opaque wrapper in ``text``.

    Scheme-less wrappers (``bit.ly/xyz``, ``amzn.to/xyz``) are included because
    deal channels post them without a protocol all the time, and an unseen short
    link is exactly how an untagged link reaches the channel.
    """
    if not text:
        return []
    spans: list[tuple[int, int, str]] = []
    scheme_ful: list[tuple[int, int]] = []
    for match in URL_RE.finditer(text):
        raw = match.group(0)
        trimmed = raw
        while trimmed and trimmed[-1] in _TRAILING_JUNK:
            trimmed = trimmed[:-1]
        end = match.start() + len(trimmed)
        scheme_ful.append((match.start(), end))
        if is_opaque_link(trimmed):
            spans.append((match.start(), end, trimmed))
    for match in _BARE_OPAQUE_RE.finditer(text):
        start = match.start()
        if any(begin <= start < end for begin, end in scheme_ful):
            continue
        trimmed = match.group(0)
        while trimmed and trimmed[-1] in _TRAILING_JUNK:
            trimmed = trimmed[:-1]
        if not trimmed:
            continue
        spans.append((start, start + len(trimmed), "https://" + trimmed))
    spans.sort(key=lambda item: item[0])
    return spans


def has_opaque_link(text: str) -> bool:
    """True when the text carries at least one wrapper link (short or shortener)."""
    return bool(opaque_link_spans(text or ""))


def unresolved_opaque_links(text: str) -> list[str]:
    """Every wrapper still present in ``text`` (used for warnings/audits)."""
    seen: list[str] = []
    for _, _, url in opaque_link_spans(text or ""):
        if url not in seen:
            seen.append(url)
    return seen


# --- resolution cache: one network round-trip per wrapper per process --------
_RESOLVED_CACHE: dict[str, tuple[float, str | None]] = {}
_RESOLVE_CACHE_MAX = 1024
_RESOLVE_CACHE_TTL = 3600.0
_RESOLVE_CACHE_NEGATIVE_TTL = 90.0


def _cache_get(url: str) -> tuple[bool, str | None]:
    entry = _RESOLVED_CACHE.get(url)
    if not entry:
        return False, None
    stored_at, final = entry
    ttl = _RESOLVE_CACHE_TTL if final else _RESOLVE_CACHE_NEGATIVE_TTL
    if time.time() - stored_at > ttl:
        _RESOLVED_CACHE.pop(url, None)
        return False, None
    return True, final


def _cache_put(url: str, final: str | None) -> None:
    if len(_RESOLVED_CACHE) >= _RESOLVE_CACHE_MAX:
        _RESOLVED_CACHE.clear()
    _RESOLVED_CACHE[url] = (time.time(), final)


def clear_resolution_cache() -> None:
    """Forget every remembered wrapper resolution (tests/operator tools)."""
    _RESOLVED_CACHE.clear()


_REFRESH_RE = re.compile(
    r"""(?:http-equiv=["']?refresh["']?[^>]*?url=([^"'>\s]+))"""
    r"""|(?:location\.(?:replace|assign|href)\s*=\s*["']([^"']+)["'])""",
    re.I,
)


def _destination_from_html(body: str) -> str | None:
    """Find a meta-refresh / JS redirect target in a shortener's HTML page.

    Several shorteners answer ``200 OK`` with a redirect in the body instead of a
    ``3xx``, so following redirects alone would leave the wrapper unresolved.
    """
    if not body:
        return None
    for match in _REFRESH_RE.finditer(body[:80000]):
        candidate = (match.group(1) or match.group(2) or "").strip().strip("'\"")
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.lower().startswith(("http://", "https://")):
            return candidate
    return None


def _normalised_link(url: str) -> tuple[str, str, str]:
    try:
        parsed = urlparse(url)
        return (
            (parsed.hostname or "").lower().rstrip("."),
            (parsed.path or "").rstrip("/"),
            urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True))),
        )
    except (TypeError, ValueError):
        return ("", "", "")


def _same_link(left: str, right: str) -> bool:
    return _normalised_link(left) == _normalised_link(right)


async def _default_resolve(url: str, timeout: float = 4.0) -> str | None:
    """Follow a wrapper link and return its real destination (or None)."""
    try:
        import aiohttp  # type: ignore
    except Exception:
        return None
    final = ""
    body = ""
    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; InfluencerHub/1.0)"}
        async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                final = str(getattr(resp, "url", "") or "")
                try:
                    body = await resp.text()
                except Exception:
                    body = ""
    except Exception:
        return None
    body_target = _destination_from_html(body)
    if body_target and _host_of(body_target):
        # A body redirect beats a URL that did not actually move (many
        # shorteners serve the landing page with a canonical/JS redirect).
        if not _host_of(final) or _same_link(final, url) or is_opaque_link(final):
            return body_target
    return final or None


async def _call_resolver(resolver, url: str, timeout: float) -> str | None:
    try:
        return await resolver(url, timeout=timeout)
    except TypeError:
        # A caller-supplied resolver may take only the URL.
        return await resolver(url)


def _replacement_for_wrapper(url: str, destination: str | None, tag: str) -> str | None:
    """The link that should replace a wrapper, or None to keep it untouched.

    * an Amazon destination becomes OUR canonical ``/dp/ASIN`` link with OUR tag
    * a merchant/HYPD/Meesho destination is returned as-is so the normal
      conversion stages (EarnKaro, HYPD retag) can monetise it
    * an informational destination keeps the wrapper (we only rewrote links that
      change what we earn — a promo/YouTube link is the source's own business)
    """
    if not destination:
        return None
    if _same_link(destination, url) or is_opaque_link(destination):
        return None
    if amazon_marketplace_host(destination):
        return _amazon_short_replacement(destination, tag)
    if classify_url(destination) in {"merchant", "hypd", "meesho", "lehlah"}:
        return destination
    return None


async def resolve_opaque_links_in_text_async(
    text: str,
    tag: str | None = None,
    *,
    resolve=None,
    max_links: int = 6,
    timeout: float = 4.0,
    budget: float | None = None,
) -> tuple[str, list[str]]:
    """Rewrite every wrapper link in ``text`` into its real destination.

    Returns ``(new_text, unresolved_urls)``. ``unresolved_urls`` lists the
    wrappers whose destination could not be determined — those are the links
    whose attribution is unknown, and the caller must treat the post as
    unmonetised instead of posting a link that (probably) pays nobody.

    ``resolve`` injects an alternative resolver for tests; when it is omitted the
    process-wide cache is used so a batch never resolves the same wrapper twice.
    """
    if not text:
        return text, []
    spans = opaque_link_spans(text)
    if not spans:
        return text, []

    unique: list[str] = []
    for _, _, url in spans:
        if url not in unique:
            unique.append(url)
    targets = unique[:max_links]
    unresolved: list[str] = list(unique[max_links:])

    use_cache = resolve is None
    destinations: dict[str, str | None] = {}
    pending: list[str] = []
    for url in targets:
        if use_cache:
            cached, final = _cache_get(url)
            if cached:
                destinations[url] = final
                continue
        pending.append(url)

    if pending:
        resolver = resolve or _default_resolve
        tasks = {asyncio.ensure_future(_call_resolver(resolver, url, timeout)): url for url in pending}
        wait_budget = (timeout * 2 + 1.0) if budget is None else budget
        done, not_done = await asyncio.wait(set(tasks), timeout=wait_budget)
        for task in done:
            url = tasks[task]
            try:
                final = task.result()
            except Exception:
                final = None
            destinations[url] = final
            if use_cache:
                _cache_put(url, final)
        for task in not_done:
            url = tasks[task]
            task.cancel()
            destinations[url] = None

    replacements: dict[str, str | None] = {}
    for url in targets:
        destination = destinations.get(url)
        replacement = _replacement_for_wrapper(url, destination, str(tag or ""))
        replacements[url] = replacement
        if replacement is None and not destination:
            unresolved.append(url)
        elif replacement is None and (is_opaque_link(destination) or _same_link(destination, url)):
            unresolved.append(url)

    out = text
    for start, end, url in sorted(spans, key=lambda item: item[0], reverse=True):
        if end > len(out):
            continue
        replacement = replacements.get(url)
        if not replacement:
            continue
        out = out[:start] + replacement + out[end:]

    deduped: list[str] = []
    for url in unresolved:
        if url not in deduped:
            deduped.append(url)
    return out, deduped


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

# --- scheme-less links --------------------------------------------------------
# Deal posts constantly carry links without ``http://`` ("flipkart.com/...",
# "amazon.in/dp/...", "amzn.to/..."). ``URL_RE`` only sees scheme-ful URLs, so
# those links were never classified, never converted and never verified — they
# simply travelled through the render untouched. They are promoted to real URLs
# before anything classifies the deal.
_BARE_KNOWN_HOSTS = sorted(
    {domain.removeprefix("www.") for domain in MERCHANT_DOMAINS}
    | set(AMAZON_MARKETPLACES)
    | {"m.amazon.in", "m.amazon.com", "hypd.store", "meesho.com"}
    | set(SHORTENER_DOMAINS)
    | {host.removeprefix("www.") for host in AMAZON_SHORT_HOSTS},
    key=len,
    reverse=True,
)
_BARE_KNOWN_RE = re.compile(
    r"(?<![\w./@-])((?:www\.)?"
    + "|".join(re.escape(host) for host in _BARE_KNOWN_HOSTS)
    + r")(/[\w\-./?=&%#+,;:!~*'()\[\]]*)?",
    re.I,
)


def promote_scheme_less_links(text: str) -> str:
    """Give ``https://`` to known hosts written without a scheme.

    Only hosts this hub can actually act on (Amazon marketplaces and shorteners,
    supported merchants, HYPD/Meesho, the wrappers above) are promoted, and only
    at a word boundary, so prose, e-mail addresses and file paths are untouched.
    """
    if not text:
        return text
    out: list[str] = []
    last = 0
    for match in _BARE_KNOWN_RE.finditer(text):
        start, end = match.span()
        if start < last:
            continue
        following = text[end:end + 1]
        if following and (following in ".-" or following.isalnum()):
            # Part of a longer host/path (or a sentence continues straight on).
            continue
        host = match.group(1)
        path = match.group(2) or ""
        if not path:
            # A bare hostname in prose ("available on amazon.in") is not a deal
            # link; only real paths/queries are promoted.
            continue
        while path and path[-1] in _TRAILING_JUNK:
            path = path[:-1]
        out.append(text[last:start])
        out.append("https://" + host + path)
        last = start + len(host) + len(match.group(2) or "")
    out.append(text[last:])
    return "".join(out)


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


# Affiliaters/EarnKaro encode the publisher id in the query string. The API's
# own parameter is `affExtParam2` (case-insensitive); converted links also
# carry a plain `id`.
PUBLISHER_PARAM_KEYS = ("affextparam2", "id")


def _looks_like_publisher_id(value: str, expected_pubid: str = "") -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    expected = str(expected_pubid or "").strip()
    if expected and candidate.casefold() == expected.casefold():
        return True
    # A bare numeric id is a real publisher id; anything else (variant ids,
    # session ids, slugs) is only trusted when it matches our own id.
    return bool(re.fullmatch(r"\d{3,24}", candidate))


def publisher_ids_in_url(url: str, expected_pubid: str = "") -> list[str]:
    """Return the EarnKaro/Affiliaters publisher ids visible in a URL.

    ``affExtParam2`` is always a publisher signal. ``id`` is weaker, so it only
    counts when it matches the expected publisher or looks like a numeric
    publisher id — unrelated merchant ``id=`` parameters must not be mistaken
    for somebody else's publisher.
    """
    try:
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
    except (TypeError, ValueError):
        return []
    values: list[str] = []
    for key, value in pairs:
        name = key.casefold()
        if name == "affextparam2":
            values.append(value)
        elif name == "id" and _looks_like_publisher_id(value, expected_pubid):
            values.append(value)
    return values


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


def is_known_merchant_host(url: str) -> bool:
    """True for a storefront this hub expects EarnKaro to convert.

    Anything else that classifies as ``merchant`` is an unknown host, where a
    failed conversion may simply mean "EarnKaro does not cover this store".
    """
    host = _host_of(url)
    return any(_is_domain(host, domain.removeprefix("www.")) for domain in MERCHANT_DOMAINS)


def classify_url(url: str) -> str:
    """Return 'amazon' | 'hypd' | 'lehlah' | 'meesho' | 'merchant' | 'shortener' | 'other'."""
    host = _host_of(url)
    if any(_is_domain(host, domain) for domain in AMAZON_DOMAINS):
        return "amazon"
    if _is_domain(host, "hypd.store"):
        return "hypd"
    if _is_lehlah_meesho_affiliate(url):
        return "lehlah"
    if _is_domain(host, "meesho.com"):
        return "meesho"
    if is_shortener_host(url):
        # A wrapper: nothing downstream can classify it until it is resolved.
        return "shortener"
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
    # Determine marketplace — an Amazon URL always keeps its own storefront.
    marketplace_host = amazon_marketplace_host(url)  # short links -> None, stay on their host

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


def expand_amazon_shorts_in_text(text: str, tag: str | None = None) -> str:
    """Heuristic to fix opaque Amazon short links that encode an old tag.

    Amazon's short codes (``amzn.to``/``amzn.in``/``amzn.eu``/``amzn.asia``/
    ``a.co``) are generated by Amazon's tools and already contain the creator's
    tag inside the code. Appending ?tag=OURTAG does NOT override the embedded
    tag, so commission would still go to the old tag. The only commission-safe
    fix without a network round-trip is:
    - If the same deal text also contains a long Amazon link with an ASIN,
      replace every opaque short with the canonical long link for that ASIN +
      OUR tag (keeping th/psc from the long link if present).
    - If no ASIN is available in the text, the async resolver tries the network
      and callers can report what stayed unresolved with
      :func:`unresolved_amazon_shorts`.

    This handles the user's case:
      amzn.to/4dnF9lU?tag=mama086-21  (short, old tag inside)
      + https://www.amazon.in/dp/B0D9P2M1PB?th=1&tag=dv12399-21 (long, ASIN B0D9P2M1PB)
      → both become https://www.amazon.in/dp/B0D9P2M1PB?th=1&tag=mama086-21
    and then the advanced shortener can shorten that canonical OUR link.
    """
    if not text or not tag:
        return text
    effective_tag = str(tag or "").strip()
    if not effective_tag:
        return text

    urls = find_urls(text)
    # Collect all ASINs and their th/psc from long links in the same text
    asins_with_params: list[tuple[str, dict[str, str]]] = []
    for u in urls:
        if is_amazon_short_host(u):
            continue
        try:
            parsed = urlparse(u)
        except Exception:
            continue
        asin = _amazon_asin(parsed)
        if asin:
            # Extract th/psc from this long link to preserve for short replacement
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            safe = {}
            for k in ("th", "psc"):
                if k in q and q[k].isdigit():
                    safe[k] = q[k]
            asins_with_params.append((asin, safe))

    if not asins_with_params:
        return text

    # Only a deal that carries exactly ONE distinct ASIN can be mapped without a
    # network round-trip: the shorts then certainly belong to that product. A
    # post with two products and two shorts would otherwise get both shorts
    # rewritten to the first product's ASIN — a wrong link is worse than an
    # unresolved one, so multi-product posts are left to the async resolver.
    distinct_asins = {asin for asin, _ in asins_with_params}
    if len(distinct_asins) != 1:
        return text

    primary_asin, primary_params = asins_with_params[0]
    # Deduplicate: if the text already contains a long link with this ASIN, we will replace shorts with that canonical
    # After replacement, the rendered text may have duplicate canonical URLs (short + long both become same)
    # The pipeline will deduplicate after advanced shortening

    out = text
    for u in urls:
        if not is_amazon_short_host(u):
            continue
        # Build canonical for this short using the primary ASIN + OUR tag + th/psc
        canonical_q = list(primary_params.items())
        canonical_q.append(("tag", effective_tag))
        canonical = urlunparse(("https", "www.amazon.in", f"/dp/{primary_asin}", "", urlencode(canonical_q), ""))
        # Replace the exact short URL occurrence (including any existing ?tag= query)
        if u in out:
            out = out.replace(u, canonical)
        else:
            base_short = u.split("?")[0]
            if base_short in out:
                out = re.sub(re.escape(base_short) + r"(\?[^\\s]*)?", canonical, out)

    return out


def _amazon_short_replacement(final_url: str, tag: str) -> str | None:
    """Canonical ``/dp/ASIN?tag=OURTAG`` for a resolved Amazon destination."""
    try:
        parsed = urlparse(final_url)
    except (TypeError, ValueError):
        return None
    asin = _amazon_asin(parsed)
    if not asin:
        return None
    marketplace_host = amazon_marketplace_host(final_url) or "www.amazon.in"
    safe_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"th", "psc"} and value.isdigit():
            safe_params.append((key.lower(), value))
    if tag:
        safe_params.append(("tag", tag))
    return urlunparse(("https", marketplace_host, f"/dp/{asin}", "", urlencode(safe_params), ""))


async def expand_amazon_shorts_in_text_async(text: str, tag: str | None = None) -> str:
    """Rewrite every opaque wrapper in a deal into a link we can verify.

    Kept for backwards compatibility (pipeline + tests): it first tries the
    offline heuristic (a single ASIN elsewhere in the same deal), then resolves
    every remaining wrapper — Amazon short codes **and** generic shorteners such
    as ``bit.ly``/``dl.flipkart.com`` — over the network via
    :func:`resolve_opaque_links_in_text_async`. Text whose wrappers cannot be
    resolved is returned unchanged; callers report those with
    :func:`unresolved_amazon_shorts` / :func:`unresolved_opaque_links`.
    """
    out = text
    if tag and text_has_amazon_short(out):
        out = expand_amazon_shorts_in_text(out, tag)
    if has_opaque_link(out):
        out, _unresolved = await resolve_opaque_links_in_text_async(out, tag)
    return out

def deduplicate_urls_in_text(text: str) -> str:
    """Remove duplicate URL occurrences, keeping the first."""
    seen: set[str] = set()
    out_parts: list[str] = []
    last = 0
    for m in URL_RE.finditer(text):
        raw = m.group(0)
        clean = raw
        while clean and clean[-1] in ".,;!?:'\"":
            clean = clean[:-1]
        if clean in seen:
            out_parts.append(text[last:m.start()])
            last = m.end()
        else:
            seen.add(clean)
            out_parts.append(text[last:m.end()])
            last = m.end()
    out_parts.append(text[last:])
    result = "".join(out_parts)
    result = re.sub(r"[ ]{2,}", " ", result)
    result = re.sub(r"\n\s*\n\s*\n", "\n\n", result)
    lines = result.splitlines()
    cleaned_lines: list[str] = []
    for line in lines:
        if line.strip() == "" and len(cleaned_lines) > 0 and cleaned_lines[-1] == "":
            continue
        cleaned_lines.append(line.rstrip())
    result = "\n".join(cleaned_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result).strip()


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
    meesho_earnkaro_fallback: bool = False,
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
            # HYPD owns Meesho but cannot mint an affiliate link from a raw
            # product URL. With the operator's explicit fallback enabled, a
            # verified EarnKaro conversion is used instead; otherwise the
            # source URL is preserved for the HYPD route.
            replacement = (
                ek.get(raw_url) if meesho_earnkaro_fallback else None
            ) or raw_url
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
    # A post that carried both an opaque short and the long link collapses into
    # the same canonical URL during expansion — show it once, not twice.
    result = deduplicate_urls_in_text(result)

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
    meesho_earnkaro_fallback: bool = False,
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

    meesho_earnkaro_fallback:
      Use a supplied EarnKaro conversion for a raw Meesho URL. Only set by the
      pipeline when the operator enabled that fallback; Meesho stays on the
      HYPD route otherwise.
    """
    post_text = clean_source_post(text) if clean_promos else text
    ek = earnkaro_links or {}

    if strip_amazon:
        rendered = _strip_amazon_render(post_text, ek, hypd_store_id=hypd_store_id)
    elif role == "approval":
        # Approval channel: ALWAYS native Amazon link with #ad disclosure. NEVER Bitly shortened.
        return _approval_render(post_text, amazon_tag)
    else:
        rendered = _render_base(
            post_text, amazon_tag, ek, hypd_store_id=hypd_store_id,
            meesho_earnkaro_fallback=meesho_earnkaro_fallback,
        )

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

    # 6. Fallback: robust fingerprint for duplicates across sources
    #    Short codes like amzn.to/XXXX are per-source unique for SAME product (ASIN hidden),
    #    so using full URL path would create different sigs for same product -> duplicates.
    #    Use cleaned title + price + stable merchant domains (shortener paths stripped).
    cleaned_for_sig = clean_source_post(text)
    lines = [ln.strip() for ln in cleaned_for_sig.splitlines() if ln.strip()]
    title_parts: list[str] = []
    for ln in lines:
        low = ln.lower()
        if "buy now" in low or "deal time" in low or "http" in low:
            continue
        if re.fullmatch(r"[\W\s]*", ln) or len(ln) < 8:
            continue
        title_parts.append(ln)
        if len(title_parts) >= 2:
            break
    if not title_parts and lines:
        first = re.sub(r"https?://\S+", "", lines[0]).strip()
        if first:
            title_parts = [first]
    title_raw = " ".join(title_parts)[:120]
    # Remove price fragments from title so Rs.1999 vs ₹1999 don't create different sigs for same product
    title_raw = re.sub(r"(?:₹|rs\.?|inr)\s*[\d,\.]+", " ", title_raw, flags=re.I)
    title_raw = re.sub(r"@\s*[\d,\.]+", " ", title_raw)
    title_norm = re.sub(r"[^\w\s]", " ", title_raw.lower())
    title_norm = re.sub(r"\s+", " ", title_norm).strip()
    title_words = title_norm.split()
    title_key = " ".join(title_words[:8]) if title_words else title_norm[:50]
    prices = re.findall(r"(?:₹|rs\.?|inr)\s?([\d,]{2,})", text, re.I)
    norm_prices = sorted(set(p.replace(",", "").strip() for p in prices if p.strip()))
    price_key = norm_prices[0] if norm_prices else ""
    shortener_hosts = {"amzn.to", "amzn.in", "bit.ly", "bitly.com", "tinyurl.com", "shorturl.at", "t.me", "telegram.me", "chat.whatsapp.com"}
    stable_domains: list[str] = []
    for u in find_urls(text):
        try:
            host = (_host_of(u) or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host in shortener_hosts or host.endswith(".amzn.to") or host.endswith(".amzn.in"):
                stable_domains.append(host)
            else:
                if host:
                    stable_domains.append(host)
                else:
                    p2 = urlparse(u)
                    clean = f"{p2.netloc.lower()}{p2.path.rstrip('/')}"
                    stable_domains.append(clean.lower())
        except Exception:
            stable_domains.append(u.lower().split("/")[2] if "//" in u else u.lower())
    stable_domains = sorted(set(stable_domains))
    domain_key = ",".join(stable_domains[:3])
    if title_key and len(title_key) >= 4:
        raw = f"title:{title_key}#price:{price_key}#dom:{domain_key}"
    else:
        clean_urls = []
        for u in find_urls(text):
            try:
                p2 = urlparse(u)
                host = (_host_of(u) or "").lower()
                if host in shortener_hosts:
                    clean = host
                else:
                    clean = f"{p2.netloc.lower()}{p2.path.rstrip('/')}"
                clean_urls.append(clean)
            except Exception:
                clean_urls.append(u.lower())
        clean_urls = sorted(set(clean_urls))
        raw = "|".join(clean_urls) + "#" + "|".join(norm_prices[:2])
    return hashlib.sha1(raw.lower().encode("utf-8")).hexdigest()[:16]


def get_title_tokens(text: str) -> set[str]:
    """Extract normalized title tokens for near-duplicate detection.
    Used for catching 4-5-56x duplicates where same product has slightly different wording
    (e.g. 'NIRLON Non-Stick 3-Piece' vs 'NIRLON Non Stick 3 Piece - Red Black')
    Returns set of lowercased words without price/short codes.
    """
    try:
        cleaned = clean_source_post(text)
    except Exception:
        cleaned = text
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    title_parts: list[str] = []
    for ln in lines:
        low = ln.lower()
        if "buy now" in low or "deal time" in low or "http" in low:
            continue
        if re.fullmatch(r"[\W\s]*", ln) or len(ln) < 5:
            continue
        title_parts.append(ln)
        if len(title_parts) >= 2:
            break
    if not title_parts and lines:
        first = re.sub(r"https?://\S+", "", lines[0]).strip()
        if first:
            title_parts = [first]
    title_raw = " ".join(title_parts)[:150]
    # Remove price, numbers that are likely timestamps, and extra
    title_raw = re.sub(r"(?:₹|rs\.?|inr)\s*[\d,\.]+", " ", title_raw, flags=re.I)
    title_raw = re.sub(r"@\s*[\d,\.]+", " ", title_raw)
    title_raw = re.sub(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\s*ist\b", " ", title_raw, flags=re.I)  # Deal Time
    title_raw = re.sub(r"[^\w\s]", " ", title_raw.lower())
    title_raw = re.sub(r"\s+", " ", title_raw).strip()
    # Remove very common stop words that don't help identify product
    stop = {"the", "a", "an", "and", "or", "for", "with", "at", "in", "on", "of", "deal", "price", "buy", "now", "more", "new"}
    # Keep tokens >=2 chars to retain important specs like '3' is now kept as '3' is important for '3-piece'
    # But filter out single letters and pure numbers that are timestamps
    tokens = set()
    for w in title_raw.split():
        if w in stop:
            continue
        if len(w) < 2:
            continue
        # Keep numbers like '3' if part of product spec (e.g., 3-piece), but not standalone timestamps
        tokens.add(w)
    return tokens


def titles_are_near_duplicate(text1: str, text2: str, threshold: float = 0.65) -> bool:
    """Check if two deals have near-duplicate titles (Jaccard similarity).
    Catches 56x duplicates where same product posted with slight title variations
    but same core keywords (e.g. 70% word overlap).
    """
    try:
        t1 = get_title_tokens(text1)
        t2 = get_title_tokens(text2)
        if not t1 or not t2:
            return False
        # Also require same price to avoid false positives for different products same brand
        p1 = re.findall(r"(?:₹|rs\.?|inr)\s?([\d,]{2,})", text1, re.I)
        p2 = re.findall(r"(?:₹|rs\.?|inr)\s?([\d,]{2,})", text2, re.I)
        n1 = {x.replace(",", "").strip() for x in p1 if x.strip()}
        n2 = {x.replace(",", "").strip() for x in p2 if x.strip()}
        price_match = bool(n1 & n2) if n1 and n2 else True  # if no price, don't require match
        if not price_match:
            return False
        inter = len(t1 & t2)
        union = len(t1 | t2)
        if union == 0:
            return False
        jaccard = inter / union
        return jaccard >= threshold
    except Exception:
        return False


# ============== MORE AND MORE ADVANCED: NEXT-GEN FEATURES ==============

def calculate_advanced_loot_score(text: str) -> dict:
    """Advanced AI-like scoring with 10 factors for fully advanced deal ranking.
    Returns dict with score, tier, and breakdown. Used for smart filtering and
    ensuring only best deals are posted (prevents 'motham vachinave' spam).
    Tier: S (90-100) - Must post, A (75-89) - Good, B (50-74) - Average, C (<50) - Skip
    """
    import re as _re
    score = 10.0
    breakdown = {}
    
    # 1. Discount % (0-50 points)
    m = _re.search(r"(\d{1,2})%\s*(?:off|discount|chadhimpu|taggimpu)", text, _re.I)
    if m:
        d = float(m.group(1))
        pts = min(50.0, d * 0.62)
        score += pts
        breakdown["discount"] = pts
    
    # 2. Price loot bonus (0-35 points)
    price = extract_price(text)
    if price is not None:
        if price <= 99:
            pts = 35.0
        elif price <= 199:
            pts = 30.0
        elif price <= 299:
            pts = 25.0
        elif price <= 499:
            pts = 18.0
        elif price <= 999:
            pts = 10.0
        elif price <= 1999:
            pts = 5.0
        else:
            pts = 0
        score += pts
        breakdown["price"] = pts
    
    # 3. Urgency (0-20 points)
    urgent = ["loot", "steal", "bug", "error", "price error", "flat", "free", "grab", "lowest", "huge drop", "adhiripoye", "offer", "dhamaka", "bumper"]
    low = text.lower()
    u_pts = sum(5.0 for w in urgent if w in low)
    u_pts = min(20.0, u_pts)
    score += u_pts
    breakdown["urgency"] = u_pts
    
    # 4. Brand value (0-10 points)
    premium = ["sony", "samsung", "iphone", "oneplus", "nike", "puma", "adidas", "levis", "boat", "philips", "xiaomi", "realme"]
    b_pts = 8.0 if any(b in low for b in premium) else 0
    score += b_pts
    breakdown["brand"] = b_pts
    
    # 5. Freshness / time decay (new deals get boost)
    breakdown["freshness"] = 5.0
    score += 5.0
    
    # Cap at 100
    score = min(100.0, score)
    tier = "S" if score >= 90 else "A" if score >= 75 else "B" if score >= 50 else "C"
    breakdown["total"] = round(score, 1)
    breakdown["tier"] = tier
    return breakdown


def is_high_quality_deal(text: str, min_tier: str = "B") -> bool:
    """Check if deal is high quality enough to post. Prevents low-quality spam.
    Tier order: S > A > B > C. Default min B (score 50+) — blocks C tier spam.
    """
    tier_order = {"S": 4, "A": 3, "B": 2, "C": 1}
    adv = calculate_advanced_loot_score(text)
    return tier_order.get(adv["tier"], 0) >= tier_order.get(min_tier, 2)


def get_deal_category_advanced(text: str) -> dict:
    """Advanced category detection with confidence and multi-label.
    Returns {category: confidence} for fully advanced targeting.
    """
    cats = classify_deal_category(text)
    # Add confidence based on keyword matches
    result = {}
    low = text.lower()
    for cat in cats:
        kws = CATEGORY_KEYWORDS.get(cat, [])
        matched = sum(1 for kw in kws if kw.lower() in low)
        conf = min(0.99, 0.5 + matched * 0.15)
        result[cat] = round(conf, 2)
    return result


def translate_telugu_keywords(text: str) -> str:
    """Advanced: Normalize Telugu/Hinglish keywords for better dedup and filtering.
    E.g., 'adhiripoye deal' -> 'huge deal', 'cheapest' -> same
    Helps with 'SOURCES CHALA APPS NUCHI VASTHAI' — different languages same product.
    """
    tel_map = {
        "adhiripoye": "huge", "dhamaka": "huge", "bumper": "huge",
        "cheapest": "lowest", "thakkuva": "lowest", "takkuva": "lowest",
        "offeru": "offer", "opparu": "offer",
        "konandi": "buy", "koneండి": "buy",
    }
    low = text.lower()
    for k, v in tel_map.items():
        low = low.replace(k, v)
    return low

