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


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


# ----- Database -----
DB_PATH = Path(_env("HUB_DB_PATH", str(BASE_DIR / "influencer_hub" / "hub.sqlite3")))

# ----- Amazon Associates / Creators API -----
# The configured Associate tag is a public identifier, not an API credential.
# It is only a fallback for new/unspecified profiles; use each creator's own
# supplied tag whenever one is available.
AMAZON_ASSOCIATE_TAG = _env("AMAZON_ASSOCIATE_TAG", "mama086-21")
DEFAULT_AMAZON_TAG = AMAZON_ASSOCIATE_TAG
AMAZON_CREATORS_API_CLIENT_ID = _env(
    "AMAZON_CREATORS_API_CLIENT_ID",
    "amzn1.application-oa2-client.83229d9d14664351be9fc2059038a4f2",
)
AMAZON_CREATORS_API_CLIENT_SECRET = _env("AMAZON_CREATORS_API_CLIENT_SECRET")
# Amazon assigns a version to each Creators API credential. The default is
# configurable; set this to the exact version shown in Associates Central.
AMAZON_CREATORS_API_VERSION = _env("AMAZON_CREATORS_API_VERSION", "3.2")
AMAZON_CREATORS_API_MARKETPLACE = _env("AMAZON_CREATORS_API_MARKETPLACE", "www.amazon.in")
AMAZON_CREATORS_API_ENDPOINT = _env(
    "AMAZON_CREATORS_API_ENDPOINT", "https://creatorsapi.amazon/catalog/v1"
)
AMAZON_CREATORS_API_APP_NAME = _env("AMAZON_CREATORS_API_APP_NAME", "SMART_BUY")
AMAZON_CREATORS_API_TIMEOUT = _int("AMAZON_CREATORS_API_TIMEOUT", 15)
# One optional, operator-owned HTTPS origin routed to this Flask app.  It powers
# compact, verified first-party redirects for Amazon (/a/...), HYPD (/m/...) and
# approved LehLah links (/l/...).  The network-specific variables below remain
# available when separate branded domains are wanted.
AFFILIATE_SHORT_LINK_BASE_URL = _env("AFFILIATE_SHORT_LINK_BASE_URL", "").rstrip("/")
# Optional Amazon-only override. Blank inherits AFFILIATE_SHORT_LINK_BASE_URL;
# if both are blank, Amazon stays canonical /dp/<ASIN>?tag=... .
AMAZON_SHORT_LINK_BASE_URL = _env("AMAZON_SHORT_LINK_BASE_URL", "").rstrip("/")

# ----- Telegram (the account that reads joined source dialogs and posts) -----
# API_ID is an application identifier. API_HASH is kept out of source control;
# put the value in the VM's ignored .env file or a secret manager.
TELEGRAM_API_ID = _env("TELEGRAM_API_ID", "33595682")
TELEGRAM_API_HASH = _env("TELEGRAM_API_HASH")
# The existing authorized Telethon SQLite session is used as the seed for a
# process-local session copy so Flask, the worker, and CLI never open the same
# SQLite file concurrently.
TELEGRAM_SESSION = _env("TELEGRAM_SESSION", "bestgaa_fresh")
TELEGRAM_SESSION_ISOLATION = _bool("TELEGRAM_SESSION_ISOLATION", True)
TELEGRAM_SESSION_SLOT = _env("HUB_TELEGRAM_SESSION_SLOT", "")
TELEGRAM_SOURCE_REQUEST_SPACING = max(0.0, _float("TELEGRAM_SOURCE_REQUEST_SPACING", 0.25))
TELEGRAM_OUTPUT_CHANNELS = [
    s.strip() for s in _env("TELEGRAM_OUTPUT_CHANNELS", "").split(",") if s.strip()
]
# The posting bot we optionally add as admin to created channels.
BOT_TOKEN = _env("BOT_TOKEN")
BOT_USERNAME = _env("BOT_USERNAME", "your_posting_bot")  # without leading @

