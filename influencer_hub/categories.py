"""Offline, explainable product categorisation. No fetching, sends or DB writes.

Evidence strength is a rule score, not a calibrated probability. URLs and common
channel footers are excluded so affiliate parameters cannot decide a product.
"""
from __future__ import annotations

import re
from copy import deepcopy
from functools import lru_cache
import unicodedata

ALIASES = {
    'jewellery': 'jewellery_accessories', 'jewelry': 'jewellery_accessories',
    'toys': 'baby', 'books': 'books_stationery', 'grocery': 'daily',
    'fashion': 'clothing', 'tech': 'electronics', 'pets': 'pet_supplies',
    'travel': 'travel_luggage',
}
UNRESTRICTED = {'all', 'any', 'unrestricted', 'all categories', 'all deals', '*', 'no_filter', 'default'}
# Product phrases override misleading words *within that phrase*, not the rest
# of a multi-product post. Thus a gaming mouse cannot become an animal product.
PHRASES = {
    'gaming mouse': ('gaming', 'electronics'),
    'gaming keyboard': ('gaming', 'electronics'),
    'smart watch': ('electronics',), 'smartwatch': ('electronics',),
    'notebook computer': ('electronics',), 'laptop stand': ('electronics',),
    'air fryer': ('appliances', 'home'), 'vacuum cleaner': ('appliances', 'home'),
    'hair dryer': ('appliances', 'beauty'), 'electric iron': ('appliances',),
    'steam iron': ('appliances',), 'iron supplement': ('daily',),
    'iron tablets': ('daily',), 'iron supplement tablets': ('daily',),
    'vitamin tablets': ('daily',), 'multivitamin tablets': ('daily',),
    'protein powder': ('sports',),
    'whey protein': ('sports',), 'pet shampoo': ('pet_supplies',),
    'dog food': ('pet_supplies',), 'cat food': ('pet_supplies',),
    'feeding bottle': ('baby',), 'baby shampoo': ('baby',),
    'baby lotion': ('baby',), 'baby soap': ('baby',),
    'school bag': ('books_stationery',), 'gold plated necklace': ('jewellery_accessories',),
    'స్మార్ట్ వాచ్': ('electronics',), 'చీర': ('clothing',),
    'చీరలు': ('clothing',), 'బూట్లు': ('footwear', 'clothing'),
    'బియ్యం': ('daily',), 'సబ్బు': ('daily',),
    'మొబైల్': ('electronics',), 'హెడ్‌ఫోన్స్': ('electronics',),
    'airpods': ('electronics',), 'airdopes': ('electronics',),
}
# Standalone colour/marketing/ambiguous terms are not enough to reject a deal.
AMBIGUOUS = {'gold', 'silver', 'diamond', 'ring', 'chain', 'compact', 'top',
             'wear', 'living', 'home', 'daily', 'health', 'wireless', 'remote control',
             'safari', 'vip', 'steam', 'iron', 'ram'}


def normalise(value: str) -> str:
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub(r'\s+', ' ', value.replace('-', ' ')).strip()


def selected(spec: str) -> set[str]:
    return {ALIASES.get(part.strip().lower(), part.strip().lower())
            for part in str(spec or '').split(',') if part.strip()}


@lru_cache(maxsize=1024)
def _pattern(term):
    return re.compile(r'(?<!\w)' + re.escape(normalise(term)).replace(r'\ ', r'\s+') + r'(?!\w)')


def product_text(text: str) -> str:
    """Normalised product-only text, also used for preference phrase matching."""
    raw = str(text or '')[:20000]
    lines = [line for line in raw.splitlines()
             if not re.match(r'^\s*(?:join|follow|subscribe)\b', line, re.I)]
    clean = re.sub(r'(?:https?://|www\.)\S+|\b\w[\w.-]*\.(?:com|in|to|org)/\S*', ' ', '\n'.join(lines), flags=re.I)
    return normalise(clean)


@lru_cache(maxsize=128)
def _analyse_cached(text: str) -> dict:
    from .link_router import CATEGORY_KEYWORDS
    clean = product_text(text)
    evidence: dict[str, set[str]] = {}
    remaining = clean
    for phrase, categories in sorted(PHRASES.items(), key=lambda item: -len(item[0])):
        pattern = _pattern(phrase)
        if pattern.search(remaining):
            for category in categories:
                evidence.setdefault(category, set()).add(phrase)
            remaining = pattern.sub(' ', remaining)
    for category, terms in CATEGORY_KEYWORDS.items():
        for term in terms:
            if term not in AMBIGUOUS and _pattern(term).search(remaining):
                evidence.setdefault(category, set()).add(term)
    # A manufacturer's name alone does not identify its product: Samsung sells
    # phones AND fridges. Brand-only evidence must not veto an appliance feed.
    weak_brands = {'samsung', 'oneplus', 'redmi', 'realme'}
    if evidence.get('electronics') and evidence['electronics'] <= weak_brands:
        del evidence['electronics']
    # Keep legacy taxonomy order stable for callers; scores are explanations only.
    categories = [key for key in CATEGORY_KEYWORDS if key in evidence]
    products = [key for key in categories if key != 'recharge_freebies']
    primary = max(products, key=lambda key: (len(evidence[key]), -categories.index(key))) if products else 'other'
    return {
        'categories': categories or ['other'], 'primary': primary,
        'product_known': bool(products), 'multi_category': len(products) > 1,
        'evidence': {key: sorted(evidence[key]) for key in categories},
        'scores': {key: round(min(0.95, 0.55 + 0.1 * len(evidence[key])), 2) for key in categories},
        'method': 'offline_keyword_rules',
        'notice': 'Rule scores are not probabilities. No product page was fetched.',
    }


def analyse(text: str) -> dict:
    """Bounded per-process cache; callers receive independent mutable results."""
    return deepcopy(_analyse_cached(str(text or '')[:20000]))


def preview(text: str, spec: str = '') -> dict:
    from . import config
    from .link_router import CATEGORY_KEYWORDS
    result = analyse(text)
    allowed = selected(spec)
    invalid = sorted(allowed - set(CATEGORY_KEYWORDS) - UNRESTRICTED - {'other'})
    if not allowed or allowed & UNRESTRICTED:
        passes, reason = True, 'All categories are enabled.'
    elif allowed & set(result['categories']):
        passes, reason = True, 'Product matches a selected category.'
    elif not result['product_known']:
        passes = not getattr(config, 'CATEGORY_FILTER_STRICT_UNKNOWN', False)
        reason = 'Unknown product: ' + ('allowed by default.' if passes else 'held by strict unknown mode.')
    else:
        passes, reason = False, 'Known product is outside the selected categories.'
    result.update(allowed=sorted(allowed), passes=passes, reason=reason, invalid_categories=invalid)
    return result
