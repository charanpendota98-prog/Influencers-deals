"""Adding creators: one easy line, understood properly, checked before it is written.

The team should be able to type what they have in front of them:

```
Ravi Loots ravi-21 @ravi_loots @ravi_amazon_deals 120363012345678901@newsletter +91 98765 43210
```

and the hub should know which token is the name, which is the Amazon tag, which
is the main channel, which is the preview channel, which is a WhatsApp
destination and which is a phone number — then say, before writing anything,
what it understood and what is wrong with the row.

* :func:`parse_line` / :func:`parse` — the tolerant parser (comma / tab / pipe /
  semicolon rows keep the documented column order; a line with no separator is
  classified token by token, so both habits work).
* :func:`preview` — the "check these lines first" view: one row per pasted line,
  what was understood, and the reason a line cannot be added.

Pure functions over strings, stdlib only, no database and no writes.
"""
from __future__ import annotations

import re

FIELDS = ("name", "tag", "approval", "main", "whatsapp", "phone",
          "categories", "schedule", "price_filter", "notes", "hypd_store_id")

# The documented positional order for delimited rows (kept for compatibility:
# a spreadsheet column that is junk in the tag slot must stay a bad tag).
POSITIONAL = ("name", "tag", "main", "approval", "whatsapp", "phone")
# What an operator may add after those columns: the order the team page and the
# README document. A value that does not fit its slot (a note in the price
# column) is kept as a note instead of becoming a wrong setting.
EXTENDED = POSITIONAL + ("categories", "schedule", "price_filter", "notes")

PRICE_WORDS = ("all", "any", "unrestricted", "budget", "cheap", "premium", "high", "low")

# Header aliases: short form plus every column name the CSV exports use.
ALIASES = {
    "name": "name", "creator": "name", "creator_name": "name", "creator_id": "creator_id",
    "full_name": "name", "influencer": "name",
    "amazon_tag": "tag", "tag": "tag", "associate_tag": "tag", "amazon": "tag",
    "main_channel": "main", "main": "main", "broadcast_tg": "main", "broadcast": "main",
    "main_tg": "main", "channel": "main", "telegram": "main", "telegram_channel": "main",
    "approval_channel": "approval", "approval": "approval", "approval_tg": "approval",
    "preview_channel": "approval", "preview": "approval",
    "whatsapp_channel": "whatsapp", "whatsapp": "whatsapp", "whatsapp_id": "whatsapp",
    "whatsapp_group": "whatsapp", "wa": "whatsapp",
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "number": "phone",
    "categories": "categories", "category": "categories", "niche": "categories",
    "posting_schedule": "schedule", "schedule": "schedule", "hours": "schedule",
    "posting_hours": "schedule",
    "price_filter": "price_filter", "price": "price_filter", "filter": "price_filter",
    "notes": "notes", "note": "notes", "remark": "notes",
    "hypd_store_id": "hypd_store_id", "store": "hypd_store_id", "store_id": "hypd_store_id",
}

# ``key=value`` spelling inside one line, so a paste can carry the extras too.
KEY_ALIASES = {
    **ALIASES,
    "tag": "tag", "amazon_tag": "tag",
}

_SPLIT = re.compile(r"[,\t|;]")
_PHONE = re.compile(r"^\+?[\d][\d\s\-()]{8,17}$")
# One marketplace longer than any real channel name means "this is one link".
_URLISH = re.compile(r"^(https?://|t\.me/|telegram\.me/|telegram\.dog/|www\.)", re.I)


def _clean_label(value: str) -> str:
    return str(value or "").strip().strip('"').strip("'").strip()


def _looks_like_phone(value: str) -> bool:
    raw = _clean_label(value)
    if not raw or not _PHONE.match(raw):
        return False
    digits = re.sub(r"\D", "", raw)
    return 10 <= len(digits) <= 14


def _phone_value(value: str) -> str:
    digits = re.sub(r"\D", "", _clean_label(value))
    if digits.startswith("0"):
        digits = digits.lstrip("0")
    return digits


def _looks_like_whatsapp(value: str) -> bool:
    low = _clean_label(value).lower()
    return low.endswith(("@newsletter", "@g.us", "@s.whatsapp.net")) or low.startswith(
        ("whatsapp:", "https://chat.whatsapp.com/", "chat.whatsapp.com/")
    )