# ----- Continuous deal worker -----
DEAL_WORKER_POLL_INTERVAL = _int("DEAL_WORKER_POLL_INTERVAL", 30)
DEAL_WORKER_BATCH_SIZE = _int("DEAL_WORKER_BATCH_SIZE", 100)
DEAL_WORKER_INITIAL_BATCH_SIZE = _int("DEAL_WORKER_INITIAL_BATCH_SIZE", 10)
DEAL_WORKER_MAX_BACKOFF = _int("DEAL_WORKER_MAX_BACKOFF", 900)
HYPD_STORE_ID = _env("HYPD_STORE_ID", "93944")
# Account model: Amazon uses each creator's own Associate tag, while
# EarnKaro and Meesho/HYPD use OUR central accounts. On (the default) a
# per-creator or per-channel hypd_store_id cannot move HYPD commission
# away from our store. See influencer_hub/accounts.py.
CENTRAL_NETWORK_ACCOUNTS = _bool("CENTRAL_NETWORK_ACCOUNTS", True)
# Optional branded first-party domain for shortening existing HYPD and LehLah
# Meesho affiliate links (e.g. https://go.yourbrand.in). No API key is needed.
MEESHO_SHORT_LINK_BASE_URL = _env("MEESHO_SHORT_LINK_BASE_URL", "").rstrip("/")
# LehLah short redirects stay disabled until your account is approved and an
# operator explicitly enables this setting.
LEHLAH_SHORTLINKS_ENABLED = _bool("LEHLAH_SHORTLINKS_ENABLED", False)
# HYPD cannot turn a raw meesho.com product URL into an affiliate link, so that
# deal would post for free. With this fallback the raw link is sent to EarnKaro
# instead (conversion is verified like any other merchant link).
MEESHO_EARNKARO_FALLBACK = _bool("MEESHO_EARNKARO_FALLBACK", True)
# Hold back deals whose links would all post without our attribution. Volume vs
# earnings trade-off, so the operator decides; the Money Radar recommends it.
ONLY_EARNING_DEALS = _bool("ONLY_EARNING_DEALS", False)
# A deal whose only links are raw merchant/raw Meesho URLs (EarnKaro down, bad
# key, or an unsupported merchant) pays exactly zero. Default: hold it back for
# this cycle instead of spending a post — the worker re-reads the source post,
# so it goes out as soon as EarnKaro converts it. Set to 1 to post anyway.
ALLOW_UNCONVERTED_POSTS = _bool("ALLOW_UNCONVERTED_POSTS", False)

# ----- EarnKaro (optional configured network for eligible non-Amazon links) -----
EARNKARO_API_KEY = _env("EARNKARO_API_KEY")
# Known owner ID used by the dashboard's vault default; explicit env/DB values override it.
EARNKARO_PUBLISHER_ID = _env("EARNKARO_PUBLISHER_ID", "5478322")
EARNKARO_API_URL = _env("EARNKARO_API_URL", "https://ekaro-api.affiliaters.in/api/converter/public")

# ----- Shared deal pool (one central source list for all influencers) -----
# Comma separated Telegram source channel usernames (without @) or t.me links.
SHARED_SOURCES = [s for s in _env("SHARED_SOURCES", "").split(",") if s]
# Dummy / test source channels — kept separate, used only when an influencer
# is flagged use_dummy_sources=true (safe staging before going live).
DUMMY_SOURCES = [s for s in _env("DUMMY_SOURCES", "").split(",") if s]

# ----- WhatsApp hub (Node/Baileys multi-session service) -----
WA_HUB_URL = _env("WA_HUB_URL", "http://127.0.0.1:8088")
WA_HUB_TOKEN = _env("WA_HUB_TOKEN", "")  # required by the hub in production

# ----- Ops / monitoring -----
# Telegram channel (username or id) where VM/bot health is reported.
OPS_TELEGRAM_CHANNEL = _env("OPS_TELEGRAM_CHANNEL", "")
VM_WATCH_INTERVAL = _int("VM_WATCH_INTERVAL", 300)  # seconds

# ----- Bitly shortener (for eligible links only) -----
BITLY_API_KEY = _env("BITLY_API_KEY", "")
# Global API credentials are opt-in per owned influencer database ID. A blank
# allowlist means the global key is not used for anyone; per-profile/channel
# keys continue to work normally.
BITLY_GLOBAL_INFLUENCER_IDS = {
    value.strip()
    for value in _env("BITLY_GLOBAL_INFLUENCER_IDS").split(",")
    if value.strip()
}

