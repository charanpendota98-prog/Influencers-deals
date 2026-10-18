"""Per-creator opt-in strict product rules, shared by preview and delivery.

An explicit channel filter can narrow a strict creator's selection, never widen
it. Existing creators remain on legacy routing until preferences are saved.
"""
from __future__ import annotations

import json

from . import categories

DEFAULT = {'mode': 'legacy', 'mixed': 'block', 'excluded_categories': [],
           'required_keywords': [], 'blocked_keywords': []}


def validate_spec(spec: str) -> str:
    from .link_router import CATEGORY_KEYWORDS
    if not isinstance(spec, str) or len(spec) > 1000:
        raise ValueError('Category selection must be text, at most 1000 characters.')
    tokens = categories.selected(spec)
    invalid = tokens - set(CATEGORY_KEYWORDS) - categories.UNRESTRICTED
    if invalid:
        raise ValueError('Unknown categories: ' + ', '.join(sorted(invalid)))
    if tokens & categories.UNRESTRICTED:
        return ''
    return ','.join(sorted(tokens))


def _phrases(value):
    if isinstance(value, str):
        value = value.split(',')
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError('Use up to 20 comma-separated product phrases.')
    result = []
    for phrase in value:
        if not isinstance(phrase, str) or len(phrase) > 80:
            raise ValueError('Each product phrase must be text, at most 80 characters.')
        phrase = categories.normalise(phrase)
        if phrase and phrase not in result:
            result.append(phrase)
    return result


def parse(raw=None) -> dict:
    if not raw:
        return {key: list(value) if isinstance(value, list) else value for key, value in DEFAULT.items()}
    if isinstance(raw, str):
        if len(raw) > 8000:
            raise ValueError('Preferences are too long.')
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError('Invalid category preferences; review and save again.') from exc
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT):
        raise ValueError('Invalid category preference fields.')
    result = {**DEFAULT, **raw}
    if result['mode'] not in ('legacy', 'strict') or result['mixed'] not in ('allow', 'block'):
        raise ValueError('Invalid category preference mode.')
    excluded = result['excluded_categories']
    if isinstance(excluded, list) and all(isinstance(value, str) for value in excluded):
        excluded = ','.join(excluded)
    if not isinstance(excluded, str) or len(excluded) > 1000:
        raise ValueError('Excluded categories must be a category list.')
    if categories.selected(excluded) & categories.UNRESTRICTED:
        raise ValueError('Exclude individual categories, not all categories.')
    result['excluded_categories'] = sorted(categories.selected(validate_spec(excluded)))
    result['required_keywords'] = _phrases(result['required_keywords'])
    result['blocked_keywords'] = _phrases(result['blocked_keywords'])
    return result


def view(profile: dict) -> dict:
    """Safe UI representation; malformed saved rules are visible, not ignored."""
    try:
        result = parse(profile.get('category_preferences'))
        result['error'] = ''
    except ValueError as exc:
        result = parse()
        result.update(mode='strict', error=str(exc))
    return result


def _strict_category_check(result, spec: str, mixed: str):
    try:
        spec = validate_spec(spec)
    except ValueError as exc:
        return False, str(exc), 'invalid_categories'
    allowed = categories.selected(spec)
    if not allowed:
        return True, 'All categories enabled.', 'all_categories'
    detected = set(result['categories'])
    matched = allowed & detected
    if not matched:
        return False, 'No selected product category was detected.', 'category_mismatch'
    if mixed == 'block':
        # Shared labels are not separate products: "shoes" is both clothing and
        # footwear. Only outside-category evidence not covered by an allowed
        # category counts as a mixed/off-topic product.
        covered = {term for key in matched for term in result['evidence'].get(key, [])}
        outside = [key for key in detected - allowed - {'other', 'recharge_freebies'}
                   if set(result['evidence'].get(key, [])) - covered]
        if outside:
            return False, 'Mixed/off-topic product evidence: ' + ', '.join(sorted(outside)), 'mixed_products'
    return True, 'Selected product category matched.', 'category_match'


def is_strict(profile: dict) -> bool:
    try:
        return parse(profile.get('category_preferences'))['mode'] == 'strict'
    except ValueError:
        return True  # malformed saved policy must fail closed


def evaluate(text: str, profile: dict, channel: dict | None = None) -> dict:
    """Pure decision used for live dispatch, highlights, mirrors and preview."""
    channel = channel or {}
    try:
        prefs = parse(profile.get('category_preferences'))
    except ValueError as exc:
        return {'passes': False, 'reason': str(exc), 'code': 'invalid_preferences', 'mode': 'strict'}
    if prefs['mode'] == 'legacy':
        result = categories.preview(text, channel.get('categories') or profile.get('categories') or '')
        result.update(mode='legacy', code='legacy_filter')
        return result
    result = categories.analyse(text)
    result.update(mode='strict')

    def decide(passes, reason, code):
        result.update(passes=passes, reason=reason, code=code)
        return result

    # Unknown/oversized input must not be interpreted as a proven match.
    restricted = bool(categories.selected(profile.get('categories') or '') - categories.UNRESTRICTED)
    channel_restricted = bool(categories.selected(channel.get('categories') or '') - categories.UNRESTRICTED)
    has_rules = restricted or channel_restricted or any(prefs[key] for key in
                        ('excluded_categories', 'required_keywords', 'blocked_keywords'))
    if has_rules and len(str(text or '')) > 20000:
        return decide(False, 'Deal exceeds the category analysis limit.', 'analysis_limit')
    excluded = set(prefs['excluded_categories']) & set(result['categories'])
    if excluded:
        return decide(False, 'Excluded category detected: ' + ', '.join(sorted(excluded)), 'excluded_category')
    clean = categories.product_text(text)
    blocked = [phrase for phrase in prefs['blocked_keywords'] if categories._pattern(phrase).search(clean)]
    if blocked:
        return decide(False, 'Blocked product phrase: ' + ', '.join(blocked), 'blocked_keyword')
    required = prefs['required_keywords']
    if required and not any(categories._pattern(phrase).search(clean) for phrase in required):
        return decide(False, 'None of the required product phrases matched.', 'required_keyword')
    offer_selected = any('recharge_freebies' in categories.selected(row.get('categories') or '')
                         for row in (profile, channel))
    if has_rules and not result['product_known'] and not (offer_selected and 'recharge_freebies' in result['categories']):
        return decide(False, 'Unknown product held by strict creator preferences.', 'unknown_product')
    passes, reason, code = _strict_category_check(result, profile.get('categories') or '', prefs['mixed'])
    if not passes:
        return decide(False, reason, code)
    if channel.get('categories'):
        passes, reason, code = _strict_category_check(result, channel['categories'], prefs['mixed'])
        if not passes:
            return decide(False, 'Channel narrowing: ' + reason, 'channel_' + code)
    return decide(True, 'Matches this creator’s saved product preferences.', 'preferences_match')