def _looks_like_telegram(value: str) -> bool:
    raw = _clean_label(value)
    if not raw:
        return False
    if _URLISH.match(raw):
        return True
    if raw.startswith("@"):
        return len(raw) > 1
    # ``t.me`` style without scheme plus a slash, or a bare @handle-less username
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_]{3,31}$", raw)) and not _PHONE.match(raw)


def _usable_tag(value: str) -> bool:
    from . import link_router

    try:
        return bool(link_router.amazon_tag_is_usable(_clean_label(value)))
    except Exception:  # pragma: no cover - defensive
        return False


def _classify(token: str) -> tuple[str, str]:
    """What one token probably is: ``(field, value)`` with field "" when unknown."""
    raw = _clean_label(token)
    if not raw:
        return "", ""
    if "=" in raw and not _URLISH.match(raw):
        key, value = raw.split("=", 1)
        field = KEY_ALIASES.get(key.strip().lower().replace(" ", "_"))
        if field and field != "creator_id":
            return field, _clean_label(value)
    if _looks_like_whatsapp(raw):
        return "whatsapp", raw
    if _URLISH.match(raw):
        return "telegram", raw
    if _looks_like_phone(raw):
        return "phone", _phone_value(raw)
    if raw.startswith("@"):
        return "telegram", raw
    if _usable_tag(raw):
        return "tag", raw
    return "", raw


def _normalise_telegram(value: str) -> str:
    raw = _clean_label(value)
    if not raw:
        return ""
    low = raw.lower()
    for prefix in ("https://", "http://"):
        if low.startswith(prefix):
            low = low[len(prefix):]
    if low.startswith(("t.me/", "telegram.me/", "telegram.dog/")):
        low = low.split("/", 1)[1] if "/" in low else ""
        low = low.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
        return ("@" + low) if low else ""
    return raw


def _header_map(parts: list[str]) -> dict[int, str] | None:
    """``{index: field}`` when the row is a header, else ``None``."""
    mapped: dict[int, str] = {}
    seen_any = False
    for index, part in enumerate(parts):
        key = _clean_label(part).lower().replace(" ", "_")
        if not key:
            continue
        field = ALIASES.get(key)
        if field:
            mapped[index] = field
            seen_any = True
    if seen_any and "name" in mapped.values():
        return mapped
    return None


def _kv_pairs(raw: str) -> tuple[dict, str]:
    """``key=value`` pairs inside a line, plus the line with them removed.

    A quoted value may contain spaces (``notes="instagram, 12k"``); an unquoted
    one ends at the next whitespace.
    """
    found: dict[str, str] = {}
    pattern = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*("[^"]*"|\'[^\']*\'|\S+)')
    def _take(match):
        key = match.group(1).lower()
        field = KEY_ALIASES.get(key)
        if not field or field == "creator_id":
            return match.group(0)
        value = _clean_label(match.group(2)).strip(",;|\t")
        if value and field not in found:
            found[field] = value
            return ""
        return match.group(0)

    return found, pattern.sub(_take, raw)


def _hours_like(value: str) -> bool:
    """``09-23`` / ``9-21`` / ``09:00-23:00``: a posting window, not a tag."""
    return bool(re.fullmatch(r"\d{1,2}(:\d{2})?\s*-\s*\d{1,2}(:\d{2})?", _clean_label(value)))


def _price_like(value: str) -> bool:
    return _clean_label(value).lower() in PRICE_WORDS


def _tag_shaped(value: str) -> bool:
    """Amazon tags look like ``ravi-21`` / ``mama086-21`` (a word plus -NN)."""
    raw = _clean_label(value)
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,28}-\d{2}$", raw))


def parse_line(line: str, number: int = 1,
               order: tuple[str, ...] | None = None) -> dict:
    """One pasted line into a row dict ({"line", "name", "tag", ...} + "how").

    ``order`` is the column order a header established; without one the
    documented positional order is used. A line with no separator is read token
    by token, so ``Ravi Loots ravi-21 @ravi_loots`` works as easily as the CSV
    row — and a value that clearly belongs to another field (a WhatsApp link in
    the tag column, a phone in a channel column, …) is moved to where it
    belongs and the move is reported in ``how``.
    """
    raw = str(line or "").rstrip()
    row: dict = {"line": number, "raw": raw.strip(), "how": []}
    for field in FIELDS:
        row[field] = ""
    parts = [_clean_label(part) for part in _SPLIT.split(raw)]
    delimited = len(parts) > 1

    if delimited and all(part == "" for part in parts):
        return row

    if not delimited:
        return _parse_tokens(raw, row)

    # ---- delimited: documented column order unless a header said otherwise ----
    inline, stripped = _kv_pairs(raw)
    if inline:
        parts = [_clean_label(part) for part in _SPLIT.split(stripped)]
        for field, value in inline.items():
            row[field] = value
            row["how"].append(f"{field} ← {value} (key=value)")
    columns = list(order) if order else list(EXTENDED)
    extras: list[str] = []
    for index, part in enumerate(parts):
        if not part:
            continue
        if index < len(columns):
            field = columns[index]
            if field == "creator_id":
                continue
            if row.get(field):
                continue                      # a key=value already set this field
            if not _slot_accepts(field, part):
                extras.append(part)           # a note typed into the price column
                continue
            row[field] = part
        else:
            extras.append(part)

    _relocate(row)
    for part in extras:
        _absorb_extra(row, part)
    for field in ("main", "approval"):
        if row.get(field):
            row[field] = _normalise_telegram(row[field])
    row["how"] = _how(row) + [item for item in row["how"] if "←" not in item]
    return row


