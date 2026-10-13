"""The guided setup: creator → channels → categories → post, in one screen.

Four steps, in the order an operator actually has the information:

1. **Creator** — the name and their *own* Amazon Associate tag (this cannot be
   skipped: a creator without an own tag would post under somebody else's id);
2. **Channels** — paste the approval link, the main channel link and the WhatsApp
   channel/group link (or pair the phone for a QR/code);
3. **What they sell** — categories, posting hours, price filter, the three network
   switches and the main → WhatsApp mirror, pre-filled from the owner's presets;
4. **Go live** — one review screen, then "save and post": the creator is active,
   the channels inherit the tag, and a first deal (or a Test a Deal preview) is one
   click away.

This module is the pure part: the step plan, the category picker and the review
lines. The routes live in ``dashboard/app.py``.
"""
from __future__ import annotations

from . import age

STEPS = (
    {"id": "creator", "number": 1, "label": "Creator", "icon": "🧑",
     "what": "Their name and their own Amazon Associate tag."},
    {"id": "channels", "number": 2, "label": "Channels", "icon": "🔗",
     "what": "The approval + main Telegram links and the WhatsApp destination."},
    {"id": "categories", "number": 3, "label": "Deals & hours", "icon": "🛍️",
     "what": "Which categories, which hours, which networks."},
    {"id": "post", "number": 4, "label": "Go live", "icon": "🚀",
     "what": "Review, save and post the first deal."},
)

# The picker mirrors the creator page's list, so "select categories" looks the
# same everywhere in the hub.
CATEGORY_CHOICES = (
    ("all", "🌟 All categories", "any loot"),
    ("clothing", "👗 Fashion & Clothing", "sarees, kurtis, mens wear"),
    ("electronics", "📱 Electronics & Tech", "phones, earbuds, gadgets"),
    ("footwear", "👟 Footwear & Shoes", "sneakers, sandals"),
    ("home", "🏠 Home & Kitchen", "kitchen, decor, storage"),
    ("daily", "🛒 Daily & Grocery", "everyday essentials"),
    ("beauty", "💄 Beauty & Care", "skincare, makeup"),
    ("jewellery", "💍 Jewellery & Watches", "gold-plated, smart watches"),
    ("toys", "🧸 Toys & Baby", "kids, baby care"),
    ("sports", "🏏 Sports & Fitness", "gym, outdoor, cricket"),
    ("books", "📚 Books & Stationery", "books, school items"),
    ("automotive", "🚗 Automotive", "car, bike accessories"),
)

CATEGORY_KEYS = {value for value, _label, _hint in CATEGORY_CHOICES}


def selected_categories(raw: str) -> list[str]:
    """The picked keys from a stored ``categories`` value ('' means everything)."""
    text = str(raw or "").strip()
    if not text:
        return ["all"]
    parts = [part.strip().lower() for part in text.split(",") if part.strip()]
    if "all" in parts or "unrestricted" in parts or "any" in parts:
        return ["all"]
    return [part for part in parts if part in CATEGORY_KEYS] or ["all"]


def category_text(picked) -> str:
    """Store a picker selection as the ``categories`` string ('' = all)."""
    keys = [str(key).strip().lower() for key in (picked or []) if str(key).strip()]
    keys = [key for key in keys if key in CATEGORY_KEYS]
    if not keys or "all" in keys:
        return ""
    ordered = [value for value, _label, _hint in CATEGORY_CHOICES if value in keys]
    return ",".join(ordered)


