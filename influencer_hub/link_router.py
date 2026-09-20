"""Link routing — the heart of the influencer scheme.

For a given deal text and a given influencer we produce a copy of the text in
which:

  * AMAZON links carry THE INFLUENCER's amazon associate tag
    (so the commission on Amazon sales goes to them)
  * every OTHER supported merchant link (Flipkart, Myntra, Ajio, ...) is
    replaced by OUR EarnKaro link (so the commission goes to us)

This module is PURE and has NO network or credentials dependency, so it is
exercised directly by the unit tests. The EarnKaro conversion itself is an
async network call done by the pipeline and passed in as a precomputed map.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Hostnames we treat as "Amazon" (taggable).
AMAZON_DOMAINS = {"amazon.in", "www.amazon.in", "amazon.com", "www.amazon.com"}

# Merchant domains we monetise through OUR EarnKaro account (everything except
# Amazon). Add more here as the deal pool grows.
MERCHANT_DOMAINS = {
    "flipkart.com", "www.flipkart.com",
    "myntra.com", "www.myntra.com",
    "ajio.com", "www.ajio.com",
    "nykaa.com", "www.nykaa.com",
    "snapdeal.com", "www.snapdeal.com",
    "meesho.com", "www.meesho.com",
}

# A reasonably permissive URL finder (http/https only).
URL_RE = re.compile(r"https?://[^\s)>\]]+", re.I)


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def classify_url(url: str) -> str:
    """Return 'amazon' | 'merchant' | 'other'."""
    host = _host_of(url)
    if host in AMAZON_DOMAINS:
        return "amazon"
    if host in MERCHANT_DOMAINS:
        return "merchant"
    return "other"


def collect_links(text: str) -> dict[str, str]:
    """Map each unique URL -> its classification."""
    out: dict[str, str] = {}
    for u in find_urls(text):
        out[u] = classify_url(u)
    return out


def compact_amazon_product_link(url: str, tag: str | None = None) -> str:
    """Normalise an Amazon URL to https://www.amazon.in/dp/<ASIN>?tag=<tag>.

    Strips tracking junk (session/attribution params) and keeps only the ASIN
    and the associate tag. If no ASIN is present the original host/path is
    preserved with the tag attached.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host not in AMAZON_DOMAINS:
        return url
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    asin = None
    m = re.search(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{8,12})", parsed.path, re.I)
    if m:
        asin = m.group(1).upper()
    else:
        for k, v in q.items():
            if k.lower() in ("asin",) and re.fullmatch(r"[A-Za-z0-9]{8,12}", v or "", re.I):
                asin = v.upper()
                break
    if asin:
        path = f"/dp/{asin}"
    else:
        path = parsed.path or "/"
    if tag:
        q = {"tag": tag}
    else:
        q = {k: v for k, v in q.items() if k.lower() == "tag"}
    query = urlencode(q)
    return urlunparse(("https", "www.amazon.in", path, "", query, ""))


def apply_amazon_tag(url: str, tag: str) -> str:
    """Ensure an Amazon URL carries exactly `tag` (replacing any existing one)."""
    parsed = urlparse(url)
    if parsed.netloc.lower() not in AMAZON_DOMAINS:
        return url
    # If it is a clean product link, compact + set tag.
    if re.search(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{8,12})", parsed.path, re.I):
        return compact_amazon_product_link(url, tag)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    q["tag"] = tag
    # Drop duplicate tag keys (case-insensitive) that some sources double up.
    seen = set()
    filtered = []
    for k, v in q.items():
        lk = k.lower()
        if lk == "tag":
            if "tag" in seen:
                continue
            seen.add("tag")
        filtered.append((k, v))
    query = urlencode(filtered)
    return urlunparse(parsed._replace(query=query))


def _render_base(text: str, amazon_tag: str, ek: dict[str, str]) -> str:
    out_parts: list[str] = []
    last = 0
    for m in URL_RE.finditer(text):
        start, end = m.span()
        url = m.group(0)
        kind = classify_url(url)
        if kind == "amazon":
            replacement = apply_amazon_tag(url, amazon_tag)
        elif kind == "merchant":
            replacement = ek.get(url, url)
        else:
            replacement = url
        out_parts.append(text[last:start])
        out_parts.append(replacement)
        last = end
    out_parts.append(text[last:])
    return "".join(out_parts)


