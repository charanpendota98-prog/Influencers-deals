"""Creator-scoped, opt-in links between an influencer's own channels.

A smart navigation row is derived from ready Telegram destinations owned by the
same creator. It deliberately never scans the fleet, and a destination without
a public handle or verified invite URL is omitted rather than guessed.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import db

SMART_CHANNEL_NAV = "__smart_creator_channels__"
_MAX_NAV_BUTTONS = 8
_ALLOWED_HOSTS = {"t.me", "telegram.me", "www.telegram.me"}


def _safe_telegram_url(channel: dict) -> str:
    invite = str(channel.get("invite_link") or "").strip()
    identifier = str(channel.get("identifier") or "").strip()
    candidate = invite
    if not candidate and identifier.startswith("@"):
        handle = identifier[1:]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", handle):
            return ""
        candidate = "https://t.me/" + handle
    elif not candidate and identifier.lower().startswith(("https://t.me/", "https://telegram.me/")):
        candidate = identifier
    if not candidate:
        return ""
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_HOSTS:
        return ""
    if parsed.username or parsed.password or port not in (None, 443):
        return ""
    if not parsed.path or parsed.path == "/" or parsed.path.startswith("//"):
        return ""
    return candidate


def creator_channel_buttons(influencer_id: int, current_channel_id: int | None = None,
                            channels: list[dict] | None = None) -> list[dict[str, str]]:
    """Return join buttons for other ready Telegram channels of this creator only."""
    owned = channels if channels is not None else db.list_channels(influencer_id)
    buttons: list[dict[str, str]] = []
    seen: set[str] = set()
    for channel in owned:
        owner_id = channel.get("influencer_id")
        if owner_id is not None and str(owner_id) != str(influencer_id):
            continue
        if str(channel.get("platform") or "").lower() != "telegram":
            continue
        if str(channel.get("status") or "").lower() not in {"ready", "active"}:
            continue
        if current_channel_id is not None and str(channel.get("id")) == str(current_channel_id):
            continue
        url = _safe_telegram_url(channel)
        if not url or url in seen:
            continue
        seen.add(url)
        title = str(channel.get("title") or channel.get("identifier") or "Channel").strip()
        if title.startswith("@"):
            title = title[1:]
        label = ("Join " + title)[:64]
        buttons.append({"text": label, "url": url})
        if len(buttons) >= _MAX_NAV_BUTTONS:
            break
    return buttons



def normalize_custom_button_url(raw: str | None) -> str:
    """Validate a CTA target and normalize supported Telegram handles.

    Only HTTPS links are accepted for explicit CTAs. This prevents unsafe
    schemes such as ``javascript:`` and avoids sending users to cleartext URLs.
    """
    raw_url = str(raw or "").strip()
    if not raw_url or any(ord(char) < 32 for char in raw_url):
        return ""
    if raw_url.startswith("@"):
        handle = raw_url[1:]
        return f"https://t.me/{handle}" if re.fullmatch(r"[A-Za-z0-9_]{1,64}", handle) else ""
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except ValueError:
        return ""
    if (parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username or parsed.password or port not in (None, 443)):
        return ""
    return raw_url


def custom_post_buttons(settings: dict | None) -> list[dict[str, str]]:
    """Return at most two validated, explicitly configured post CTA buttons.

    Creator/channel-specific CTA links are never fetched or guessed. Telegram
    handles are normalized to HTTPS links; arbitrary URLs must be HTTPS and
    must not contain credentials or a non-standard port.
    """
    if not isinstance(settings, dict):
        return []
    buttons: list[dict[str, str]] = []
    seen: set[str] = set()
    for suffix in ("", "2"):
        candidate = normalize_custom_button_url(
            settings.get(f"custom_button{suffix}_url")
        )
        if not candidate or candidate in seen:
            continue
        label = str(settings.get(f"custom_button{suffix}_text") or "").strip()
        label = label[:64] or ("Open link" if suffix else "Join Shpsy Loots ❤️")
        seen.add(candidate)
        buttons.append({"text": label, "url": candidate})
    return buttons


def navigation_matrix(influencer_id: int, channels: list[dict] | None = None) -> dict:
    """Build a read-only admin simulation of the links each post will expose."""
    owned = channels if channels is not None else db.list_channels(influencer_id)
    owned = [
        channel for channel in owned
        if channel.get("influencer_id") is None
        or str(channel.get("influencer_id")) == str(influencer_id)
    ]
    telegram_rows = [
        channel for channel in owned
        if str(channel.get("platform") or "").lower() == "telegram"
    ]
    ready_rows = [
        channel for channel in telegram_rows
        if str(channel.get("status") or "").lower() in {"ready", "active"}
    ]
    linkable = [channel for channel in ready_rows if _safe_telegram_url(channel)]
    matrix = []
    for source in ready_rows:
        buttons = creator_channel_buttons(
            influencer_id, source.get("id"), channels=owned
        )
        matrix.append({
            "channel": str(source.get("title") or source.get("identifier") or "Telegram channel"),
            "buttons": buttons,
            "button_count": len(buttons),
        })
    return {
        "ready_channels": len(ready_rows),
        "linkable_channels": len(linkable),
        "offline_channels": len(telegram_rows) - len(ready_rows),
        "unlinked_channels": len(ready_rows) - len(linkable),
        "rows": matrix,
        "max_buttons": _MAX_NAV_BUTTONS,
    }


def whatsapp_navigation_footer(buttons: list[dict[str, str]]) -> str:
    """Format Telegram-channel destinations as a clean WhatsApp text footer."""
    return "\n".join(f"👉 {button['text']}: {button['url']}" for button in buttons)
