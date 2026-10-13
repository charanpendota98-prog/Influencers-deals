"""Deal sources: which channels a creator's posts may come from.

The owner's rule: a creator must have a source, and if nothing is selected the
hub falls back to **their** default source — ``https://t.me/LootZoneIndia11``
(``config.DEFAULT_SOURCE_SPEC``) — instead of quietly posting from every source
in the hub. The picker beside the source card lets one or many be ticked, and
what is stored is the plain token list the pipeline already filters on
(``creators.allowed_sources``: ``powerloot, lootzoneindia11``).

The stored tokens are matched loosely and case-insensitively by
``pipeline._is_source_allowed`` against the dialog name/username a deal actually
arrived from, so ``LootZoneIndia11`` (the username) is what a tick writes — not
the full ``https://t.me/…`` URL, which could never match a dialog name.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import config, db

#: What "no selection" means for a creator or channel.
DEFAULT_SPEC = str(getattr(config, "DEFAULT_SOURCE_SPEC", "") or "").strip()
DEFAULT_NAME = str(getattr(config, "DEFAULT_SOURCE_NAME", "") or "").strip()
#: The token stored in ``allowed_sources`` when nothing else was picked.
DEFAULT_TOKEN = ""


def _clean(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


def token_for(spec: str) -> str:
    """``https://t.me/LootZoneIndia11`` / ``@LootZoneIndia11`` → ``LootZoneIndia11``.

    A public username is the exact selector Telegram dialogs resolve to; a private
    invite keeps its hash (``+ab12…``), which the puller matches by joined-dialog
    title instead — either way the token is what a creator's filter compares.
    """
    text = _clean(spec)
    if not text:
        return ""
    if text.startswith("@") and "/" not in text:
        return text[1:]
    lowered = text.lower()
    if lowered.startswith(("http://", "https://", "t.me/", "telegram.me/")):
        if not lowered.startswith("http"):
            text = "https://" + text
        path = urlparse(text).path.strip("/")
        return path.split("/")[-1] if path else ""
    # Already a token (a name the operator typed, or a private invite hash).
    return text.lstrip("@")


def token_from_source_row(source: dict) -> str:
    """The token a saved source row stands for (its username, else its name)."""
    spec = str(source.get("spec") or "")
    token = token_for(spec)
    if token and not token.startswith("+"):
        return token
    return _clean(source.get("name") or token)


def normalize_selection(raw: str) -> str:
    """Accept anything the operator pastes; store plain tokens, comma-separated.

    ``https://t.me/LootZoneIndia11``, ``@LootZoneIndia11`` and
    ``LootZoneIndia11`` all become ``LootZoneIndia11``; ``a, b`` and newline
    lists are both accepted; blank entries and duplicates are dropped.
    """
    parts = re.split(r"[,\n;]+", str(raw or ""))
    tokens: list[str] = []
    seen: set[str] = set()
    for part in parts:
        token = token_for(part)
        key = token.lower()
        if token and key not in seen:
            seen.add(key)
            tokens.append(token)
    return ", ".join(tokens)


def selection_tokens(raw: str) -> list[str]:
    return [token for token in normalize_selection(raw).split(", ") if token]


def choice_rows() -> list[dict]:
    """The tick-box rows: every configured source, plus tokens already in use.

    A row is ``{token, label, spec, kind, active, used_by, is_default}``. Sources
    the operator configured come first (in their order), then any token a creator
    already uses that has no configured row — so an older selector is never
    silently lost when the picker is used.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    try:
        configured = db.list_sources(active_only=False)
    except Exception:  # pragma: no cover - display only
        configured = []
    default_key = default_token().lower()
    for source in configured:
        token = token_from_source_row(source)
        if not token or token.lower() in seen:
            continue
        seen.add(token.lower())
        rows.append({
            "token": token,
            "label": _clean(source.get("name") or token),
            "spec": _clean(source.get("spec") or token),
            "kind": _clean(source.get("kind") or "production"),
            "active": bool(source.get("active")),
            "is_default": token.lower() == default_key,
            "used_by": 0,
        })
    try:
        for profile in db.list_influencers():
            for token in selection_tokens(profile.get("allowed_sources") or ""):
                if token.lower() in seen:
                    continue
                seen.add(token.lower())
                rows.append({
                    "token": token,
                    "label": token,
                    "spec": "",
                    "kind": "in use",
                    "active": True,
                    "is_default": token.lower() == default_key,
                    "used_by": 0,
                })
    except Exception:  # pragma: no cover - display only
        pass
    # Count how many creators post from each token.
    try:
        counts: dict[str, int] = {}
        for profile in db.list_influencers():
            for token in selection_tokens(profile.get("allowed_sources") or ""):
                counts[token.lower()] = counts.get(token.lower(), 0) + 1
        for row in rows:
            row["used_by"] = counts.get(row["token"].lower(), 0)
    except Exception:  # pragma: no cover - display only
        pass
    return rows


def default_token() -> str:
    """The token a creator gets when no source is selected."""
    token = token_for(DEFAULT_SPEC) or _clean(DEFAULT_NAME)
    return token.lstrip("+")


def ensure_default() -> dict:
    """Make sure the default source exists, so the worker can really read it.

    Returns ``{added, source_id, name, spec, token}``. Idempotent: the spec is
    matched exactly, and a hub that already has it is left untouched.
    """
    spec = DEFAULT_SPEC
    token = default_token()
    result = {"added": False, "source_id": 0, "name": DEFAULT_NAME or token,
              "spec": spec, "token": token}
    if not spec:
        return result
    try:
        existing = {str(row.get("spec") or "").strip(): row
                    for row in db.list_sources(active_only=False)}
    except Exception:  # pragma: no cover - defensive
        existing = {}
    row = existing.get(spec)
    if row:
        result["source_id"] = int(row.get("id") or 0)
        return result
    try:
        result["source_id"] = db.add_source(DEFAULT_NAME or token, spec, kind="production")
        result["added"] = True
    except Exception:  # pragma: no cover - defensive
        pass
    return result


def selection_line(raw: str) -> str:
    """One sentence for the dashboard: which sources this creator posts from."""
    tokens = selection_tokens(raw)
    if not tokens:
        return "every source in the hub"
    return ", ".join(tokens)