def _parse_tokens(raw: str, row: dict) -> dict:
    """A line with no separator: classify token by token."""
    inline, stripped = _kv_pairs(raw)
    for field, value in inline.items():
        row[field] = value
        row["how"].append(f"{field} ← {value} (key=value)")

    tokens = _merge_phone_tokens(stripped.split())

    name_bits: list[str] = []
    strays: list[str] = []
    telegram_seen = 0
    name_open = True
    for token in tokens:
        field, value = _classify_token(token)
        if field:
            name_open = False
            if field == "telegram":
                telegram_seen += 1
                target = "main" if telegram_seen == 1 else "approval"
                if not row.get(target):
                    row[target] = value
                continue
            if not row.get(field):
                row[field] = value
            continue
        if name_open:
            name_bits.append(token)
        else:
            strays.append(token)
    if name_bits:
        row["name"] = " ".join(name_bits).strip()
    if strays:
        row["notes"] = (row["notes"] + " " + " ".join(strays)).strip()
    _relocate(row)
    for field in ("main", "approval"):
        if row.get(field):
            row[field] = _normalise_telegram(row[field])
    row["how"] = _how(row) + [item for item in row["how"] if "←" not in item]
    return row


def _merge_phone_tokens(chunks) -> list[str]:
    """Put a phone number typed with spaces back together (``+91 98765 43210``)."""
    tokens = list(chunks)
    out: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("+") or token.isdigit():
            combined = token
            look = index
            while (look + 1 < len(tokens)
                   and len(re.sub(r"\D", "", combined)) < 10
                   and re.fullmatch(r"[\d\s\-()]+", tokens[look + 1] or "")):
                combined = combined + " " + tokens[look + 1]
                look += 1
            if _looks_like_phone(combined):
                out.append(combined)
                index = look + 1
                continue
        out.append(token)
        index += 1
    return out


def _classify_token(token: str) -> tuple[str, str]:
    """Like :func:`_classify`, but a bare word is never a tag.

    In a free-form line the name comes first ("Ravi Loots ravi-21 …"), so only a
    tag-shaped token (``ravi-21``) is read as one; a value that merely happens to
    be usable as a tag stays part of the name.
    """
    raw = _clean_label(token)
    if not raw:
        return "", ""
    if _usable_tag(raw) and not _tag_shaped(raw):
        return "", raw
    return _classify(raw)


def _slot_accepts(field: str, value: str) -> bool:
    """Does this value really belong in that column?"""
    if field == "schedule":
        return _hours_like(value) or _bool_like(value) is None and not _looks_like_phone(value)
    if field == "price_filter":
        return _price_like(value)
    if field in {"allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon"}:
        return _bool_like(value) is not None
    return True


def _bool_like(value: str):
    text = _clean_label(value).lower()
    if text in {"1", "true", "yes", "on", "y", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "n", "disabled"}:
        return False
    return None


def _absorb_extra(row: dict, part: str) -> None:
    """A token past the known columns: kv, channel, phone, else a note."""
    field, value = _classify(part)
    if field in FIELDS and not row.get(field):
        row[field] = value
        return
    if field == "telegram":
        for target in ("main", "approval"):
            if not row.get(target):
                row[target] = value
                return
        row["notes"] = (row["notes"] + " " + value).strip()
        return
    if not field and part:
        row["notes"] = (row["notes"] + " " + part).strip()


