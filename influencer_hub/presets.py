"""What every new creator starts with — set once by the owner, inherited by every add.

The team adds creators all day; they must never have to decide which networks are
on, which HYPD store to use or whether the main channel mirrors to WhatsApp. The
owner answers those questions **once** here, and every add path (the team page,
Easy Setup, a bulk paste, the inventory import and the CLI) reads them:

```python
presets.load()            # {'allow_amazon': True, ..., 'mirror_whatsapp': True}
presets.describe()        # [{'key': 'allow_amazon', 'label': 'Amazon links', ...}, ...]
presets.save({'only_amazon': True}, actor='admin')
```

Stored as JSON in the hub's own settings table (so it travels with the database,
survives restarts, and needs no .env edit). Empty means "leave the code default",
which is exactly what the hub did before this existed — so an untouched hub
behaves the same while a configured one can be set up once and left alone.
"""
from __future__ import annotations

import json

from . import config, db

STORED_KEY = "add_defaults"

DEFAULTS: dict[str, object] = {
    "allow_amazon": True,
    "allow_earnkaro": True,
    "allow_hypd": True,
    "only_amazon": False,
    "categories": "",
    "posting_schedule": "",
    "price_filter": "all",
    "mirror_whatsapp": True,
    "hypd_store_id": "",          # "" = the hub's effective central store at add time
}

FLAGS = ("allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon", "mirror_whatsapp")
TEXTS = ("categories", "posting_schedule", "price_filter", "hypd_store_id")

LABELS = {
    "allow_amazon": ("Amazon links", "Post Amazon deals with the creator's own tag"),
    "allow_earnkaro": ("EarnKaro links", "Convert other links through the central account"),
    "allow_hypd": ("HYPD / Meesho", "Convert Meesho links through the central HYPD store"),
    "only_amazon": ("Amazon-only channels", "Skip every non-Amazon deal for new channels"),
    "categories": ("Categories", "Deal categories, comma separated (empty = all)"),
    "posting_schedule": ("Posting hours", "When deals may be posted (empty = any time)"),
    "price_filter": ("Price filter", "all / budget / premium …"),
    "mirror_whatsapp": ("Main → WhatsApp mirror", "New main channels mirror into WhatsApp"),
    "hypd_store_id": ("HYPD store id", "Our central store (empty = the hub's current one)"),
}


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def load() -> dict:
    """The defaults every add path should start from (never raises)."""
    values = dict(DEFAULTS)
    try:
        raw = db.get_global_setting(STORED_KEY, "")
    except Exception:  # pragma: no cover - a missing table must not break an add
        return values
    if not raw:
        return values
    try:
        stored = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        return values
    if not isinstance(stored, dict):
        return values
    for key, value in stored.items():
        if key in FLAGS:
            values[key] = _bool(value, bool(DEFAULTS[key]))
        elif key in TEXTS:
            values[key] = str(value or "").strip()
    return values


def save(updates: dict, *, actor: str = "admin", ip: str = "") -> dict:
    """Store a partial change (only known keys) and return the merged defaults."""
    current = load()
    changed: list[str] = []
    for key, value in (updates or {}).items():
        if key not in DEFAULTS:
            continue
        wanted = _bool(value, bool(current.get(key, DEFAULTS[key]))) if key in FLAGS \
            else str(value or "").strip()
        if current.get(key) != wanted:
            current[key] = wanted
            changed.append(key)
    if not changed:
        return current
    try:
        db.set_global_setting(STORED_KEY, json.dumps(current, sort_keys=True))
    except Exception as exc:  # pragma: no cover - report, do not crash the page
        raise RuntimeError(f"could not save the new-creator defaults: {exc}") from exc
    try:
        from . import security

        security.audit(actor, security.ADMIN, "add_defaults_changed",
                       detail=", ".join(sorted(changed)), ip=ip)
    except Exception:  # pragma: no cover - audit is best effort here
        pass
    return current


def resolved() -> dict:
    """Defaults with the hub's live fallbacks filled in (what an add really uses)."""
    values = load()
    if not str(values.get("hypd_store_id") or "").strip():
        try:
            from . import accounts

            values["hypd_store_id"] = str(accounts.central_hypd_store_id() or "")
        except Exception:  # pragma: no cover - display only
            values["hypd_store_id"] = ""
    if not str(values.get("price_filter") or "").strip():
        values["price_filter"] = "all"
    return values


def apply_to(values: dict) -> dict:
    """Overlay the defaults on an add form's values (blank fields only)."""
    resolved_values = resolved()
    merged = dict(values)
    for key, value in resolved_values.items():
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def describe(values: dict | None = None) -> list[dict]:
    """Rows for the setup card / the team page ("your adds start with …")."""
    current = resolved() if values is None else dict(values)
    rows: list[dict] = []
    for key in ("allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon",
                "mirror_whatsapp", "categories", "posting_schedule", "price_filter",
                "hypd_store_id"):
        label, hint = LABELS.get(key, (key, ""))
        value = current.get(key, DEFAULTS.get(key))
        if isinstance(value, bool):
            shown = "on" if value else "off"
        else:
            shown = str(value or "").strip() or ("all" if key == "price_filter" else "default")
        rows.append({"key": key, "label": label, "hint": hint,
                     "value": value, "shown": shown,
                     "is_flag": key in FLAGS})
    return rows


def summary_line(values: dict | None = None) -> str:
    """One sentence for the team page and the console."""
    current = resolved() if values is None else dict(values)
    on = [name for name, key in (("Amazon", "allow_amazon"), ("EarnKaro", "allow_earnkaro"),
                                 ("HYPD", "allow_hypd"))
          if current.get(key)]
    store = str(current.get("hypd_store_id") or "").strip()
    bits = ", ".join(on) or "no networks"
    if current.get("only_amazon"):
        bits += " · Amazon-only"
    if current.get("mirror_whatsapp"):
        bits += " · mirror on"
    if store and current.get("allow_hypd"):
        bits += f" · HYPD store {store}"
    schedule = str(current.get("posting_schedule") or "").strip()
    if schedule:
        bits += f" · hours {schedule}"
    return f"New creators start with: {bits}."


def env_effective() -> dict:
    """The code-level fallbacks, for the card's "reset" text."""
    return {"mirror_default_from_env": bool(config.MIRROR_MAIN_TO_WHATSAPP)}