# ----- Advanced shortener for ONLY OUR affiliate links (HYPD + Amazon) -----
# When enabled, ONLY links with OUR store ID / OUR Amazon tag are shortened
# via first-party (if base URL configured) or Bitly fallback. This is the
# ADVANCED system the user requested: "ONLY MANA LINK KI MATHARME"
ADVANCED_SHORTENER_ENABLED = _bool("ADVANCED_SHORTENER_ENABLED", True)
AMAZON_ADVANCED_SHORTENER_ENABLED = _bool("AMAZON_ADVANCED_SHORTENER_ENABLED", True)
HYPD_ADVANCED_SHORTENER_ENABLED = _bool("HYPD_ADVANCED_SHORTENER_ENABLED", True)
# Bitly fallback for OUR links when first-party base URL not configured
# Default False for backward compat with existing tests; user can enable via vault
ADVANCED_BITLY_FALLBACK_ENABLED = _bool("ADVANCED_BITLY_FALLBACK_ENABLED", False)
# When True, generic merchant Bitly (for long Flipkart etc.) is disabled;
# ONLY OUR affiliate links (Amazon/HYPD/EarnKaro) are shortened. User requested "ONLY MANA LINK KI"
ADVANCED_ONLY_OUR_LINKS = _bool("ADVANCED_ONLY_OUR_LINKS", False)

# ----- Dashboard security -----
# No default admin password is shipped. The dashboard fails closed until a
# password is set in the private environment. ADMIN_DELETE_PASSWORD may be
# separate; if omitted, it inherits the dashboard password.
_ADMIN_DELETE_PASSWORD = _env("ADMIN_DELETE_PASSWORD")
DASHBOARD_ADMIN_PASSWORD = _env("DASHBOARD_ADMIN_PASSWORD") or _ADMIN_DELETE_PASSWORD
ADMIN_DELETE_PASSWORD = _ADMIN_DELETE_PASSWORD or DASHBOARD_ADMIN_PASSWORD
DASHBOARD_SECRET_KEY = _env("DASHBOARD_SECRET_KEY")
# Every Gunicorn worker must sign session cookies with the SAME key. When
# DASHBOARD_SECRET_KEY is unset, a generated key is persisted to this private
# file so a session created by one worker is not rejected by the next one
# (which shows up as endless redirects to /login and 400 CSRF failures).
DASHBOARD_SECRET_KEY_FILE = Path(
    _env("DASHBOARD_SECRET_KEY_FILE", str(BASE_DIR / ".dashboard_secret_key"))
)
# Sensitive setup changes (adding/saving/deleting channels, …) ask for the
# admin password ONCE and then stay unlocked for this many seconds of work.
# The window is idle-based: every confirmed change renews it, so an active
# operator is never interrupted, while an idle dashboard locks itself again.
DASHBOARD_SETUP_UNLOCK_SECONDS = _int("DASHBOARD_SETUP_UNLOCK_SECONDS", 1800)
# Bind the unlock to the IP that confirmed it, so a stolen cookie cannot be
# replayed from another machine. Disable only behind a fixed-IP tunnel.
DASHBOARD_SETUP_UNLOCK_BIND_IP = _bool("DASHBOARD_SETUP_UNLOCK_BIND_IP", True)
# Trust X-Forwarded-* from one reverse proxy hop (HTTPS termination). Leave
# off for direct-to-port access so a client cannot spoof "https".
DASHBOARD_TRUST_PROXY = _bool("DASHBOARD_TRUST_PROXY", False)
HUB_ENV = _env("HUB_ENV", "development").lower()
DASHBOARD_COOKIE_SECURE = _bool(
    "DASHBOARD_COOKIE_SECURE", default=HUB_ENV == "production"
)

# ----- Pipeline pacing -----
QUEUE_WORKERS = _int("HUB_QUEUE_WORKERS", 4)
POST_QUIET_START = _env("HUB_POST_QUIET_START", "02:00")
POST_QUIET_END = _env("HUB_POST_QUIET_END", "06:00")