def _relocate(row: dict) -> None:
    """Move values that ended up in the wrong column, and say so in ``how``."""
    # 1. channel columns holding a WhatsApp destination or a phone number
    for field in ("main", "approval"):
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        if _looks_like_whatsapp(value):
            if not row.get("whatsapp"):
                row["whatsapp"] = value
                row["how"].append(f"{value} → WhatsApp (was in the {field} column)")
            row[field] = ""
        elif _looks_like_phone(value):
            if not row.get("phone"):
                row["phone"] = _phone_value(value)
                row["how"].append(f"{value} → phone (was in the {field} column)")
            row[field] = ""
    # 2. the whatsapp column holding a phone number
    whatsapp = str(row.get("whatsapp") or "").strip()
    if whatsapp and _looks_like_phone(whatsapp):
        if not row.get("phone"):
            row["phone"] = _phone_value(whatsapp)
        row["whatsapp"] = ""
        row["how"].append(f"{whatsapp} → phone (was in the whatsapp column)")
    # 3. the tag column holding something that is clearly not a tag
    tag = str(row.get("tag") or "").strip()
    if tag:
        if _looks_like_whatsapp(tag):
            if not row.get("whatsapp"):
                row["whatsapp"] = tag
                row["how"].append(f"{tag} → WhatsApp (was in the tag column)")
            row["tag"] = ""
        elif _looks_like_phone(tag):
            if not row.get("phone"):
                row["phone"] = _phone_value(tag)
                row["how"].append(f"{tag} → phone (was in the tag column)")
            row["tag"] = ""
        elif _looks_like_telegram(tag) and not _tag_shaped(tag):
            for target in ("main", "approval"):
                if not row.get(target):
                    row[target] = tag
                    row["how"].append(f"{tag} → {target} channel (was in the tag column)")
                    row["tag"] = ""
                    break


def _how(row: dict) -> list[str]:
    """A short "what I understood" list for the preview."""
    bits = []
    for field, label in (("name", "name"), ("tag", "Amazon tag"), ("main", "main channel"),
                         ("approval", "approval channel"), ("whatsapp", "WhatsApp"),
                         ("phone", "phone"), ("categories", "categories"),
                         ("schedule", "hours"), ("price_filter", "price"),
                         ("hypd_store_id", "store"), ("notes", "notes")):
        value = str(row.get(field) or "").strip()
        if value:
            bits.append(f"{label} ← {value}")
    return bits


def parse(text: str) -> list[dict]:
    """Many lines (a paste or a CSV) into rows; a header row is understood."""
    rows: list[dict] = []
    order: tuple[str, ...] | None = None
    header_done = False
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [_clean_label(part) for part in _SPLIT.split(line)]
        if not header_done:
            header_done = True
            mapped = _header_map(parts) if len(parts) > 1 else None
            if mapped is not None:
                width = max(mapped) + 1
                order = tuple(mapped.get(index, "") for index in range(width))
                continue
        row = parse_line(line, number, order)
        if any(str(row.get(field) or "").strip() for field in FIELDS):
            rows.append(row)
    return rows


def preview(text: str, *, limit: int = 200) -> dict:
    """What the parser understood, and what is missing, before anything is written.

    Read-only: the caller may render this and wait for a human.
    """
    rows = parse(text)
    shown: list[dict] = []
    for row in rows[: max(1, int(limit))]:
        problems: list[str] = []
        if not str(row.get("name") or "").strip():
            problems.append("no name")
        if not str(row.get("tag") or "").strip():
            problems.append("no Amazon tag — the hub-wide tag would be used")
        elif not _usable_tag(str(row.get("tag") or "")):
            problems.append(f"“{row['tag']}” is not a usable Amazon tag")
        if not str(row.get("main") or "").strip() and not str(row.get("approval") or "").strip():
            problems.append("no main or approval channel")
        shown.append({
            "line": row["line"], "name": row.get("name") or "",
            "tag": row.get("tag") or "", "main": row.get("main") or "",
            "approval": row.get("approval") or "",
            "whatsapp": row.get("whatsapp") or "", "phone": row.get("phone") or "",
            "notes": row.get("notes") or "", "how": row.get("how") or [],
            "problems": problems, "ready": not problems,
        })
    return {
        "rows": shown,
        "total": len(rows),
        "shown": len(shown),
        "ready": sum(1 for row in shown if row["ready"]),
        "problems": sum(1 for row in shown if not row["ready"]),
        "unparsed": max(0, len(rows) - len(shown)),
    }


def explain(row: dict) -> str:
    """One readable sentence for a parsed row (used in tests and tooltips)."""
    return " · ".join(row.get("how") or []) or "nothing understood"