def checkpoint(profile: dict | None, channels: list[dict] | None = None,
               *, wa_status: str = "") -> dict:
    """Which step the creator is on, and what is already done.

    Used by the wizard (progress bar + "continue where you left off") and by the
    lists ("set up 2 months ago, 3 of 4 steps done").
    """
    profile = dict(profile or {})
    channels = list(channels or [])
    roles = {str(channel.get("role") or "broadcast"): channel for channel in channels}
    main = roles.get("broadcast")
    approval = roles.get("approval")
    whatsapp = next((channel for channel in channels
                     if str(channel.get("platform") or "").startswith("whatsapp")), None)
    tag = str(profile.get("amazon_tag") or "").strip()

    creator_done = bool(profile.get("id")) and bool(str(profile.get("name") or "").strip()) \
        and bool(tag)
    channels_done = bool(main and str(main.get("status") or "").lower() == "ready")
    deals_done = bool(str(profile.get("categories") or "").strip()) \
        or bool(str(profile.get("posting_schedule") or "").strip())
    live_done = bool(profile.get("active")) and channels_done

    done = {"creator": creator_done, "channels": channels_done,
            "categories": deals_done, "post": live_done}
    order = [step["id"] for step in STEPS]
    current = next((step_id for step_id in order if not done[step_id]), "post")
    finished = sum(1 for step_id in order if done[step_id])
    return {
        "done": done,
        "current": current,
        "current_number": next(step["number"] for step in STEPS if step["id"] == current),
        "finished": finished,
        "total": len(order),
        "percent": int(round(100 * finished / len(order))),
        "ready": bool(profile.get("id")) and live_done and not whatsapp is None,
        "review": review_lines(profile, channels, wa_status=wa_status),
    }


def review_lines(profile: dict, channels: list[dict] | None = None, *,
                 wa_status: str = "") -> list[dict]:
    """The step-4 review: exactly what will be saved, in plain words."""
    profile = dict(profile or {})
    channels = list(channels or [])
    roles = {str(channel.get("role") or "broadcast"): channel for channel in channels}
    approval = roles.get("approval")
    main = roles.get("broadcast")
    whatsapp = next((channel for channel in channels
                     if str(channel.get("platform") or "").startswith("whatsapp")), None)
    tag = str(profile.get("amazon_tag") or "")
    lines = [
        {"label": "Creator", "value": str(profile.get("name") or "—"),
         "ok": bool(profile.get("name"))},
        {"label": "Their own Amazon tag", "value": tag or "not saved yet",
         "ok": bool(tag)},
        {"label": "Approval (preview) channel",
         "value": str((approval or {}).get("identifier") or "not linked yet"),
         "ok": bool(approval)},
        {"label": "Main channel",
         "value": (str((main or {}).get("identifier") or "not linked yet")
                   + (f" · mirrors to WhatsApp" if str((main or {}).get('mirror_whatsapp') or '')
                      not in {"", "0"} else "")),
         "ok": bool(main)},
        {"label": "WhatsApp destination",
         "value": (str((whatsapp or {}).get("identifier") or "not linked yet")
                   + (f" · {wa_status}" if wa_status else "")),
         "ok": whatsapp is not None},
        {"label": "Categories",
         "value": ", ".join(selected_categories(profile.get("categories")))
                  if profile.get("categories") else "all (any loot)",
         "ok": True},
        {"label": "Posting hours",
         "value": str(profile.get("posting_schedule") or "any time"), "ok": True},
        {"label": "Price filter",
         "value": str(profile.get("price_filter") or "all"), "ok": True},
        {"label": "Set up",
         "value": age.setup_label(profile.get("created_at"),
                                  member=str(profile.get("added_by") or "")) or "—",
         "ok": True},
    ]
    # The proof the owner asks for at the end: the exact Amazon link this
    # creator's channels will carry. Filled by the caller (the wizard) so this
    # module stays free of the database and the router.
    if lines[1]["ok"]:  # a tag is saved
        lines.append({"label": "Amazon links will read", "value": "", "ok": True,
                      "link": True})
    return lines


def progress_text(checkpoint_data: dict) -> str:
    """One line for a card: ``3 of 4 steps done · next: Deals & hours``."""
    current = next((step for step in STEPS
                    if step["id"] == checkpoint_data.get("current")), STEPS[-1])
    return (f"{checkpoint_data.get('finished', 0)} of {checkpoint_data.get('total', 4)} "
            f"steps done · next: {current['label']}")
