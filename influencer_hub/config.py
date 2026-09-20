"""Centralised configuration, read lazily from the environment.

Nothing here throws at import time — every value has a sane default so the
pure-logic modules (link_router, db) can be imported and unit-tested on a
machine with no provisioned .env.
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dependency).

    Reads <repo>/.env so `python -m influencer_hub.cli` and the dashboard pick
    up secrets without manual `export`. Already-set env vars win. Kept out of
    git via .gitignore.
    """
    p = Path(__file__).resolve().parent.parent / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

BASE_DIR = Path(os.getenv("HUB_BASE_DIR", str(Path(__file__).resolve().parent.parent)))


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    if not v:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# ----- Database -----
DB_PATH = Path(_env("HUB_DB_PATH", str(BASE_DIR / "influencer_hub" / "hub.sqlite3")))

# ----- Telegram (our bot account that owns the created channels) -----
TELEGRAM_API_ID = _env("TELEGRAM_API_ID")
TELEGRAM_API_HASH = _env("TELEGRAM_API_HASH")
# Telethon session file used by the existing bestgaa bot; we reuse it so the
# channels we create live under the SAME account that already runs 2 channels.
TELEGRAM_SESSION = _env("TELEGRAM_SESSION", "bestgaa_fresh")
# The posting bot we add as admin to every created channel.
BOT_TOKEN = _env("BOT_TOKEN")
BOT_USERNAME = _env("BOT_USERNAME", "your_posting_bot")  # without leading @

# ----- EarnKaro (OUR publisher id — used for every non-Amazon merchant link) -----
EARNKARO_API_KEY = _env("EARNKARO_API_KEY")
EARNKARO_PUBLISHER_ID = _env("EARNKARO_PUBLISHER_ID")
EARNKARO_API_URL = _env("EARNKARO_API_URL", "https://ekaro-api.affiliaters.in/api/converter/public")

# ----- Shared deal pool (one central source list for all influencers) -----
# Comma separated Telegram source channel usernames (without @) or t.me links.
SHARED_SOURCES = [s for s in _env("SHARED_SOURCES", "").split(",") if s]
# Dummy / test source channels — kept separate, used only when an influencer
# is flagged use_dummy_sources=true (safe staging before going live).
DUMMY_SOURCES = [s for s in _env("DUMMY_SOURCES", "").split(",") if s]

# ----- WhatsApp hub (Node/baileys multi-session service) -----
WA_HUB_URL = _env("WA_HUB_URL", "http://127.0.0.1:8088")
WA_HUB_TOKEN = _env("WA_HUB_TOKEN", "")  # optional shared secret

# ----- Ops / monitoring -----
# Telegram channel (username or id) where VM/bot health is reported.
OPS_TELEGRAM_CHANNEL = _env("OPS_TELEGRAM_CHANNEL", "")
VM_WATCH_INTERVAL = _int("VM_WATCH_INTERVAL", 300)  # seconds

# ----- Pipeline pacing -----
QUEUE_WORKERS = _int("HUB_QUEUE_WORKERS", 4)
POST_QUIET_START = _env("HUB_POST_QUIET_START", "02:00")
POST_QUIET_END = _env("HUB_POST_QUIET_END", "06:00")