def _approval_render(text: str, amazon_tag: str) -> str:
    """Produce an Amazon-approval-friendly post.

    Requirements (matching Amazon associate policies + SmartBuy Hub format):
      1. ONLY Amazon products/links (native amazon.in links with ?tag=<influencer_tag>).
      2. Non-Amazon merchant lines/links are completely removed.
      3. No URL shorteners on Amazon links (plain amazon.in visible).
      4. Ends with the mandatory disclosure: '#ad (paid link)'.
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

        # Line contains URLs: check if any is Amazon vs merchant
        line_has_amazon = any(classify_url(u) == "amazon" for u in urls)
        line_has_merchant = any(classify_url(u) == "merchant" for u in urls)

        if line_has_amazon:
            # Retag all amazon URLs on this line
            out_line = line
            for u in urls:
                if classify_url(u) == "amazon":
                    out_line = out_line.replace(u, apply_amazon_tag(u, amazon_tag))
                elif classify_url(u) == "merchant":
                    # Drop merchant url from line
                    out_line = out_line.replace(u, "").strip()
            if out_line.strip():
                kept_lines.append(out_line)
                has_amazon = True
        elif line_has_merchant:
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


def _strip_amazon_render(text: str, ek: dict[str, str]) -> str:
    """Produce a post where Amazon links/lines are completely REMOVED,
    and all non-Amazon merchant links are converted to our EarnKaro."""
    lines = text.splitlines()
    kept_lines: list[str] = []

    for line in lines:
        urls = find_urls(line)
        if not urls:
            kept_lines.append(line)
            continue

        line_has_amazon = any(classify_url(u) == "amazon" for u in urls)
        line_has_merchant = any(classify_url(u) == "merchant" for u in urls)

        if line_has_amazon and not line_has_merchant:
            # Pure Amazon line -> remove completely
            continue
        elif line_has_amazon and line_has_merchant:
            # Line has both: remove amazon, rewrite merchant
            out_line = line
            for u in urls:
                if classify_url(u) == "amazon":
                    out_line = out_line.replace(u, "").strip()
                elif classify_url(u) == "merchant":
                    out_line = out_line.replace(u, ek.get(u, u))
            if out_line.strip():
                kept_lines.append(out_line)
        else:
            # Non-amazon line -> rewrite merchant to earnkaro
            out_line = line
            for u in urls:
                if classify_url(u) == "merchant":
                    out_line = out_line.replace(u, ek.get(u, u))
            kept_lines.append(out_line)

    result = "\n".join(kept_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result)


def render_for_influencer(
    text: str,
    amazon_tag: str,
    earnkaro_links: dict[str, str] | None = None,
    role: str = "broadcast",
    strip_amazon: bool = False,
) -> str:
    """Render `text` for one influencer on a given channel `role`.

    strip_amazon:
      If True -> Amazon links and Amazon-only product lines are completely REMOVED.
      Only Flipkart/Myntra/etc. (monetised through our EarnKaro) are posted.

    role:
      'broadcast' / 'whatsapp' -> full deal: Amazon links retagged to THEIR tag
          (or omitted if strip_amazon=True), other merchants swapped to OUR EarnKaro.
      'approval' -> Amazon-only, posted NATIVELY (no shortener, amazon.in visible)
          with the '#ad (paid link)' disclosure.
    """
    ek = earnkaro_links or {}
    if strip_amazon:
        return _strip_amazon_render(text, ek)
    if role == "approval":
        return _approval_render(text, amazon_tag)
    return _render_base(text, amazon_tag, ek)


def has_amazon_link(text: str) -> bool:
    return any(classify_url(u) == "amazon" for u in find_urls(text))


def deal_signature(text: str) -> str:
    """A stable signature for dedup: lower-cased alphanumerics of the URLs +
    the first price-looking token. Keeps one product from posting twice per
    influencer/channel."""
    import hashlib
    urls = sorted(find_urls(text))
    prices = re.findall(r"(?:₹|rs\.?|inr)\s?([\d,]{2,})", text, re.I)
    raw = "|".join(urls) + "#" + "|".join(prices)
    return hashlib.sha1(raw.lower().encode("utf-8")).hexdigest()[:16]
