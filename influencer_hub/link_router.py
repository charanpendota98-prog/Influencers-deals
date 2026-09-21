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
    shortened_links: dict[str, str] | None = None,
    role: str = "broadcast",
    strip_amazon: bool = False,
    clean_promos: bool = True,
) -> str:
    """Render `text` for one influencer on a given channel `role`.

    shortened_links:
      Map of URL -> Bitly short link (if Bitly shortening was executed for 2+ links or long links).
      Applied on non-approval channels to keep multi-link posts clean and uncluttered.
      NOTE: Amazon approval channel ALWAYS bypasses shorteners to preserve compliance with Amazon's native link rules.

    clean_promos:
      If True (default), strips source channel promotional text, invite links, and @channel watermarks,
      while strictly preserving product titles, descriptions, and pricing.

    strip_amazon:
      If True -> Amazon links and Amazon-only product lines are completely REMOVED.
      Only Flipkart/Myntra/etc. (monetised through our EarnKaro) are posted.

    role:
      'broadcast' / 'whatsapp' -> full deal: Amazon links retagged to THEIR tag
          (or omitted if strip_amazon=True), other merchants swapped to OUR EarnKaro.
      'approval' -> Amazon-only, posted NATIVELY (no shortener, amazon.in visible)
          with the '#ad (paid link)' disclosure.
    """
    post_text = clean_source_post(text) if clean_promos else text
    ek = earnkaro_links or {}

    if strip_amazon:
        rendered = _strip_amazon_render(post_text, ek)
    elif role == "approval":
        # Approval channel: ALWAYS native Amazon link with #ad disclosure. NEVER Bitly shortened.
        return _approval_render(post_text, amazon_tag)
    else:
        rendered = _render_base(post_text, amazon_tag, ek)

    # Apply Bitly shortener replacements if available (for broadcast/whatsapp channels)
    if shortened_links and role != "approval":
        for long_u, short_u in shortened_links.items():
            if long_u and short_u and long_u != short_u:
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
        r"(?:deal price|price|at)\s*(?:₹|rs\.?|inr|:)?\s*(\d+(?:\.\d{1,2})?)",
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
]


def clean_source_post(text: str) -> str:
    """Clean promotional watermarks, source telegram links, @admin tags,
    and invite links from source posts while PRESERVING product titles,
    descriptions, prices, and merchant/amazon links completely intact.
    """
    lines = text.splitlines()
    cleaned_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue

        # Check if line is purely an invite/telegram link
        pure_tg = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me|chat\.whatsapp\.com)/\S+$", stripped, re.I)
        if pure_tg:
            continue

        # Check if line is a generic promo line
        promo_match = re.search(r"(?i)^\s*(?:join|subscribe|follow|join channel|join fast|share with friends|for more deals|more offers at)\b.*", stripped)
        if promo_match and not find_urls(stripped):
            continue

        # Line might have product name or price + an @handle or promo at the end.
        # Strip out telegram handles/links from the line while keeping product name and valid store urls.
        line_out = line
        # Remove telegram invite links inside the line
        line_out = re.sub(r"https?://(?:t\.me|telegram\.me|chat\.whatsapp\.com)/\S+", "", line_out, flags=re.I)
        # Remove channel tag / handle (e.g. @PowerLoots or @secretdeal) but don't damage normal text
        line_out = re.sub(r"(?i)\s*@(?:[a-zA-Z0-9_]{3,30})\b", "", line_out)
        # Remove trailing promo phrases
        line_out = re.sub(r"(?i)\s*[-|•~]\s*(?:join|loot by|powered by|credit)\s*.*$", "", line_out)

        line_out = line_out.strip()
        if line_out:
            cleaned_lines.append(line_out)

    result = "\n".join(cleaned_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result)


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
}


def classify_deal_category(text: str) -> list[str]:
    """Identify which categories (clothing, electronics, home, daily) a deal belongs to."""
    lower = text.lower()
    matched = []
    for cat, kws in CATEGORY_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(kw)}\b", lower) for kw in kws):
            matched.append(cat)
    return matched or ["other"]


def matches_category_filter(deal_text: str, allowed_categories_spec: str) -> bool:
    """Return True if the deal belongs to any of the allowed categories.
    allowed_categories_spec: comma-separated list like 'clothing,electronics' or empty/all for all.
    """
    s = (allowed_categories_spec or "").strip().lower()
    if not s or s == "all":
        return True
    allowed_set = {x.strip() for x in s.split(",") if x.strip()}
    deal_cats = set(classify_deal_category(deal_text))
    # If the deal matches any allowed category, return True
    if deal_cats.intersection(allowed_set):
        return True
    # If deal has uncategorized items, let it pass if 'other' is in allowed or allow graceful pass
    if "other" in deal_cats and ("all" in allowed_set or not allowed_set):
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
    if not spec or spec.lower() == "all" or spec.lower() == "24/7":
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




def deal_signature(text: str) -> str:
    """A stable signature for dedup:
    1. Extracts ASINs from Amazon links (B0...)
    2. Extracts clean merchant URLs
    3. Normalizes title and first price token.
    This guarantees that even if multiple source channels (Powerloot, Secret Loots)
    post the same product with slightly different emojis or referral tags,
    we generate the EXACT SAME SIGNATURE so duplicate products NEVER post twice.
    """
    import hashlib
    asins = sorted(set(re.findall(r"/(?:dp|gp/product|product)/([A-Za-z0-9]{8,12})", text, re.I)))
    if asins:
        # High-confidence Amazon product dedup by ASIN
        raw = "asin:" + ":".join(a.upper() for a in asins)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    # Non-Amazon or general URLs: strip queries/tracking params to get canonical deal
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
