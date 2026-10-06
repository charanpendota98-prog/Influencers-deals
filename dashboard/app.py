"""Influencer hub dashboard (Flask).

Single-origin app that:
  * lists / registers influencers
  * creates the Telegram channel for an influencer (via Telethon)
  * pairs the influencer's WhatsApp number (QR) through the wa_hub service,
    then creates a WA group feed + official Channel
  * shows per-influencer post stats and live VM/bot health

Run:  PYTHONPATH=.. FLASK_ENV=development python app.py
Binds 0.0.0.0 so it is reachable from the live preview.
"""
from __future__ import annotations

import asyncio
import atexit
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from influencer_hub import (
    config, db, hypd_shortlinks, lehlah_shortlinks, link_router, polls,
    puller, telegram_ops, whatsapp_client,
)  # noqa: E402

def _running_under_pytest() -> bool:
    """True inside a pytest run, where no key file should ever be written."""
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _resolve_dashboard_secret_key() -> str:
    """Return one stable session-signing key shared by every Gunicorn worker.

    Without a shared key, worker A signs a session that worker B rejects, so a
    login page rendered by A always fails its CSRF check on B (HTTP 400) and
    the operator is bounced back to /login forever. The key is taken from
    DASHBOARD_SECRET_KEY when set; otherwise a generated key is persisted in a
    private 0600 file that all workers on this host read.
    """
    if config.DASHBOARD_SECRET_KEY:
        return config.DASHBOARD_SECRET_KEY
    if _running_under_pytest():
        return secrets.token_hex(32)
    path = Path(config.DASHBOARD_SECRET_KEY_FILE)
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        # No key yet: create one so every worker (and every restart) agrees.
        path.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_hex(32)
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(generated)
        return generated
    except OSError:
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _resolve_dashboard_secret_key()
app.config.update(
    SESSION_COOKIE_NAME="influencer_hub_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.DASHBOARD_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
if config.DASHBOARD_TRUST_PROXY:
    # One trusted reverse-proxy hop (HTTPS termination) so request.is_secure
    # and the client IP stay correct behind nginx/Caddy/Tailscale.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"
_LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
# Password policy: the password is asked only at sign-in and before a REMOVAL
# (delete a channel, a creator, or a deal source) for a rolling idle window
# (see config.DASHBOARD_SETUP_UNLOCK_SECONDS). Adding, saving, toggling and
# polling cost no extra confirmation; a removal asks once per window.
DEFAULT_SETUP_UNLOCK_SECONDS = 30 * 60
MIN_SETUP_UNLOCK_SECONDS = 60
SETUP_UNLOCK_AT_KEY = "_setup_unlock_at"
SETUP_UNLOCK_IP_KEY = "_setup_unlock_ip"
UNDO_CHANNEL_KEY = "_undo_channel"
REAUTH_REQUIRED_ENDPOINTS = frozenset({
    "delete_channel", "delete_influencer", "delete_deal_source",
})

DEFAULT_SOURCE_CATALOG = [
    ("Meesho Deals Official", "https://t.me/+6LA1ljXGlbNmMjA1"),
    ("Shopsy Loots Official", "https://t.me/+O3j4ghbtJzhjZjJl"),
    ("Premium Loot Deals", "https://t.me/+HUga1JTHwhBmNDE1"),
    ("Mega Loot Deals", "https://t.me/+8KzU3P58MJ9jN2M1"),
    ("Under 99 Special Loots", "https://t.me/+LP6MYEpCwi0zOGYx"),
    ("VIP Secret Loot Pool", "https://t.me/+qhlEwwkhb2hlNWZl"),
    ("Flash Lootzone Tricks", "https://t.me/+uV5wcTkUWJEwM2Y1"),
    ("Fast Deals Network", "https://t.me/+WvEWEYf7j3MyYzNl"),
    ("Instant Trick Alerts", "https://t.me/+t--iQ-QFeJZiNmVl"),
    ("Prime Lightning Deals", "https://t.me/+ky8g5O5KTr5mZmQ9"),
    ("Loot Matrix Network", "https://t.me/+LNRQ0Y1-9RkzZDRl"),
    ("Discount Express", "https://t.me/+-mv6ttVsltczNzFl"),
    ("OZ Mega Source Pool", "https://t.me/+vZKuuHCZcX44M2I1"),
    ("Fast Loot Tracker", "https://t.me/+FpXKV70NYNY0NzQ1"),
    ("PowerLoot Official", "@powerloot"),
    ("Deals Under 99", "@DealsUnder99_com"),
    ("Under 99 Loot Deals", "@under_99_loot_deals"),
    ("Loot Alerts Direct", "@loot_alerts"),
    ("Telugu Techworld Loots", "@TeluguTechworld"),
    ("Flipkarthiik Loots", "@Flipkarthiik"),
    ("SmartBuy Loots & Deals", "@SB_Loots_And_Deals"),
    ("IDOffers Prime", "@idoffers"),
    ("IDOffers 2", "@idoffers2"),
    ("Indian Online Offers", "@indian_online_offer"),
    ("TechGlare Deals", "@techglaredeals"),
    ("PriceHistory Deals", "@pricehistory"),
    ("DealDost Community", "@dealdost"),
    ("Magix Deals", "@Magixdeals_Magix"),
    ("Deals Velocity", "@dealsvelocity"),
    ("Rapid Deals Unlimited", "@rapiddeals_unlimited"),
    ("Meesho Shopsy Offers", "@msho_shpsy_offers"),
    ("Myntra Ajio Shopsy Deals", "@Myntra_Ajio_Deals_Shopsy"),
    ("GrabOn Deals India", "@GrabOnIndiaOfficial"),
    ("Hidden Deals Amazon", "@hidden_loot_deals_amazon"),
    ("Real Shopping Deals", "@RealShoppingDeals"),
    ("iCoolz Tricks & Loots", "@icoolzTricks"),
]



_ASYNC_LOOP = None
_ASYNC_THREAD = None
_ASYNC_START_LOCK = threading.Lock()


def _start_async_loop():
    """Start one long-lived loop so Flask never reuses a Telethon client across loops."""
    global _ASYNC_LOOP, _ASYNC_THREAD
    with _ASYNC_START_LOCK:
        if _ASYNC_LOOP is not None and _ASYNC_LOOP.is_running():
            return _ASYNC_LOOP

        ready = threading.Event()
        loop = asyncio.new_event_loop()

        def _serve():
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        thread = threading.Thread(target=_serve, name="dashboard-asyncio", daemon=True)
        thread.start()
        ready.wait(timeout=5)
        _ASYNC_LOOP, _ASYNC_THREAD = loop, thread
        return loop


def _run(coro):
    """Run async I/O on the dashboard's single stable event loop."""
    loop = _start_async_loop()
    try:
        active_loop = asyncio.get_running_loop()
    except RuntimeError:
        active_loop = None
    if active_loop is loop:
        raise RuntimeError("dashboard _run cannot block its own asyncio event loop")
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


def _form_flag(name: str, default: bool | None = None) -> bool | None:
    """Parse select values and checkbox/hidden-input pairs consistently.

    HTML checkboxes are omitted when unchecked; some channel forms also submit a
    hidden ``0`` followed by a checkbox ``1``. Checking any submitted value for
    truth avoids Flask MultiDict's first-value behavior from silently disabling
    checked affiliate options.
    """
    values = request.form.getlist(name)
    if not values:
        return default
    truthy = {"1", "true", "yes", "on", "enabled", "active"}
    return any(str(value).strip().lower() in truthy for value in values)


def _effective_hypd_store_id() -> str:
    """OUR central HYPD Store ID (vault/global setting, then environment).

    Resolved through influencer_hub.accounts so the dashboard and the pipeline
    can never disagree about which store earns.
    """
    try:
        from influencer_hub import accounts

        return accounts.central_hypd_store_id()
    except Exception:  # pragma: no cover - defensive
        configured = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)
        return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID


def _routing_hypd_store(requested: str = "") -> str:
    """The HYPD store a post will really carry.

    With central accounts on (the default) that is always OUR store: a value
    typed for one creator cannot move HYPD commission elsewhere. With the
    setting off the requested/profile value is used, so the legacy per-creator
    behaviour stays reachable.
    """
    try:
        from influencer_hub import accounts, config as hub_config

        if accounts.central_network_accounts_enabled():
            return accounts.central_hypd_store_id()
        requested = str(requested or "").strip()
        if requested:
            return requested
        setting = db.get_global_setting("hypd_store_id", hub_config.HYPD_STORE_ID)
        return str(setting or hub_config.HYPD_STORE_ID).strip() or hub_config.HYPD_STORE_ID
    except Exception:  # pragma: no cover - defensive
        return str(requested or "").strip() or _effective_hypd_store_id()


def _effective_earnkaro_publisher_id() -> str:
    configured = db.get_global_setting(
        "earnkaro_publisher_id", config.EARNKARO_PUBLISHER_ID or "5478322"
    )
    return str(configured or config.EARNKARO_PUBLISHER_ID or "5478322").strip()


def _shutdown_async_loop():  # pragma: no cover - process shutdown
    loop = _ASYNC_LOOP
    thread = _ASYNC_THREAD
    if loop is not None and loop.is_running():
        try:
            from influencer_hub import telegram_ops
            asyncio.run_coroutine_threadsafe(telegram_ops.disconnect(), loop).result(timeout=3)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(timeout=3)


atexit.register(_shutdown_async_loop)


def clean_identifier(raw: str) -> str:
    """Normalize Telegram channel link/username or WhatsApp JID.
    e.g.:
      'https://t.me/ravi_loots'         -> '@ravi_loots'
      'https://t.me/+AbCdEf'            -> 'https://t.me/+AbCdEf' (Preserves invite links!)
      'https://chat.whatsapp.com/...'   -> WhatsApp invite link preserved
      '120363xxx@g.us'                  -> WhatsApp group JID preserved
      'ravi_loots'                      -> '@ravi_loots'
      '-100123456789'                   -> '-100123456789'
    """
    s = raw.strip()
    if not s:
        return ""

    # Preserve WhatsApp links and JIDs
    if "chat.whatsapp.com" in s or s.endswith("@g.us") or s.endswith("@newsletter") or s.startswith("120363"):
        return s

    # Preserve private Telegram invite links (+hash or joinchat/hash)
    if "t.me/+" in s or "telegram.me/+" in s or "joinchat/" in s or s.startswith("+"):
        if not s.startswith("http") and s.startswith("+"):
            return f"https://t.me/{s}"
        return s

    # Strip url prefix for standard public channels
    s = re.sub(r"^https?://(?:t\.me|telegram\.me)/", "", s, flags=re.I)
    s = re.sub(r"^t\.me/", "", s, flags=re.I)
    s = s.strip("/")

    if s.startswith("-100") or s.isdigit():
        return s
    if "@" in s:
        return s if s.startswith("@") else "@" + s.split("@")[-1]
    return f"@{s}" if not s.endswith(".net") and not s.endswith(".us") else s


def _classify_whatsapp_destination(raw: str) -> tuple[str, str] | None:
    """Validate a WhatsApp JID or an invite URL before using it as a destination."""
    value = str(raw or "").strip()
    if re.fullmatch(r"[0-9]+@newsletter", value):
        return "whatsapp_channel_jid", value
    if re.fullmatch(r"[0-9-]+@g\.us", value):
        return "whatsapp_group_jid", value
    if not value:
        return None

    candidate = value if re.match(r"^https?://", value, re.I) else f"https://{value.lstrip('/')}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() != "https":
        return None
    host = (parsed.hostname or "").lower()
    if host in {"whatsapp.com", "www.whatsapp.com"}:
        match = re.fullmatch(r"/channel/([A-Za-z0-9_-]+)/?", parsed.path, re.I)
        if match:
            return "whatsapp_channel_link", value
    if host == "chat.whatsapp.com":
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)/?", parsed.path)
        if match:
            return "whatsapp_group_link", value
    return None


def _resolve_whatsapp_destination(inf_id: int, raw: str) -> tuple[dict | None, str]:
    """Resolve an explicitly supplied channel/group link to its real WhatsApp JID.

    Newsletter invite resolution uses Baileys metadata; group invite resolution
    only finds the JID and does not silently join the group. A test send is still
    required before a newly linked destination becomes active.
    """
    classified = _classify_whatsapp_destination(raw)
    if not classified:
        return None, "Enter a valid whatsapp.com/channel link, chat.whatsapp.com group invite, or detected WhatsApp JID."

    kind, value = classified
    if kind == "whatsapp_channel_jid":
        return {"platform": "whatsapp_channel", "identifier": value, "invite_link": ""}, ""
    if kind == "whatsapp_group_jid":
        return {"platform": "whatsapp_group", "identifier": value, "invite_link": ""}, ""

    try:
        if kind == "whatsapp_channel_link":
            result = _run(whatsapp_client.resolve_newsletter(WA_SESSION_KEY(inf_id), value))
            expected_suffix = "@newsletter"
            platform = "whatsapp_channel"
        else:
            result = _run(whatsapp_client.resolve_invite(WA_SESSION_KEY(inf_id), value))
            expected_suffix = "@g.us"
            platform = "whatsapp_group"
    except Exception:
        return None, "Could not resolve this link. Pair the influencer's WhatsApp account first, then try again."

    jid = str((result or {}).get("jid") or "").strip()
    valid_jid = (
        re.fullmatch(r"[0-9]+@newsletter", jid)
        if expected_suffix == "@newsletter"
        else re.fullmatch(r"[0-9-]+@g\.us", jid)
    )
    if not (result or {}).get("ok") or not valid_jid:
        return None, "WhatsApp did not resolve that invite to a valid destination. Check the link and paired account."
    return {"platform": platform, "identifier": jid, "invite_link": value}, ""


def _save_whatsapp_destination(
    inf_id: int,
    destination: dict,
    *,
    role: str = "whatsapp",
    allowed_sources: str = "",
    wa_session_key: str = "",
    channel_settings: dict | None = None,
) -> int:
    """Save a new WhatsApp destination as pending, deduplicating the same JID.

    Pending destinations cannot receive feed posts until an operator explicitly
    sends a test message; a successful test activates the channel.
    """
    platform = destination["platform"]
    jid = str(destination.get("identifier") or "").strip()
    if platform == "whatsapp_channel" and not re.fullmatch(r"[0-9]+@newsletter", jid):
        raise ValueError("WhatsApp Channel destination must be a validated @newsletter JID.")
    if platform == "whatsapp_group" and not re.fullmatch(r"[0-9-]+@g\.us", jid):
        raise ValueError("WhatsApp Group destination must be a validated @g.us JID.")
    session_key = wa_session_key or WA_SESSION_KEY(inf_id)
    existing = next(
        (channel for channel in db.list_channels(inf_id)
         if channel.get("platform") == platform and channel.get("identifier") == jid),
        None,
    )
    settings = dict(channel_settings or {})
    if existing:
        db.update_channel_details(
            existing["id"],
            identifier=jid,
            invite_link=destination.get("invite_link") or existing.get("invite_link") or "",
            role=role,
            status="pending",
            allowed_sources=allowed_sources or existing.get("allowed_sources") or "",
            wa_session_key=session_key,
            **settings,
        )
        return int(existing["id"])

    return db.add_channel(
        inf_id,
        platform,
        jid,
        invite_link=destination.get("invite_link") or "",
        status="pending",
        role=role,
        allowed_sources=allowed_sources,
        wa_session_key=session_key,
        **settings,
    )


def _get_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


# --------------------------------------------------------------------------
# Removal unlock: one password confirmation, then a rolling idle window.
# --------------------------------------------------------------------------
def _setup_unlock_window_seconds() -> int:
    """Length of the unlock window, configurable and clamped to sane values."""
    try:
        configured = int(getattr(config, "DASHBOARD_SETUP_UNLOCK_SECONDS", 0) or 0)
    except (TypeError, ValueError):
        configured = 0
    return max(MIN_SETUP_UNLOCK_SECONDS, configured or DEFAULT_SETUP_UNLOCK_SECONDS)


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _setup_unlock_remaining_seconds() -> int:
    """Seconds left before the dashboard asks for the password again."""
    stamp = session.get(SETUP_UNLOCK_AT_KEY)
    try:
        stamp = float(stamp)
    except (TypeError, ValueError):
        return 0
    if getattr(config, "DASHBOARD_SETUP_UNLOCK_BIND_IP", True) and (
        session.get(SETUP_UNLOCK_IP_KEY) != _client_ip()
    ):
        return 0
    remaining = int(stamp + _setup_unlock_window_seconds() - time.time())
    return remaining if remaining > 0 else 0


def _setup_unlocked() -> bool:
    return _setup_unlock_remaining_seconds() > 0


def _grant_setup_unlock() -> None:
    session[SETUP_UNLOCK_AT_KEY] = time.time()
    session[SETUP_UNLOCK_IP_KEY] = _client_ip()


def _renew_setup_unlock() -> None:
    """Sliding window: each confirmed removal restarts the idle timer."""
    if _setup_unlocked():
        session[SETUP_UNLOCK_AT_KEY] = time.time()


def _clear_setup_unlock() -> None:
    session.pop(SETUP_UNLOCK_AT_KEY, None)
    session.pop(SETUP_UNLOCK_IP_KEY, None)


def _expects_json_response() -> bool:
    """True for fetch()/API callers that must keep receiving JSON, not HTML."""
    if request.path.startswith("/api/"):
        return True
    if (request.headers.get("X-Requested-With") or "").lower() in {"fetch", "xmlhttprequest"}:
        return True
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" in accept and "application/json" not in accept:
        return False
    return True


def _local_referrer(*, with_reauth: bool) -> str:
    """Return the page the browser came from, with or without the lock flag."""
    referrer = request.referrer or request.form.get("next") or ""
    if not referrer:
        return ""
    parsed = urlparse(referrer)
    same_host = not parsed.netloc or parsed.netloc == request.host
    if not (same_host and parsed.path.startswith("/")):
        return ""
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key != "reauth"]
    if with_reauth:
        query.append(("reauth", "1"))
    return f"{parsed.path}?{urlencode(query)}" if query else parsed.path


def _reauth_return_url() -> str:
    """Send a browser form post back to the page it came from, flagged locked."""
    return _local_referrer(with_reauth=True) or url_for("index", reauth=1)


def _csrf_failure_response():
    """Reject a bad CSRF token without dumping JSON into an operator's browser."""
    error = {"ok": False, "error": "csrf_validation_failed"}
    if request.endpoint == "login":
        # A lost/expired session on the sign-in screen: hand back a fresh page
        # instead of a bare 400 so the operator can simply sign in again.
        return render_template(
            "login.html",
            next_url=request.values.get("next", ""),
            login_error="Your sign-in session expired. Enter the admin password again.",
        ), 400
    if not _expects_json_response():
        flash("Your session expired. Sign in again and repeat the change.", "warning")
        return redirect(url_for("login", next=_safe_local_redirect(request.path)))
    return jsonify(error), 400


@app.context_processor
def inject_dashboard_security_context():
    remaining = _setup_unlock_remaining_seconds() if session.get("dashboard_authenticated") else 0
    return {
        "csrf_token": _get_csrf_token,
        "dashboard_authenticated": bool(session.get("dashboard_authenticated")),
        "setup_unlocked": remaining > 0,
        "setup_unlock_remaining": remaining,
        "setup_unlock_minutes": max(1, remaining // 60),
        "setup_unlock_window_minutes": _setup_unlock_window_seconds() // 60,
        "undo_channel": session.get(UNDO_CHANNEL_KEY),
        "insecure_login": (
            config.HUB_ENV == "production" and not request.is_secure
        ),
    }


@app.before_request
def protect_dashboard_routes():
    """Require an admin session and same-session CSRF token for dashboard actions.

    Public short-link redirects and liveness checks are intentionally exempt.
    Tests can opt into Flask's TESTING mode; real deployments fail closed until
    DASHBOARD_ADMIN_PASSWORD (or the legacy ADMIN_DELETE_PASSWORD) is configured.
    """
    if app.testing:
        return None

    endpoint = request.endpoint
    public_endpoints = {
        "static", "login", "healthz", "amazon_short_link",
        "meesho_hypd_short_link", "lehlah_meesho_short_link",
    }
    # Only protect the session cookie when the browser can actually use it:
    # a Secure cookie over plain HTTP is silently dropped, which locks the
    # operator out of the dashboard entirely.
    app.config["SESSION_COOKIE_SECURE"] = bool(config.DASHBOARD_COOKIE_SECURE) and request.is_secure
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = str(session.get("_csrf_token") or "")
        supplied = str(
            request.form.get("_csrf_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return _csrf_failure_response()

    if endpoint in public_endpoints:
        return None

    if not _dashboard_security_ready():
        return render_template("setup_required.html"), 503

    if not session.get("dashboard_authenticated"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "authentication_required"}), 401
        destination = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=destination))

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and endpoint in REAUTH_REQUIRED_ENDPOINTS:
        # Ask for the password once, then keep the dashboard unlocked for a
        # rolling idle window so the next add/save/delete does not ask again.
        if not _setup_unlocked():
            _clear_setup_unlock()
            if _expects_json_response():
                return jsonify({
                    "ok": False,
                    "error": "reauthentication_required",
                    "unlock_url": url_for("reauthenticate_setup_change"),
                }), 428
            flash(
                "Removals are locked. Confirm the dashboard password once, "
                "then every delete stays unlocked for "
                f"{_setup_unlock_window_seconds() // 60} minutes. "
                "Adding and saving never ask.",
                "warning",
            )
            return redirect(_reauth_return_url())
        _renew_setup_unlock()
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'",
    )
    if request.endpoint not in {"static", "amazon_short_link", "meesho_hypd_short_link", "lehlah_meesho_short_link", "healthz"}:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _dashboard_secret_key_is_persistent() -> bool:
    """True when sessions survive a restart (env key or the generated key file)."""
    if config.DASHBOARD_SECRET_KEY:
        return True
    try:
        return Path(config.DASHBOARD_SECRET_KEY_FILE).exists()
    except OSError:
        return False


def _dashboard_security_ready() -> bool:
    if not config.DASHBOARD_ADMIN_PASSWORD:
        return False
    if config.HUB_ENV == "production":
        return (
            _dashboard_secret_key_is_persistent()
            and len(config.DASHBOARD_ADMIN_PASSWORD) >= 16
        )
    return True


def _password_matches(supplied: str, expected: str) -> bool:
    expected = str(expected or "")
    supplied = str(supplied or "")
    return bool(expected) and hmac.compare_digest(supplied, expected)


def _safe_local_redirect(destination: str | None) -> str:
    candidate = (destination or "").strip()
    parsed = urlparse(candidate)
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or any(ord(character) < 32 or ord(character) == 127 for character in candidate)
        or parsed.scheme
        or parsed.netloc
    ):
        return url_for("index")
    return candidate


@app.route("/healthz")
def healthz():
    """Unauthenticated liveness endpoint with no operational or secret details."""
    return jsonify({"ok": True})


@app.route("/login", methods=["GET", "POST"])
def login():
    if not _dashboard_security_ready():
        return render_template("setup_required.html"), 503
    if session.get("dashboard_authenticated"):
        return redirect(_safe_local_redirect(request.args.get("next")))

    next_url = request.values.get("next", "")
    if request.method == "GET":
        return render_template("login.html", next_url=next_url, login_error="")

    client_ip = request.remote_addr or "unknown"
    now = time.monotonic()
    recent = [
        timestamp for timestamp in _LOGIN_FAILURES[client_ip]
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]
    _LOGIN_FAILURES[client_ip] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        return render_template(
            "login.html", next_url=next_url,
            login_error="Too many sign-in attempts. Try again in 15 minutes.",
        ), 429

    supplied = request.form.get("password", "")
    if not hmac.compare_digest(supplied, config.DASHBOARD_ADMIN_PASSWORD):
        _LOGIN_FAILURES[client_ip].append(now)
        return render_template(
            "login.html", next_url=next_url,
            login_error="Sign-in failed. Check the password and try again.",
        ), 401

    _LOGIN_FAILURES.pop(client_ip, None)
    session.clear()
    session.permanent = True
    session["dashboard_authenticated"] = True
    session["_csrf_token"] = secrets.token_urlsafe(32)
    return redirect(_safe_local_redirect(next_url))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/reauth", methods=["POST"])
def reauthenticate_setup_change():
    """Confirm the admin password once and unlock removals.

    The unlock is a rolling idle window (default 30 minutes): the first delete
    asks and later deletes do not, unless the dashboard goes idle, is locked
    manually, or signs out. Adding and saving never ask at all.
    """
    if not session.get("dashboard_authenticated"):
        return jsonify({"ok": False, "error": "authentication_required"}), 401

    client_ip = _client_ip()
    bucket_key = f"reauth:{client_ip}"
    now = time.monotonic()
    recent = [
        timestamp for timestamp in _LOGIN_FAILURES[bucket_key]
        if now - timestamp < LOGIN_WINDOW_SECONDS
    ]
    _LOGIN_FAILURES[bucket_key] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        return jsonify({"ok": False, "error": "too_many_attempts"}), 429

    if not _password_matches(request.form.get("password", ""), config.DASHBOARD_ADMIN_PASSWORD):
        _LOGIN_FAILURES[bucket_key].append(now)
        if not _expects_json_response():
            flash("Password did not match. Removals are still locked.", "error")
            return redirect(_reauth_return_url())
        return jsonify({"ok": False, "error": "password_confirmation_failed"}), 401

    _LOGIN_FAILURES.pop(bucket_key, None)
    _grant_setup_unlock()
    minutes = _setup_unlock_window_seconds() // 60
    if not _expects_json_response():
        # Works without JavaScript: the page reloads unlocked so the operator
        # can press the original button again.
        flash(f"Removals unlocked for {minutes} minutes of work.", "success")
        return redirect(_local_referrer(with_reauth=False) or url_for("index"))
    return jsonify({
        "ok": True,
        "unlocked": True,
        "window_seconds": _setup_unlock_window_seconds(),
        "remaining_seconds": _setup_unlock_remaining_seconds(),
    })


@app.route("/reauth/status")
def setup_unlock_status():
    """Tell the page whether a removal will ask for the password right now."""
    if not session.get("dashboard_authenticated"):
        return jsonify({"ok": False, "error": "authentication_required"}), 401
    remaining = _setup_unlock_remaining_seconds()
    return jsonify({
        "ok": True,
        "unlocked": remaining > 0,
        "remaining_seconds": remaining,
        "window_seconds": _setup_unlock_window_seconds(),
    })


@app.route("/reauth/lock", methods=["POST"])
def lock_setup_changes():
    """Lock removals immediately so the next delete asks again."""
    if not session.get("dashboard_authenticated"):
        return jsonify({"ok": False, "error": "authentication_required"}), 401
    _clear_setup_unlock()
    if _expects_json_response():
        return jsonify({"ok": True, "unlocked": False, "remaining_seconds": 0})
    flash("Locked. The next removal will ask for the password again.", "success")
    return redirect(_local_referrer(with_reauth=False) or url_for("index"))


@app.route("/amazon/<code>")
def amazon_short_link(code: str):
    """Resolve a transparent first-party short link to a tagged Amazon.in item."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8}", code):
        abort(404)
    record = db.get_amazon_short_link(code)
    if not record:
        abort(404)

    requested_tags = request.args.getlist("tag")
    if len(requested_tags) != 1 or requested_tags[0].strip() != record["associate_tag"]:
        abort(404)

    target = str(record["target_url"])
    parsed = urlparse(target)
    target_tags = [
        value for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() == "tag" and value
    ]
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"amazon.in", "www.amazon.in"}
        or not re.fullmatch(r"/dp/[A-Za-z0-9]{10}", parsed.path, re.I)
        or target_tags != [record["associate_tag"]]
    ):
        # Only redirect to generated Amazon.in product links; never accept an
        # arbitrary destination from the short-link URL or query string.
        abort(404)
    return redirect(target, code=302)


@app.route("/m/<code>")
def meesho_hypd_short_link(code: str):
    """Resolve a branded first-party code to its stored HYPD affiliate URL."""
    if not hypd_shortlinks.is_valid_short_code(code):
        abort(404)
    record = db.get_hypd_short_link(code)
    if not record:
        abort(404)

    target = str(record["target_url"])
    if not hypd_shortlinks.is_valid_hypd_affiliate_url(target):
        # Never use this redirect as an open redirect, even if the DB is changed.
        abort(404)
    target_path = re.fullmatch(r"/(\d+)/afflink/([A-Za-z0-9_-]+)", urlparse(target).path)
    if not target_path or target_path.group(1) != str(record["store_id"]):
        abort(404)
    return redirect(target, code=302)


@app.route("/l/<code>")
def lehlah_meesho_short_link(code: str):
    """Resolve a branded code to the untouched LehLah-attributed Meesho URL."""
    if not lehlah_shortlinks.is_valid_short_code(code):
        abort(404)
    record = db.get_lehlah_short_link(code)
    if not record:
        abort(404)
    target = str(record["target_url"])
    if not lehlah_shortlinks.is_valid_lehlah_meesho_url(target):
        # Redirects stay on a validated Meesho product host/path with LehLah markers.
        abort(404)
    return redirect(target, code=302)


def _setup_readiness_snapshot() -> dict:
    """Build a local, secret-free source-to-channel launch checklist."""
    all_profiles = db.list_influencers()
    active_profiles = [profile for profile in all_profiles if profile.get("active")]
    profile_ids = {int(profile["id"]) for profile in active_profiles}
    channels = [
        channel for channel in db.list_channels()
        if int(channel.get("influencer_id", -1)) in profile_ids
    ]
    active_sources = db.list_sources(kind="production", active_only=True)
    source_specs = {
        str(source.get("spec") or "").strip().casefold()
        for source in active_sources if str(source.get("spec") or "").strip()
    }
    source_specs.update(
        str(spec).strip().casefold()
        for spec in config.SHARED_SOURCES if str(spec).strip()
    )
    ready_channels = [
        channel for channel in channels
        if str(channel.get("status", "")).strip().lower() in {"ready", "active"}
    ]
    pending_whatsapp = [
        channel for channel in channels
        if str(channel.get("platform", "")).startswith("whatsapp")
        and str(channel.get("status", "")).strip().lower() == "pending"
    ]
    telegram_channels = [
        channel for channel in ready_channels
        if str(channel.get("platform", "")).lower() == "telegram"
    ]
    whatsapp_channels = [
        channel for channel in ready_channels
        if str(channel.get("platform", "")).startswith("whatsapp")
    ]

    def enabled(value, default=True):
        if value is None:
            return default
        return str(value).strip().lower() not in {"", "0", "false", "no", "off", "disabled", "none"}

    active_amazon_profiles = [
        profile for profile in active_profiles
        if enabled(profile.get("allow_amazon"), True)
    ]
    fallback_tag = str(config.AMAZON_ASSOCIATE_TAG or "").strip().casefold()
    fallback_tag_profiles = sum(
        not str(profile.get("amazon_tag") or "").strip()
        or str(profile.get("amazon_tag") or "").strip().casefold() == fallback_tag
        for profile in active_amazon_profiles
    )
    earnkaro_enabled = any(
        enabled(profile.get("allow_earnkaro"), True)
        and not enabled(profile.get("only_amazon"), False)
        for profile in active_profiles
    )
    earnkaro_configured = bool(
        db.get_global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY
    )
    worker = db.get_worker_heartbeat()
    worker_age = max(0.0, time.time() - float(worker.get("heartbeat_at", 0))) if worker else None
    worker_freshness = max(45, int(config.DEAL_WORKER_POLL_INTERVAL) * 2)
    worker_live = bool(
        worker and worker.get("state") in {"running", "degraded"}
        and worker_age is not None and worker_age <= worker_freshness
    )
    try:
        session_file_exists = telegram_ops._session_base_path().is_file()
    except (OSError, ValueError):
        session_file_exists = False
    telegram_configured = bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH and session_file_exists)
    production = config.HUB_ENV == "production"
    wa_url_host = (urlparse(config.WA_HUB_URL).hostname or "").lower()
    wa_loopback = wa_url_host in {"127.0.0.1", "::1", "localhost"}
    wa_hub_configured = (
        len(config.WA_HUB_TOKEN) >= 32 and wa_loopback
        if production else bool(wa_loopback)
    )

    production_security_ok = (
        config.HUB_ENV == "production"
        and _dashboard_security_ready()
        and config.DASHBOARD_COOKIE_SECURE
    )
    items = [
        {
            "key": "dashboard",
            "title": "Private dashboard access",
            "status": "ready" if production_security_ok else "warning",
            "detail": (
                "Production password/session security is configured. Keep this behind Tailscale Serve with device approval and do not expose Flask ports publicly."
                if production_security_ok else
                "This process is not verified as a private production deployment. Use a strong password, stable secret, Secure cookies, loopback binding, and Tailscale device controls before real use."
            ),
            "action": "Review health",
            "href": url_for("vm"),
        },
        {
            "key": "telegram",
            "title": "Telegram account",
            "status": "warning" if telegram_configured else "blocked",
            "detail": (
                "API configuration and a session file are present. Run the live check to verify authorization."
                if telegram_configured else
                "Set TELEGRAM_API_HASH and provision/authorize the configured Telethon session before source reads or Telegram posts."
            ),
            "action": "Check account",
            "href": "#live-checks",
        },
        {
            "key": "sources",
            "title": "Joined deal sources",
            "status": "warning" if source_specs else "blocked",
            "detail": (
                f"{len(source_specs)} active production selector(s). The worker reads only dialogs already joined to the Telegram account; it never joins invite links."
                if source_specs else
                "Add source selectors, then join those channels from the authorized Telegram account. Adding a link here does not join it."
            ),
            "action": "Manage sources",
            "href": "#source-pool",
        },
        {
            "key": "worker",
            "title": "24/7 deal worker",
            "status": "ready" if worker_live and worker.get("state") == "running" else ("warning" if worker_live else "blocked"),
            "detail": (
                "Worker heartbeat is fresh; a successful live source scan is still required to verify ingestion."
                if worker_live and worker.get("state") == "running" else
                "Worker is alive but reporting a poll error. Check worker logs and the source/live-check results."
                if worker_live else
                "No fresh worker heartbeat. Install/start the deal-worker service; a running dashboard alone does not ingest deals."
            ),
            "action": "Open health",
            "href": url_for("vm"),
        },
        {
            "key": "destinations",
            "title": "Output channels",
            "status": "warning" if ready_channels else ("warning" if pending_whatsapp else "blocked"),
            "detail": (
                f"{len(telegram_channels)} configured-ready Telegram and {len(whatsapp_channels)} ready WhatsApp destination(s); {len(pending_whatsapp)} WhatsApp destination(s) still need a successful visible test post. Telegram destinations need their own explicit test post before treating delivery as verified."
                if ready_channels or pending_whatsapp else
                "Add output destinations. WhatsApp destinations stay Pending until an explicit test post succeeds."
            ),
            "action": "Add a creator/channel",
            "href": "#quick-add-form",
        },
        {
            "key": "routing",
            "title": "Affiliate routing",
            "status": "warning" if (fallback_tag_profiles or (earnkaro_enabled and not earnkaro_configured)) else "ready",
            "detail": (
                f"{fallback_tag_profiles} active Amazon-enabled profile(s) use the configured fallback tag; confirm each creator's own tag. "
                if fallback_tag_profiles else ""
            ) + (
                "EarnKaro is enabled but its API credential is missing; eligible non-Amazon/non-Meesho links will not be converted yet."
                if earnkaro_enabled and not earnkaro_configured else
                "Amazon uses each saved creator tag; EarnKaro is for supported non-Amazon/non-Meesho merchants; HYPD is reserved for Meesho."
            ),
            "action": "Review routes",
            "href": "#quick-add-form",
        },
        {
            "key": "whatsapp-hub",
            "title": "WhatsApp hub",
            "status": "warning" if wa_hub_configured else "blocked",
            "detail": (
                "Loopback-only URL and production token length are configured. Live reachability is checked below."
                if production and wa_hub_configured else
                "The hub URL is loopback-only. Live reachability is checked below; production additionally requires a private token of at least 32 characters."
                if wa_hub_configured else
                "Keep WA_HUB_URL loopback-only; production also requires a private WA_HUB_TOKEN of at least 32 characters."
            ),
            "action": "Check hub",
            "href": "#live-checks",
        },
    ]
    return {
        "items": items,
        "blocked_count": sum(item["status"] == "blocked" for item in items),
        "warning_count": sum(item["status"] == "warning" for item in items),
    }


@app.route("/")
def index():
    """Read-only operational overview; all setup actions live under /setup."""
    influencers = db.list_influencers()
    stats = db.post_stats()
    vm = db.latest_vm()
    insights = db.get_live_deal_insights()
    sources = db.list_sources(active_only=False)
    active_sources = sum(1 for source in sources if source.get("active"))
    return render_template(
        "index.html",
        creator_count=len(influencers),
        stats=stats,
        vm=vm,
        insights=insights,
        money=_money_summary(),
        source_count=len(sources),
        active_source_count=active_sources,
    )


@app.route("/setup")
def setup():
    """Dedicated protected setup center for profiles, routes, and operations."""
    q = request.args.get("q", "").strip()
    influencers = db.search_influencers(q) if q else db.list_influencers()
    settings = db.get_all_global_settings()
    sources = db.list_sources(active_only=False)
    current_ek_key = settings.get("earnkaro_api_key") or config.EARNKARO_API_KEY or ""
    current_ek_pubid = settings.get("earnkaro_publisher_id") or _effective_earnkaro_publisher_id()
    current_hypd_store = settings.get("hypd_store_id") or _effective_hypd_store_id()
    return render_template(
        "setup.html",
        influencers=influencers,
        settings=settings,
        sources=sources,
        readiness=_setup_readiness_snapshot(),
        search_query=q,
        current_ek_key=current_ek_key,
        current_ek_pubid=current_ek_pubid,
        current_hypd_store=current_hypd_store,
        default_amazon_tag=config.AMAZON_ASSOCIATE_TAG,
        sources_added=request.args.get("sources_added", type=int),
        source_saved=request.args.get("source_saved", type=int),
        source_updated=request.args.get("source_updated", type=int),
        source_deleted=request.args.get("source_deleted", type=int),
        source_error=request.args.get("source_error", ""),
        imported=request.args.get("imported", type=int),
        import_error=request.args.get("import_error", ""),
    )


# ---------- Money Radar: where commission leaks out of posted deals ----------
# Each switch is a global setting; the pipeline reads it on every render.
MONEY_SWITCHES: dict[str, tuple[str, str]] = {
    "meesho_earnkaro_fallback": (
        "Meesho → EarnKaro fallback",
        "HYPD cannot turn a raw meesho.com product URL into an affiliate link, "
        "so that deal currently posts for free. With this on, those links go to "
        "EarnKaro, which runs a Meesho programme, instead of leaking.",
    ),
    "only_earning_deals": (
        "Only post deals that earn",
        "Hold back a deal when none of its links would carry our attribution. "
        "Fewer posts, but no post goes out that pays nothing.",
    ),
}


def _money_switch_is_on(key: str, default: bool) -> bool:
    value = db.get_global_setting(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _money_summary() -> dict | None:
    """Cheap 7-day attribution summary for the dashboard card."""
    from influencer_hub import money_radar

    try:
        return money_radar.report(days=7, limit=200)["totals"]
    except Exception:  # pragma: no cover - the dashboard must still load
        import logging
        logging.getLogger(__name__).exception("money summary failed")
        return None


@app.route("/money")
def money_dashboard():
    """Report how much of what we posted actually carried our attribution."""
    from influencer_hub import money_radar

    try:
        days = int(request.args.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))

    defaults = {
        "meesho_earnkaro_fallback": bool(config.MEESHO_EARNKARO_FALLBACK),
        "only_earning_deals": bool(config.ONLY_EARNING_DEALS),
    }
    switches = [
        {
            "key": key,
            "label": label,
            "detail": detail,
            "on": _money_switch_is_on(key, defaults[key]),
            "default_on": defaults[key],
        }
        for key, (label, detail) in MONEY_SWITCHES.items()
    ]

    try:
        data = money_radar.report(days=days)
    except Exception:  # pragma: no cover - the page must still open
        import logging
        logging.getLogger(__name__).exception("money radar report failed")
        data = {
            "days": days,
            "totals": {"posts": 0, "links": 0, "earning": 0, "leak": 0,
                       "monetisable": 0, "clean_posts": 0,
                       "zero_commission_posts": 0, "coverage_pct": 100},
            "by_reason": [],
            "creators": [],
            "suggestions": [],
        }

    return render_template(
        "money.html",
        report=data,
        days=days,
        day_options=(1, 7, 30, 90),
        switches=switches,
        switched=request.args.get("switched", "").strip(),
    )


@app.route("/money/switch", methods=["POST"])
def toggle_money_switch():
    """Flip one Money Radar switch (password-confirmed, like every setup change)."""
    key = (request.form.get("setting") or "").strip()
    if key not in MONEY_SWITCHES:
        flash("That money switch does not exist.", "warning")
        return redirect(url_for("money_dashboard"))

    wanted = str(request.form.get("value", "")).strip().lower() in {"1", "on", "true", "yes"}
    db.set_global_setting(key, "on" if wanted else "off")
    label = MONEY_SWITCHES[key][0]
    flash(f"{label} turned {'ON' if wanted else 'OFF'}.", "success")
    return redirect(url_for("money_dashboard", switched=key))


@app.route("/api/setup/live-checks", methods=["POST"])
def setup_live_checks():
    """Run bounded, read-only Telegram/source and WhatsApp-hub checks.

    These checks never join sources, read Telegram message history, send a post,
    or activate a destination. Actual channel permissions still require the
    operator's explicit per-channel test-post action.
    """
    checks = {}
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        checks["telegram"] = {
            "state": "not_configured",
            "message": "Set TELEGRAM_API_ID and TELEGRAM_API_HASH before testing Telegram.",
        }
        checks["sources"] = {
            "state": "not_checked",
            "message": "Source matching was not checked because Telegram credentials are incomplete.",
        }
    else:
        try:
            source_report = _run(asyncio.wait_for(
                puller.inspect_source_selection(), timeout=30
            ))
            if not source_report.get("authorized"):
                checks["telegram"] = {
                    "state": "not_authorized",
                    "message": "Telegram connected, but the configured session is not authorized. Sign in with the account that has joined your sources.",
                }
                checks["sources"] = {
                    "state": "not_checked",
                    "message": "Authorize the Telegram session, then run this check again.",
                }
            else:
                checks["telegram"] = {
                    "state": "connected",
                    "message": "Telegram account authorization succeeded. No message was sent.",
                }
                source_state = (
                    "fallback" if source_report.get("selected_sources")
                    and source_report.get("selection_mode") == "joined_dialog_fallback" else
                    "matched" if source_report.get("selected_sources") else
                    "no_matches" if source_report.get("configured_sources") else
                    "no_sources"
                )
                details = (
                    f"{source_report.get('selected_sources', 0)} joined dialog(s) selected from "
                    f"{source_report.get('configured_sources', 0)} production selector(s); "
                    f"{source_report.get('joined_group_channels', 0)} joined group/channel dialog(s) were visible."
                )
                if source_report.get("unresolved_private_invites"):
                    details += (
                        " Some private invite selectors have no display name; the worker's safe fallback uses already-joined dialogs only."
                    )
                details += " No invite was checked or joined, and no history was read."
                checks["sources"] = {"state": source_state, "message": details}
        except asyncio.TimeoutError:
            checks["telegram"] = {
                "state": "timeout",
                "message": "Telegram check timed out. Check the VM network and Telegram session, then retry.",
            }
            checks["sources"] = {
                "state": "not_checked",
                "message": "Source matching did not finish before the safe timeout.",
            }
        except Exception as exc:
            error_code = type(exc).__name__
            checks["telegram"] = {
                "state": "error",
                "message": f"Telegram check failed ({error_code}). Review API credentials, session authorization, and worker logs.",
            }
            checks["sources"] = {
                "state": "not_checked",
                "message": "Fix the Telegram connection first, then retry source matching.",
            }

    try:
        hub = _run(asyncio.wait_for(whatsapp_client.health_snapshot(), timeout=8))
        checks["whatsapp_hub"] = {
            "state": "reachable",
            "message": (
                f"Hub reachable: {hub['connected_count']} connected, "
                f"{hub['qr_pending_count']} waiting for QR, "
                f"{hub['offline_count']} offline/error, "
                f"{hub['session_count']} session(s) known. No message was sent."
            ),
        }
    except asyncio.TimeoutError:
        checks["whatsapp_hub"] = {
            "state": "timeout",
            "message": "WhatsApp hub check timed out. Confirm the private hub service is running on loopback.",
        }
    except Exception as exc:
        checks["whatsapp_hub"] = {
            "state": "offline",
            "message": f"WhatsApp hub is unreachable or rejected authentication ({type(exc).__name__}). Check WA_HUB_URL, token, and service logs.",
        }
    return jsonify({"ok": True, "checks": checks})


@app.route("/sources/seed-defaults", methods=["POST"])
def seed_default_sources():
    """Register the suggested source catalog only after an explicit operator action."""
    existing_specs = {source["spec"] for source in db.list_sources(active_only=False)}
    added = 0
    for name, spec in DEFAULT_SOURCE_CATALOG:
        if spec not in existing_specs:
            db.add_source(name, spec, kind="production")
            existing_specs.add(spec)
            added += 1
    return redirect(url_for("setup", sources_added=added))


@app.route("/sources/add", methods=["POST"])
def add_deal_source():
    name = request.form.get("name", "").strip()
    spec = request.form.get("spec", "").strip()
    kind = request.form.get("kind", "production").strip().lower()
    from_vault = request.form.get("from_vault") == "1"
    from_setup = request.form.get("from_setup") == "1"
    if kind not in {"production", "dummy"}:
        kind = "production"
    if spec and len(spec) <= 512:
        _, _, is_private_invite = puller._source_parts(spec)
        if is_private_invite and not name:
            return redirect(url_for("setup", source_error="private_name_required"))
        if not name:
            name = spec.split("/")[-1].replace("+", "").replace("@", "")
        db.add_source(name[:120], spec, kind=kind)
    if from_setup:
        return redirect(url_for("setup", source_saved=1))
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/sources/<int:source_id>/delete", methods=["POST"])
def delete_deal_source(source_id):
    from_vault = request.form.get("from_vault") == "1"
    from_setup = request.form.get("from_setup") == "1"
    db.delete_source(source_id)
    if from_setup:
        return redirect(url_for("setup", source_deleted=1))
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/sources/<int:source_id>/toggle", methods=["POST"])
def toggle_deal_source(source_id):
    from_vault = request.form.get("from_vault") == "1"
    from_setup = request.form.get("from_setup") == "1"
    db.toggle_source(source_id)
    if from_setup:
        return redirect(url_for("setup", source_updated=1))
    if from_vault:
        return redirect(url_for("secret_vault_tab"))
    return redirect(url_for("index"))


@app.route("/global-settings/update", methods=["POST"])
def update_global_settings():
    nav_btn_en = "1" if request.form.get("nav_button_enabled") in ("1", "on", "true") else "0"
    nav_btn_text = request.form.get("nav_button_text", "Join Shpsy Loots ❤️").strip()
    nav_btn_url = request.form.get("nav_button_url", "").strip()

    db.set_global_setting("nav_button_enabled", nav_btn_en)
    db.set_global_setting("nav_button_text", nav_btn_text)
    db.set_global_setting("nav_button_url", nav_btn_url)

    return redirect(url_for("setup"))


@app.route("/admin/affiliate-vault/update", methods=["POST"])
def update_affiliate_vault():
    pwd = request.form.get("admin_password", "").strip()

    if not _password_matches(pwd, config.ADMIN_DELETE_PASSWORD):
        return redirect(url_for("secret_vault_tab", vault_err="invalid_password"))

    session["vault_unlocked"] = True
    ek_key = request.form.get("earnkaro_api_key", "").strip()
    ek_pubid = request.form.get("earnkaro_publisher_id", "").strip()
    hypd_store = request.form.get("hypd_store_id", "").strip()

    if ek_key:
        db.set_global_setting("earnkaro_api_key", ek_key)
    if ek_pubid:
        db.set_global_setting("earnkaro_publisher_id", ek_pubid)
    if hypd_store:
        db.set_global_setting("hypd_store_id", hypd_store)

    return redirect(url_for("secret_vault_tab", vault_success="1"))

@app.route("/admin/secret-vault", methods=["GET", "POST"])
def secret_vault_tab():
    """Separate password-protected tab for Central Vault & Deal Sources."""
    auth_err = False
    vault_success = request.args.get("vault_success") == "1"
    vault_err = request.args.get("vault_err")
    unlocked = session.get("vault_unlocked", False)

    if request.method == "POST":
        pwd = request.form.get("admin_password", "").strip()
        if _password_matches(pwd, config.ADMIN_DELETE_PASSWORD):
            session["vault_unlocked"] = True
            unlocked = True
        else:
            auth_err = True
    current_hypd_store = _effective_hypd_store_id()
    current_ek_pubid = _effective_earnkaro_publisher_id()
    from influencer_hub.puller import PRIORITY_SOURCE_SPECS
    raw_sources = db.list_sources()
    p_map = {spec: idx for idx, spec in enumerate(PRIORITY_SOURCE_SPECS)}
    sources = sorted(raw_sources, key=lambda s: p_map.get(s.get("spec", ""), 999))

    return render_template(
        "secret_vault.html",
        unlocked=unlocked,
        auth_err=auth_err,
        vault_success=vault_success,
        vault_err=vault_err,
        current_hypd_store=current_hypd_store,
        current_ek_pubid=current_ek_pubid,
        sources=sources
    )


@app.route("/admin/secret-vault/lock", methods=["POST"])
def lock_vault():
    session.pop("vault_unlocked", None)
    return redirect(url_for("secret_vault_tab"))


@app.route("/quick-add", methods=["POST"])
def quick_add():
    """Ultra-fast 1-click addition of influencer + their ready-made channels.
    No code deploy, no server restart needed!
    """
    name = request.form.get("name", "").strip()
    tag = request.form.get("tag", "").strip()
    phone = request.form.get("phone_number", "").strip()
    insta = request.form.get("insta_id", "").strip()
    handle = request.form.get("handle", "").strip()
    price_filt = request.form.get("price_filter", "all").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories_list = request.form.getlist("categories")
    categories = ",".join(c.strip() for c in categories_list if c.strip())
    schedule_list = request.form.getlist("posting_schedule")
    schedule = ",".join(s.strip() for s in schedule_list if s.strip()) or request.form.get("custom_schedule", "").strip()
    only_amazon = bool(_form_flag("only_amazon", default=False))
    allow_amazon = bool(_form_flag("allow_amazon", default=False))
    allow_earnkaro = bool(_form_flag("allow_earnkaro", default=False))
    allow_hypd = bool(_form_flag("allow_hypd", default=False))
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )
    approval_tg = request.form.get("approval_tg", "").strip()
    broadcast_tg = request.form.get("broadcast_tg", "").strip()
    whatsapp_id = request.form.get("whatsapp_id", "").strip()
    strip_amz_all = request.form.get("strip_amazon_broadcast") == "1"

    # Custom button fields on quick add
    btn_enabled = request.form.get("custom_button_enabled") == "1"
    btn_text = request.form.get("custom_button_text", "Join Shpsy Loots ❤️").strip()
    btn_url = request.form.get("custom_button_url", "").strip()

    # Fallback to defaults so form never fails silently if minor details missed
    if not name:
        name = "Influencer-" + phone[-4:] if phone else "New Partner"
    if not tag:
        tag = config.AMAZON_ASSOCIATE_TAG

    iid = db.add_influencer(name, tag, handle=handle, insta_id=insta,
                            phone_number=phone, price_filter=price_filt,
                            allowed_sources=allowed_src, bitly_api_key=bitly_key,
                            categories=categories, posting_schedule=schedule,
                            only_amazon=only_amazon, allow_amazon=allow_amazon,
                            allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                            hypd_store_id=hypd_store_id)

    if btn_enabled or btn_url:
        db.update_influencer(iid, custom_button_enabled=btn_enabled,
                             custom_button_text=btn_text, custom_button_url=btn_url)

    # 1. Approval Channel
    if approval_tg:
        ident = clean_identifier(approval_tg)
        db.add_channel(iid, "telegram", ident, role="approval", status="ready",
                       allowed_sources=allowed_src, categories=categories, posting_schedule=schedule,
                       only_amazon=True, allow_amazon=True, allow_earnkaro=False,
                       allow_hypd=False, hypd_store_id=hypd_store_id)

    # 2. Broadcast Channel
    if broadcast_tg:
        ident = clean_identifier(broadcast_tg)
        db.add_channel(iid, "telegram", ident, role="broadcast", status="ready",
                       strip_amazon=strip_amz_all,
                       price_filter=price_filt if price_filt != "all" else "",
                       allowed_sources=allowed_src, bitly_api_key=bitly_key,
                       categories=categories, posting_schedule=schedule,
                       only_amazon=only_amazon, allow_amazon=allow_amazon,
                       allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                       hypd_store_id=hypd_store_id)

    # 3. WhatsApp destinations are type-checked and staged Pending. Invite links
    # need the creator's paired account to resolve; direct groups/channels JIDs
    # can be saved now, but none activate before an explicit successful test.
    if whatsapp_id:
        destination, wa_error = _resolve_whatsapp_destination(iid, whatsapp_id)
        if destination:
            _save_whatsapp_destination(
                iid,
                destination,
                role="whatsapp",
                allowed_sources=allowed_src,
                wa_session_key=WA_SESSION_KEY(iid),
                channel_settings={
                    "strip_amazon": strip_amz_all,
                    "price_filter": price_filt if price_filt != "all" else "",
                    "bitly_api_key": bitly_key,
                    "categories": categories,
                    "posting_schedule": schedule,
                    "only_amazon": only_amazon,
                    "allow_amazon": allow_amazon,
                    "allow_earnkaro": allow_earnkaro,
                    "allow_hypd": allow_hypd,
                    "hypd_store_id": hypd_store_id,
                },
            )
        else:
            return redirect(url_for(
                "influencer_detail", inf_id=iid,
                wa_link_status="failed", wa_link_error=wa_error,
            ))

    return redirect(url_for("influencer_detail", inf_id=iid))


@app.route("/influencer/<int:inf_id>/update-profile", methods=["POST"])
def update_profile(inf_id):
    existing_inf = db.get_influencer(inf_id)
    previous_hypd_store = (
        str(existing_inf.get("hypd_store_id") or "").strip() if existing_inf else ""
    )
    name = request.form.get("name", "").strip()
    tag = request.form.get("amazon_tag", "").strip()
    phone = request.form.get("phone_number", "").strip()
    handle = request.form.get("handle", "").strip()
    insta = request.form.get("insta_id", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories_list = request.form.getlist("categories")
    categories = ",".join(c.strip() for c in categories_list if c.strip()) if categories_list else request.form.get("categories", "").strip()
    schedule_list = request.form.getlist("posting_schedule")
    schedule = ",".join(s.strip() for s in schedule_list if s.strip()) or request.form.get("custom_schedule", "").strip()
    only_amazon = _form_flag("only_amazon", default=None)
    allow_amazon = _form_flag("allow_amazon", default=None)
    allow_earnkaro = _form_flag("allow_earnkaro", default=None)
    allow_hypd = _form_flag("allow_hypd", default=None)
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )
    notes = request.form.get("notes", "").strip()
    active_str = request.form.get("active")
    active = (active_str == "1") if active_str is not None else None

    # Custom navigation button fields
    btn_enabled = request.form.get("custom_button_enabled") == "1"
    btn_text = request.form.get("custom_button_text", "").strip()
    btn_url = request.form.get("custom_button_url", "").strip()

    # Deal quality floor: an empty selection inherits the global setting.
    min_deal_tier = request.form.get("min_deal_tier")
    min_deal_tier = min_deal_tier.strip() if min_deal_tier is not None else None

    db.update_influencer(
        inf_id,
        name=name if name else None,
        amazon_tag=tag if tag else None,
        phone_number=phone if phone else None,
        handle=handle if handle else None,
        insta_id=insta if insta else None,
        price_filter=price_filt if price_filt else None,
        allowed_sources=allowed_src if allowed_src is not None else None,
        bitly_api_key=bitly_key if bitly_key is not None else None,
        categories=categories if categories is not None else None,
        posting_schedule=schedule if schedule is not None else None,
        only_amazon=only_amazon,
        allow_amazon=allow_amazon,
        allow_earnkaro=allow_earnkaro,
        allow_hypd=allow_hypd,
        hypd_store_id=hypd_store_id,
        custom_button_enabled=btn_enabled,
        custom_button_text=btn_text,
        custom_button_url=btn_url,
        min_deal_tier=min_deal_tier,
        notes=notes if notes else None,
        active=active,
    )
    if existing_inf and hypd_store_id != previous_hypd_store:
        db.update_inherited_channel_hypd_store_ids(
            inf_id, previous_hypd_store, hypd_store_id
        )
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/channel/<int:channel_id>/update", methods=["POST"])
def update_channel_route(channel_id):
    current_channel = next(
        (channel for channel in db.list_channels()
         if int(channel.get("id", -1)) == int(channel_id)),
        None,
    )
    if not current_channel:
        return redirect(url_for("index"))
    owner_id = int(current_channel["influencer_id"])
    provided_inf_id = request.form.get("inf_id", "").strip()
    if provided_inf_id and provided_inf_id.isdigit() and int(provided_inf_id) != owner_id:
        return redirect(url_for("influencer_detail", inf_id=owner_id))

    ident = request.form.get("identifier", "").strip()
    role = request.form.get("role", "").strip()
    status = request.form.get("status", "").strip()
    override_tag = request.form.get("amazon_override_tag", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    wa_key = request.form.get("wa_session_key", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories = request.form.get("categories", "").strip()
    schedule = request.form.get("posting_schedule", "").strip()
    only_amz = _form_flag("only_amazon", default=None)
    strip_amz = _form_flag("strip_amazon", default=None)
    allow_amz = _form_flag("allow_amazon", default=None)
    allow_ek = _form_flag("allow_earnkaro", default=None)
    allow_hypd = _form_flag("allow_hypd", default=None)
    hypd_store_id = request.form.get("hypd_store_id", "").strip()

    # Custom button fields per channel
    btn_en = _form_flag("custom_button_enabled", default=None)
    btn_text = request.form.get("custom_button_text")
    btn_url = request.form.get("custom_button_url")
    invite_link = None
    needs_test = False

    # Deal quality floor for this channel; empty inherits the creator's value.
    channel_min_tier = request.form.get("min_deal_tier")
    channel_min_tier = channel_min_tier.strip() if channel_min_tier is not None else None

    if current_channel.get("platform") in {"whatsapp_group", "whatsapp_channel"}:
        if ident:
            destination, error = _resolve_whatsapp_destination(owner_id, ident)
            if not destination or destination["platform"] != current_channel["platform"]:
                error = error or "The supplied destination does not match this WhatsApp channel type."
                return redirect(url_for(
                    "influencer_detail", inf_id=owner_id,
                    wa_link_status="failed", wa_link_error=error,
                ))
            ident = destination["identifier"]
            invite_link = destination.get("invite_link") or None
            if ident != current_channel.get("identifier"):
                status = "pending"
                needs_test = True
        if current_channel.get("status") == "pending" and status in {"ready", "active"}:
            status = None
            needs_test = True
        wa_key = wa_key or current_channel.get("wa_session_key") or WA_SESSION_KEY(owner_id)
    elif ident:
        ident = clean_identifier(ident)

    # A pending "undo removal" is stale once this channel is edited by hand.
    session.pop(UNDO_CHANNEL_KEY, None)
    db.update_channel_details(
        channel_id,
        identifier=ident if ident else None,
        role=role if role else None,
        invite_link=invite_link,
        status=status if status else None,
        amazon_override_tag=override_tag,
        strip_amazon=strip_amz,
        price_filter=price_filt,
        allowed_sources=allowed_src,
        wa_session_key=wa_key,
        bitly_api_key=bitly_key,
        categories=categories,
        posting_schedule=schedule,
        only_amazon=only_amz,
        allow_amazon=allow_amz,
        allow_earnkaro=allow_ek,
        allow_hypd=allow_hypd,
        hypd_store_id=hypd_store_id if hypd_store_id else None,
        custom_button_enabled=btn_en,
        custom_button_text=btn_text,
        custom_button_url=btn_url,
        min_deal_tier=channel_min_tier,
    )
    if needs_test:
        return redirect(url_for(
            "influencer_detail", inf_id=owner_id, wa_link_status="pending"
        ))
    return redirect(url_for("influencer_detail", inf_id=owner_id))


# --------------------------------------------------------------------------
# Easy setup: the three network switches, in plain language
# --------------------------------------------------------------------------
# "Amazon ante only Amazon, EarnKaro on cheste verevi, HYPD on cheste Meesho".
ROUTING_SWITCHES = (
    {
        "name": "allow_amazon",
        "emoji": "📦",
        "label": "Amazon",
        "meaning": "Amazon links get this creator's Associates tag. Nothing else touches them.",
    },
    {
        "name": "allow_earnkaro",
        "emoji": "💰",
        "label": "EarnKaro",
        "meaning": "Flipkart, Shopsy, Myntra, Ajio, Nykaa, Croma, TataCliq… become EarnKaro links.",
    },
    {
        "name": "allow_hypd",
        "emoji": "🛍️",
        "label": "HYPD (Meesho)",
        "meaning": "Meesho deals: HYPD affiliate links are retagged to this creator's store.",
    },
)
ROUTING_SAMPLES = (
    ("Amazon deal", "https://www.amazon.in/dp/B0D9P2M1PB?th=1", "amazon"),
    ("Flipkart / Shopsy deal", "https://www.flipkart.com/sample-deal/p/itmEXAMPLE", "merchant"),
    ("Myntra / Ajio deal", "https://www.myntra.com/sample-deal/1234567", "merchant"),
    ("Meesho deal", "https://www.meesho.com/sample-deal/p/xyz123", "meesho"),
    ("HYPD affiliate link", "https://hypd.store/93944/afflink/SAMPLETOKEN", "hypd"),
    ("Plain info link", "https://example.com/deal-news", "other"),
)


def _earnkaro_ready() -> bool:
    return bool(db.get_global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY)


def _routing_preview(
    *,
    amazon_tag: str,
    allow_amazon: bool,
    allow_earnkaro: bool,
    allow_hypd: bool,
    only_amazon: bool = False,
    hypd_store_id: str = "",
    channel_role: str = "broadcast",
) -> dict:
    """Explain, in one table, exactly what happens to each kind of link.

    Pure and offline: it mirrors the pipeline's routing rules so the operator
    can see the outcome before a single deal is posted.
    """
    strict = bool(only_amazon) or channel_role == "approval"
    amazon_on = True if strict else bool(allow_amazon)
    ek_on = False if strict else bool(allow_earnkaro)
    hypd_on = False if strict else bool(allow_hypd)
    tag = str(amazon_tag or "").strip() or config.AMAZON_ASSOCIATE_TAG
    store = str(hypd_store_id or "").strip() or _effective_hypd_store_id()
    ek_ready = _earnkaro_ready()
    try:
        from influencer_hub import accounts

        model = accounts.model_rows(
            amazon_tag=tag, hypd_store=store,
            earnkaro_pubid=_effective_earnkaro_publisher_id(),
        )
    except Exception:  # pragma: no cover - defensive
        model = []

    rows: list[dict] = []
    for label, sample, kind in ROUTING_SAMPLES:
        row = {"label": label, "sample": sample, "kind": kind, "result": "", "state": "ok", "note": ""}
        if kind == "amazon":
            if amazon_on:
                row["result"] = link_router.apply_amazon_tag(sample, tag)
                row["note"] = (
                    f"Posted with this creator's OWN tag ({tag}) — Amazon commission is theirs. "
                    "Never shortened away from Amazon."
                )
            else:
                row["state"] = "off"
                row["note"] = "Amazon is OFF — this link is removed from the post."
        elif kind == "merchant":
            if not ek_on:
                row["state"] = "off"
                row["note"] = "EarnKaro is OFF — non-Amazon merchant links are removed from the post."
            elif ek_ready:
                row["result"] = sample
                row["note"] = (
                    "Converted at send time on OUR EarnKaro account — this link earns for us."
                )
            else:
                row["state"] = "warn"
                row["result"] = sample
                row["note"] = (
                    "EarnKaro is ON but its API key is missing — the original link is kept. "
                    "Add the key in Vault & Sources to start earning."
                )
        elif kind == "meesho":
            if hypd_on:
                row["state"] = "warn"
                row["result"] = sample
                row["note"] = (
                    "HYPD owns Meesho. HYPD affiliate links (hypd.store/…/afflink/…) are retagged "
                    f"to store {store}; a raw Meesho link cannot be converted yet, so it is posted as-is."
                )
            else:
                row["state"] = "off"
                row["note"] = "HYPD (Meesho) is OFF — Meesho links are removed from the post."
        elif kind == "hypd":
            if hypd_on:
                row["result"] = link_router.convert_hypd_store_link(sample, store)
                row["note"] = f"Retagged to OUR HYPD store {store} (our account, not the creator's)."
            else:
                row["state"] = "off"
                row["note"] = "HYPD (Meesho) is OFF — this link is removed from the post."
        else:
            row["result"] = sample
            row["note"] = "Informational links are never changed or dropped."
        rows.append(row)

    summary = " · ".join(
        part for part in (
            f"Amazon → creator's tag {tag}" if amazon_on else "Amazon off",
            "Other merchants → our EarnKaro" if ek_on else "Other merchants off",
            f"Meesho → our HYPD store {store}" if hypd_on else "Meesho off",
        )
    )
    return {
        "rows": rows,
        "model": model,
        "summary": summary,
        "strict": strict,
        "amazon_on": amazon_on,
        "earnkaro_on": ek_on,
        "hypd_on": hypd_on,
        "earnkaro_ready": ek_ready,
        "amazon_tag": tag,
        "hypd_store_id": store,
    }


CHANNEL_PLATFORMS = frozenset({"telegram", "whatsapp_group", "whatsapp_channel"})
CHANNEL_ROLES = frozenset({"approval", "broadcast", "whatsapp"})
PRICE_FILTER_CHOICES = frozenset({"", "all", "under_99", "under_199", "under_499", "under_999"})
_TELEGRAM_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{4,32}$")
_TELEGRAM_NUMERIC_RE = re.compile(r"^-?\d{5,20}$")
_TELEGRAM_INVITE_RE = re.compile(r"^https://t\.me/(\+|joinchat/)[A-Za-z0-9_-]+$", re.I)


def _clean_price_filter(raw: str) -> str:
    """Only accept the budget filters the pipeline actually understands."""
    value = str(raw or "").strip().lower()
    return value if value in PRICE_FILTER_CHOICES else ""


def _valid_telegram_identifier(value: str) -> tuple[bool, str]:
    """Reject typos that would silently create an undeliverable channel."""
    candidate = str(value or "").strip()
    if (
        _TELEGRAM_USERNAME_RE.fullmatch(candidate)
        or _TELEGRAM_NUMERIC_RE.fullmatch(candidate)
        or _TELEGRAM_INVITE_RE.fullmatch(candidate)
    ):
        return True, ""
    return False, (
        "Enter a valid Telegram destination: a public @username (4-32 letters, "
        "numbers or underscores), a numeric channel id, or a private t.me/+… invite link."
    )


def _channel_feedback(inf_id: int, category: str, message: str):
    """Flash the result of a channel action and return to the creator's page."""
    flash(message, category)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/add-manual-channel", methods=["POST"])
def add_manual_channel(inf_id):
    influencer = db.get_influencer(inf_id)
    if not influencer:
        flash("That creator no longer exists.", "warning")
        return redirect(url_for("index"))

    platform = request.form.get("platform", "telegram").strip().lower()
    raw_ident = request.form.get("identifier", "").strip()
    role = (request.form.get("role", "broadcast").strip().lower() or "broadcast")
    if role not in CHANNEL_ROLES:
        role = "broadcast"
    invite = request.form.get("invite", "").strip()
    override_tag = request.form.get("amazon_override_tag", "").strip()
    price_filt = _clean_price_filter(request.form.get("price_filter", ""))
    allowed_src = request.form.get("allowed_sources", "").strip()[:400]
    wa_key = request.form.get("wa_session_key", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories = request.form.get("categories", "").strip()
    schedule = request.form.get("posting_schedule", "").strip()

    if platform not in CHANNEL_PLATFORMS:
        return _channel_feedback(
            inf_id, "error",
            "Unsupported destination type. Choose Telegram, WhatsApp Group, or WhatsApp Channel.",
        )
    if not raw_ident:
        return _channel_feedback(
            inf_id, "error",
            "Enter the channel username, invite link, or WhatsApp JID before connecting.",
        )
    if len(raw_ident) > 256:
        return _channel_feedback(
            inf_id, "error", "That destination link is too long. Check the value and try again.",
        )
    only_amz = bool(_form_flag("only_amazon", default=False))
    strip_amz = bool(_form_flag("strip_amazon", default=False))
    allow_amz = bool(_form_flag("allow_amazon", default=False))
    allow_ek = bool(_form_flag("allow_earnkaro", default=False))
    allow_hypd = bool(_form_flag("allow_hypd", default=False))
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )

    channel_settings = {
        "amazon_override_tag": override_tag,
        "strip_amazon": strip_amz,
        "price_filter": price_filt,
        "bitly_api_key": bitly_key,
        "categories": categories,
        "posting_schedule": schedule,
        "only_amazon": only_amz,
        "allow_amazon": allow_amz,
        "allow_earnkaro": allow_ek,
        "allow_hypd": allow_hypd,
        "hypd_store_id": hypd_store_id,
    }
    if platform == "telegram":
        ident = clean_identifier(raw_ident)
        valid, error = _valid_telegram_identifier(ident)
        if not valid:
            return _channel_feedback(inf_id, "error", error)
        duplicate = next(
            (channel for channel in db.list_channels(inf_id)
             if channel.get("platform") == "telegram"
             and str(channel.get("identifier") or "").strip().lower() == ident.lower()),
            None,
        )
        if duplicate:
            db.update_channel_details(
                duplicate["id"],
                identifier=ident,
                role=role,
                allowed_sources=allowed_src or duplicate.get("allowed_sources") or "",
                wa_session_key=wa_key or duplicate.get("wa_session_key") or "",
                **channel_settings,
            )
            return _channel_feedback(
                inf_id, "warning",
                f"{ident} is already connected to {influencer['name']}. "
                "Its settings were updated instead of adding a duplicate.",
            )
        session.pop(UNDO_CHANNEL_KEY, None)
        db.add_channel(
            inf_id,
            "telegram",
            ident,
            invite_link=invite,
            status="ready",
            role=role,
            allowed_sources=allowed_src,
            wa_session_key=wa_key,
            **channel_settings,
        )
        return _channel_feedback(
            inf_id, "success",
            f"✅ {ident} connected to {influencer['name']} as a "
            f"{'approval' if role == 'approval' else 'broadcast'} channel.",
        )
    elif platform in {"whatsapp_group", "whatsapp_channel"}:
        destination, error = _resolve_whatsapp_destination(inf_id, raw_ident)
        if not destination:
            return redirect(url_for(
                "influencer_detail", inf_id=inf_id,
                wa_link_status="failed", wa_link_error=error,
            ))
        if destination["platform"] != platform:
            return redirect(url_for(
                "influencer_detail", inf_id=inf_id,
                wa_link_status="failed",
                wa_link_error="The selected WhatsApp type does not match the supplied link or JID.",
            ))
        if invite and not destination.get("invite_link"):
            destination["invite_link"] = invite
        session.pop(UNDO_CHANNEL_KEY, None)
        _save_whatsapp_destination(
            inf_id,
            destination,
            role="whatsapp",
            allowed_sources=allowed_src,
            wa_session_key=wa_key,
            channel_settings=channel_settings,
        )
        return redirect(url_for(
            "influencer_detail", inf_id=inf_id, wa_link_status="pending"
        ))
    else:
        return redirect(url_for(
            "influencer_detail", inf_id=inf_id,
            wa_link_status="failed", wa_link_error="Unsupported destination type.",
        ))

    return redirect(url_for("influencer_detail", inf_id=inf_id))


def _easy_setup_defaults() -> dict:
    return {
        "name": "",
        "amazon_tag": config.AMAZON_ASSOCIATE_TAG,
        "approval": "",
        "main": "",
        "whatsapp": "",
        "allow_amazon": True,
        "allow_earnkaro": True,
        "allow_hypd": True,
        "only_amazon": False,
        "hypd_store_id": _effective_hypd_store_id(),
    }


def _easy_setup_form_values() -> dict:
    """Read the easy-setup form, defaulting every switch sensibly."""
    values = _easy_setup_defaults()
    values.update({
        "name": request.form.get("name", "").strip(),
        "amazon_tag": request.form.get("amazon_tag", "").strip() or config.AMAZON_ASSOCIATE_TAG,
        "approval": request.form.get("approval_channel", "").strip(),
        "main": request.form.get("main_channel", "").strip(),
        "whatsapp": request.form.get("whatsapp_channel", "").strip(),
        "allow_amazon": bool(_form_flag("allow_amazon", default=False)),
        "allow_earnkaro": bool(_form_flag("allow_earnkaro", default=False)),
        "allow_hypd": bool(_form_flag("allow_hypd", default=False)),
        "only_amazon": bool(_form_flag("only_amazon", default=False)),
        "hypd_store_id": (
            request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
        ),
    })
    return values


def _routing_arguments(values: dict) -> dict:
    """Only the routing keys, so the form values can be forwarded safely."""
    return {
        "amazon_tag": values["amazon_tag"],
        "allow_amazon": values["allow_amazon"],
        "allow_earnkaro": values["allow_earnkaro"],
        "allow_hypd": values["allow_hypd"],
        "only_amazon": values["only_amazon"],
        "hypd_store_id": values["hypd_store_id"],
    }


@app.route("/easy-setup", methods=["GET", "POST"])
def easy_setup():
    """One screen: creator + approval channel + main channel + 3 switches."""
    sources = db.list_sources(active_only=False)
    active_sources = sum(1 for source in sources if source.get("active"))
    values = _easy_setup_defaults()
    result: dict | None = session.pop("_easy_setup_result", None)
    errors: list[str] = []

    if request.method == "POST":
        values = _easy_setup_form_values()
        if not values["name"]:
            errors.append("Enter the creator's name.")
        if not values["main"] and not values["approval"]:
            errors.append("Enter at least the main channel (and ideally the approval channel).")

        approval_ident, main_ident = "", ""
        if values["approval"]:
            approval_ident = clean_identifier(values["approval"])
            ok, error = _valid_telegram_identifier(approval_ident)
            if not ok:
                errors.append(f"Approval channel: {error}")
        if values["main"]:
            main_ident = clean_identifier(values["main"])
            ok, error = _valid_telegram_identifier(main_ident)
            if not ok:
                errors.append(f"Main channel: {error}")
        if not errors and values["approval"] and values["main"] and approval_ident == main_ident:
            errors.append("Approval and main channels must be two different channels.")

        if not errors:
            applied = _apply_easy_setup(values, approval_ident, main_ident)
            session["_easy_setup_result"] = applied
            flash(
                f"✅ {applied['name']} is set up and posting. "
                f"Amazon → {values['amazon_tag']}"
                + (", other merchants → EarnKaro" if values["allow_earnkaro"] else "")
                + (f", Meesho → HYPD store {values['hypd_store_id']}" if values["allow_hypd"] else ""),
                "success",
            )
            return redirect(url_for("easy_setup", done=applied["inf_id"]))

    preview = _routing_preview(**_routing_arguments(values))
    return render_template(
        "easy_setup.html",
        values=values,
        preview=preview,
        result=result,
        errors=errors,
        switches=ROUTING_SWITCHES,
        source_count=len(sources),
        active_source_count=active_sources,
    )


def _apply_easy_setup(values: dict, approval_ident: str, main_ident: str) -> dict:
    """Create or update one creator with both channels and the chosen routes."""
    name = values["name"]
    existing = next(
        (profile for profile in db.list_influencers()
         if str(profile.get("name") or "").strip().lower() == name.lower()),
        None,
    )
    settings = {
        "only_amazon": values["only_amazon"],
        "allow_amazon": values["allow_amazon"],
        "allow_earnkaro": values["allow_earnkaro"],
        "allow_hypd": values["allow_hypd"],
        "hypd_store_id": values["hypd_store_id"],
        "active": True,
    }
    if existing:
        inf_id = int(existing["id"])
        db.update_influencer(inf_id, name=name, amazon_tag=values["amazon_tag"], **settings)
        action = "updated"
    else:
        inf_id = db.add_influencer(
            name, values["amazon_tag"], allow_amazon=values["allow_amazon"],
            allow_earnkaro=values["allow_earnkaro"], allow_hypd=values["allow_hypd"],
            only_amazon=values["only_amazon"], hypd_store_id=values["hypd_store_id"],
        )
        action = "created"
    db.set_influencer_active(inf_id, True)

    channels = db.list_channels(inf_id)
    created, updated = [], []

    def _upsert(identifier: str, role: str) -> None:
        if not identifier:
            return
        match = next(
            (channel for channel in db.list_channels(inf_id)
             if channel.get("platform") == "telegram"
             and str(channel.get("identifier") or "").strip().lower() == identifier.lower()),
            None,
        )
        if match:
            db.update_channel_details(
                match["id"], identifier=identifier, role=role, status="ready",
                allow_amazon=values["allow_amazon"],
                allow_earnkaro=values["allow_earnkaro"],
                allow_hypd=values["allow_hypd"],
                hypd_store_id=values["hypd_store_id"],
            )
            updated.append(identifier)
        else:
            db.add_channel(
                inf_id, "telegram", identifier, status="ready", role=role,
                allow_amazon=values["allow_amazon"],
                allow_earnkaro=values["allow_earnkaro"],
                allow_hypd=values["allow_hypd"],
                hypd_store_id=values["hypd_store_id"],
            )
            created.append(identifier)

    _upsert(approval_ident, "approval")
    _upsert(main_ident, "broadcast")

    sources = db.list_sources(active_only=False)
    return {
        "action": action,
        "inf_id": inf_id,
        "name": name,
        "created": created,
        "updated": updated,
        "existing_channels": len(channels),
        "active_sources": sum(1 for source in sources if source.get("active")),
    }


@app.route("/api/routing-preview")
def routing_preview_api():
    """Live routing table for the current easy-setup form values."""
    values = _easy_setup_defaults()
    values.update({
        "amazon_tag": request.args.get("amazon_tag", "").strip() or config.AMAZON_ASSOCIATE_TAG,
        "allow_amazon": str(request.args.get("allow_amazon", "1")).lower() in {"1", "true", "on"},
        "allow_earnkaro": str(request.args.get("allow_earnkaro", "1")).lower() in {"1", "true", "on"},
        "allow_hypd": str(request.args.get("allow_hypd", "1")).lower() in {"1", "true", "on"},
        "only_amazon": str(request.args.get("only_amazon", "0")).lower() in {"1", "true", "on"},
        "hypd_store_id": _routing_hypd_store(request.args.get("hypd_store_id", "")),
    })
    inf_id = request.args.get("inf_id", "").strip()
    if inf_id.isdigit():
        profile = db.get_influencer(int(inf_id))
        if profile:
            values["amazon_tag"] = str(profile.get("amazon_tag") or values["amazon_tag"])
            # Our store stays ours: a creator's stored store id is not used for
            # routing while central accounts are on (the default).
            values["hypd_store_id"] = _routing_hypd_store(profile.get("hypd_store_id"))
    return jsonify({"ok": True, "preview": _routing_preview(**_routing_arguments(values))})


@app.route("/bulk-import", methods=["POST"])
def bulk_import():
    """Bulk import hundreds/thousands of influencers via CSV file upload or raw CSV text.
    Columns: name, amazon_tag, phone, insta, approval_tg, broadcast_tg, whatsapp_id, price_filter, allowed_sources
    """
    import csv
    import io

    records = []
    file = request.files.get("csv_file")
    raw_text = request.form.get("csv_text", "").strip()

    if file and file.filename:
        stream = io.StringIO(file.stream.read().decode("utf-8", errors="ignore"))
        reader = csv.DictReader(stream)
        records = list(reader)
    elif raw_text:
        stream = io.StringIO(raw_text)
        reader = csv.DictReader(stream)
        records = list(reader)

    if records:
        cleaned_records = []
        for r in records:
            name = (r.get("name") or "").strip()
            tag = (r.get("amazon_tag") or r.get("tag") or "").strip()
            if not name:
                continue
            rec = {
                "name": name,
                "amazon_tag": tag,
                "phone_number": (r.get("phone_number") or r.get("phone") or "").strip(),
                "insta_id": (r.get("insta_id") or r.get("insta") or "").strip(),
                "price_filter": (r.get("price_filter") or "all").strip(),
                "allowed_sources": (r.get("allowed_sources") or "").strip(),
                "bitly_api_key": (r.get("bitly_api_key") or r.get("bitly_key") or "").strip(),
                "categories": (r.get("categories") or "").strip(),
                "posting_schedule": (r.get("posting_schedule") or r.get("schedule") or "").strip(),
                "wa_session_key": (r.get("wa_session_key") or "").strip(),
                "approval_tg": clean_identifier(r.get("approval_tg", "")),
                "broadcast_tg": clean_identifier(r.get("broadcast_tg", "")),
                "whatsapp_id": (r.get("whatsapp_id") or "").strip(),
                "strip_amazon": r.get("strip_amazon", "0"),
                "only_amazon": r.get("only_amazon", "0"),
                "allow_amazon": r.get("allow_amazon", ""),
                "allow_earnkaro": r.get("allow_earnkaro", ""),
                "allow_hypd": r.get("allow_hypd", ""),
                "hypd_store_id": (r.get("hypd_store_id") or "").strip(),
            }
            cleaned_records.append(rec)

        added = db.add_bulk_influencers(cleaned_records)
        return redirect(url_for("setup", imported=added))

    return redirect(url_for("setup", import_error="empty_file"))


@app.route("/channel/<int:channel_id>/delete", methods=["POST"])
def delete_channel(channel_id):
    """Remove one destination, keeping a one-step undo snapshot."""
    inf_id = request.form.get("inf_id", "").strip()
    channel = next(
        (candidate for candidate in db.list_channels()
         if int(candidate.get("id", -1)) == int(channel_id)),
        None,
    )
    if not channel:
        flash("That channel was already removed.", "warning")
        return redirect(url_for("index"))

    owner_id = int(channel["influencer_id"])
    if inf_id.isdigit() and int(inf_id) != owner_id:
        flash("That channel does not belong to this creator. Nothing was removed.", "error")
        return redirect(url_for("influencer_detail", inf_id=owner_id))

    identifier = str(channel.get("identifier") or "").strip() or f"#{channel_id}"
    session[UNDO_CHANNEL_KEY] = {key: channel[key] for key in channel.keys()}
    db.delete_channel(channel_id)
    flash(
        f"Removed the {channel.get('platform')} destination {identifier}. "
        "Use Undo to put it straight back.",
        "success",
    )
    return redirect(url_for("influencer_detail", inf_id=owner_id))


@app.route("/channels/undo", methods=["POST"])
def undo_channel_delete():
    """Restore the most recently removed channel with its original settings."""
    snapshot = session.pop(UNDO_CHANNEL_KEY, None)
    if not snapshot:
        flash("There is no recent channel removal to undo.", "warning")
        return redirect(url_for("index"))

    inf_id = int(snapshot.get("influencer_id") or 0)
    if not db.get_influencer(inf_id):
        flash("The creator for that channel no longer exists, so it cannot be restored.", "error")
        return redirect(url_for("index"))

    restored_id = db.add_channel(
        inf_id,
        snapshot.get("platform") or "telegram",
        snapshot.get("identifier") or "",
        invite_link=snapshot.get("invite_link") or "",
        status=snapshot.get("status") or "pending",
        role=snapshot.get("role") or "broadcast",
        amazon_override_tag=snapshot.get("amazon_override_tag") or "",
        strip_amazon=bool(snapshot.get("strip_amazon")),
        price_filter=snapshot.get("price_filter") or "",
        allowed_sources=snapshot.get("allowed_sources") or "",
        wa_session_key=snapshot.get("wa_session_key") or "",
        bitly_api_key=snapshot.get("bitly_api_key") or "",
        categories=snapshot.get("categories") or "",
        posting_schedule=snapshot.get("posting_schedule") or "",
        only_amazon=bool(snapshot.get("only_amazon")),
        allow_amazon=snapshot.get("allow_amazon", 1),
        allow_earnkaro=snapshot.get("allow_earnkaro", 1),
        allow_hypd=snapshot.get("allow_hypd", 1),
        hypd_store_id=snapshot.get("hypd_store_id") or None,
        custom_button_enabled=bool(snapshot.get("custom_button_enabled")),
        custom_button_text=snapshot.get("custom_button_text") or "",
        custom_button_url=snapshot.get("custom_button_url") or "",
    )
    flash(
        f"Restored the {snapshot.get('platform')} destination "
        f"{snapshot.get('identifier') or restored_id}.",
        "success",
    )
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/toggle-active", methods=["POST"])
def toggle_influencer_active(inf_id):
    """Instant 1-click master switch to turn posting ON or totally OFF for this creator."""
    inf = db.get_influencer(inf_id)
    if inf:
        new_active = not bool(inf.get("active", 1))
        db.set_influencer_active(inf_id, new_active)
    ref = request.referrer or url_for("index")
    return redirect(ref)


@app.route("/channel/<int:channel_id>/toggle-status", methods=["POST"])
def toggle_channel_status(channel_id):
    """Toggle a verified destination; pending WhatsApp channels require a test first."""
    supplied_inf_id = request.form.get("inf_id", "").strip()
    ch = next(
        (channel for channel in db.list_channels()
         if int(channel.get("id", -1)) == int(channel_id)),
        None,
    )
    if not ch:
        return redirect(url_for("index"))
    owner_id = int(ch["influencer_id"])
    if supplied_inf_id.isdigit() and int(supplied_inf_id) != owner_id:
        return redirect(url_for("influencer_detail", inf_id=owner_id))
    if ch.get("platform") in {"whatsapp_group", "whatsapp_channel"} and ch.get("status") == "pending":
        return redirect(url_for(
            "influencer_detail", inf_id=owner_id, wa_link_status="pending"
        ))

    new_status = "paused" if ch.get("status") in {"ready", "active"} else "ready"
    db.update_channel_details(channel_id, status=new_status)
    return redirect(url_for("influencer_detail", inf_id=owner_id))


@app.route("/influencer/<int:inf_id>/delete", methods=["POST"])
def delete_influencer(inf_id):
    # The shared unlock window already confirmed the operator's password for
    # this removal (see REAUTH_REQUIRED_ENDPOINTS), so there is no second,
    # separate password prompt here.
    db.delete_influencer(inf_id)
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/test-post", methods=["POST"])
def test_post_demo(inf_id):
    """Instant preview demo of how deals will render for all 3 channels."""
    return redirect(url_for("influencer_detail", inf_id=inf_id, demo=1))


@app.route("/onboard", methods=["GET", "POST"])
def onboard():
    if request.method == "GET":
        return render_template(
            "onboard.html", result=None,
            default_amazon_tag=config.AMAZON_ASSOCIATE_TAG,
            default_hypd_store=_effective_hypd_store_id(),
        )
    name = request.form.get("name", "").strip()
    tag = request.form.get("tag", "").strip()
    phone = request.form.get("whatsapp", "").strip()
    handle = request.form.get("handle", "").strip()
    dummy = request.form.get("dummy") == "on"
    tg = request.form.get("tg") in ("on", "1", "true")
    wa = request.form.get("wa") in ("on", "1", "true")
    only_amazon = bool(_form_flag("only_amazon", default=False))
    allow_amz = bool(_form_flag("allow_amazon", default=False))
    allow_ek = bool(_form_flag("allow_earnkaro", default=False))
    allow_hypd = bool(_form_flag("allow_hypd", default=False))
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )

    if not name:
        name = "Influencer-" + phone[-4:] if phone else "New Partner"
    if not tag:
        tag = config.AMAZON_ASSOCIATE_TAG

    iid = db.add_influencer(name, tag, handle=handle, phone_number=phone,
                            use_dummy_sources=dummy, telegram_enabled=tg,
                            whatsapp_enabled=wa, only_amazon=only_amazon,
                            allow_amazon=allow_amz, allow_earnkaro=allow_ek,
                            allow_hypd=allow_hypd,
                            hypd_store_id=hypd_store_id)
    msgs = []
    # 3-channel auto setup for each influencer:
    # Channel 1: Amazon Approval Telegram channel (pure amazon.in + #ad disclosure)
    # Channel 2: Real / Broadcast Telegram channel (Amazon to their tag + others to EarnKaro)
    # Channel 3: WhatsApp session & group/channel
    if tg:
        # Channel 1: Amazon Approval
        try:
            appr = _run(telegram_ops_create(iid, f"{name} Amazon Deals", "Amazon Official Deals", role="approval"))
            msgs.append(f"Channel 1 (Amazon Approval TG): {appr.get('identifier')} [{appr.get('role')}]")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 1 (Approval TG) error: {e}")
        # Channel 2: Real Broadcast
        try:
            bcast = _run(telegram_ops_create(iid, f"{name} Loots", "Best Daily Deals", role="broadcast"))
            msgs.append(f"Channel 2 (Real Broadcast TG): {bcast.get('identifier')} [{bcast.get('role')}]")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 2 (Broadcast TG) error: {e}")
    if wa:
        # Channel 3: WhatsApp
        try:
            _run(whatsapp_client.create_session(iid, "wa"))
            db.upsert_wa_session(iid, WA_SESSION_KEY(iid), phone="")
            db.set_wa_session_status(WA_SESSION_KEY(iid), "qr")
            msgs.append("Channel 3 (WhatsApp): Session started — scan QR below to complete.")
        except Exception as e:  # pragma: no cover
            msgs.append(f"Channel 3 (WhatsApp) error: {e}")
    return render_template(
        "onboard.html", result={"inf_id": iid, "name": name, "msgs": msgs},
        default_amazon_tag=config.AMAZON_ASSOCIATE_TAG,
        default_hypd_store=_effective_hypd_store_id(),
    )


@app.route("/influencer/<int:inf_id>/flags", methods=["POST"])
def set_flags(inf_id):
    tg = request.form.get("tg") in ("1", "true", "on", "yes")
    wa = request.form.get("wa") in ("1", "true", "on", "yes")
    db.set_channel_flags(inf_id, telegram_enabled=tg, whatsapp_enabled=wa)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/onboard-tg", methods=["POST"])
def onboard_tg(inf_id):
    name = db.get_influencer(inf_id)['name']
    title_appr = request.form.get("title_appr") or f"{name} Amazon Deals"
    title_bcast = request.form.get("title_bcast") or f"{name} Loots"
    try:
        _run(telegram_ops_create(inf_id, title_appr, "Amazon Official Deals", role="approval"))
        _run(telegram_ops_create(inf_id, title_bcast, "Best Daily Deals", role="broadcast"))
    except Exception as e:  # pragma: no cover
        print("onboard-tg failed:", e)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/onboard-wa", methods=["POST"])
def onboard_wa(inf_id):
    try:
        _run(whatsapp_client.create_session(inf_id, "wa"))
        db.upsert_wa_session(inf_id, WA_SESSION_KEY(inf_id), phone="")
        db.set_wa_session_status(WA_SESSION_KEY(inf_id), "qr")
    except Exception as e:  # pragma: no cover
        print("onboard-wa failed:", e)
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>")
def influencer_detail(inf_id):
    inf = db.get_influencer(inf_id)
    if not inf:
        return redirect(url_for("index"))
    channels = db.list_channels(inf_id)
    wa_sessions = db.list_wa_sessions(inf_id)
    stats = db.post_stats(inf_id)
    poll_targets = polls.eligible_poll_targets(inf, channels)
    poll_target_counts = {
        platform: sum(target.get("poll_platform") == platform for target in poll_targets)
        for platform in ("telegram", "whatsapp")
    }
    poll_history = db.list_recent_polls(inf_id)
    wa_key = WA_SESSION_KEY(inf_id)

    # Check live WhatsApp session status & fetch groups/channels
    wa_status_info = {"connected": False, "phone": "", "chats": []}
    try:
        status_res = _run(whatsapp_client.session_status(wa_key))
        if status_res.get("status") == "connected":
            wa_status_info["connected"] = True
            wa_status_info["phone"] = status_res.get("phone", "")
            chats = _run(whatsapp_client.list_chats(wa_key))
            wa_status_info["chats"] = [c for c in chats if c.get("isGroup") or c.get("isChannel")]
    except Exception:
        pass
    
    # Generate live preview demo if requested or for display
    demo_sample = (
        "🔥 Portronics Handheld Mini Fan, at Rs.799.\n"
        "https://www.amazon.in/dp/B0H5PTMXV1?tag=old-21\n"
        "Flipkart loot: https://www.flipkart.com/sony-headphones/p/itmABC123\n"
        "Myntra loot: https://www.myntra.com/bags/p/1234567"
    )
    from influencer_hub import link_router
    demo_approval = link_router.render_for_influencer(demo_sample, inf["amazon_tag"], role="approval")
    demo_broadcast = link_router.render_for_influencer(demo_sample, inf["amazon_tag"], role="broadcast")

    routing_arguments = dict(
        amazon_tag=inf.get("amazon_tag") or config.AMAZON_ASSOCIATE_TAG,
        allow_amazon=bool(inf.get("allow_amazon", 1)),
        allow_earnkaro=bool(inf.get("allow_earnkaro", 1)),
        allow_hypd=bool(inf.get("allow_hypd", 1)),
        only_amazon=bool(inf.get("only_amazon", 0)),
        hypd_store_id=str(inf.get("hypd_store_id") or "").strip() or _effective_hypd_store_id(),
    )
    return render_template(
        "influencer.html", inf=inf, channels=channels,
        wa_sessions=wa_sessions, stats=stats, wa_key=wa_key,
        wa_hub_url=config.WA_HUB_URL, wa_status_info=wa_status_info,
        poll_targets=poll_targets, poll_target_counts=poll_target_counts,
        poll_history=poll_history,
        demo_approval=demo_approval, demo_broadcast=demo_broadcast,
        routing_broadcast=_routing_preview(**routing_arguments),
        routing_approval=_routing_preview(**routing_arguments, channel_role="approval"),
        current_hypd_store=_effective_hypd_store_id(),
        current_ek_pubid=_effective_earnkaro_publisher_id(),
        earnkaro_configured=bool(
            db.get_global_setting("earnkaro_api_key") or config.EARNKARO_API_KEY
        ),
    )


@app.route("/influencer/<int:inf_id>/send-poll", methods=["POST"])
def send_poll(inf_id):
    """Explicitly dispatch one globally unique poll to selected ready targets."""
    influencer = db.get_influencer(inf_id)
    if not influencer:
        flash("Influencer profile was not found.", "error")
        return redirect(url_for("index"))

    try:
        question, options, normalized_question = polls.validate_poll(
            request.form.get("poll_question", ""),
            request.form.get("poll_options", "").splitlines(),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("influencer_detail", inf_id=inf_id))

    selected_platforms = set(request.form.getlist("poll_platforms")) & {"telegram", "whatsapp"}
    if not selected_platforms:
        flash("Select Telegram and/or WhatsApp Groups as poll destinations.", "error")
        return redirect(url_for("influencer_detail", inf_id=inf_id))

    targets = [
        target for target in polls.eligible_poll_targets(
            influencer, db.list_channels(inf_id)
        )
        if target["poll_platform"] in selected_platforms
    ]
    if not targets:
        flash(
            "No ready poll destinations are available for that platform right now. "
            "The profile must be active, the channel enabled, and its posting window open.",
            "error",
        )
        return redirect(url_for("influencer_detail", inf_id=inf_id))

    try:
        result = polls.enqueue_poll(
            inf_id,
            question,
            options,
            normalized_question,
            request.form.get("allow_multiple") == "1",
            targets,
        )
    except Exception as exc:
        print(f"Poll queueing failed for influencer {inf_id}: {exc}")
        flash("Poll could not be queued. Check service logs before trying again.", "error")
        return redirect(url_for("influencer_detail", inf_id=inf_id))

    if result.get("duplicate"):
        flash(
            "This question (or a high-confidence near-duplicate) has already been used. "
            "Choose a genuinely fresh question; no poll was queued.",
            "warning",
        )
        return redirect(url_for("influencer_detail", inf_id=inf_id))

    flash(
        f"Unique poll queued for {result.get('queued_count', 0)} destination(s). "
        "The background worker will deliver it; check Recent poll history for status.",
        "success",
    )
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/create-tg", methods=["POST"])
def create_tg(inf_id):
    title = request.form.get("title") or f"{db.get_influencer(inf_id)['name']} Loots"
    about = request.form.get("about", "")
    role = request.form.get("role", "broadcast")
    try:
        out = _run(telegram_ops_create(inf_id, title, about, role=role))
    except Exception as e:  # pragma: no cover
        out = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


async def telegram_ops_create(inf_id, title, about, role="broadcast"):
    from influencer_hub import telegram_ops
    return await telegram_ops.create_channel_for_influencer(inf_id, title, about, role=role)


@app.route("/influencer/<int:inf_id>/pair-wa", methods=["POST"])
def pair_wa(inf_id):
    key = WA_SESSION_KEY(inf_id)
    try:
        res = _run(whatsapp_client.create_session(inf_id, "wa"))
        db.upsert_wa_session(inf_id, key, phone="")
        db.set_wa_session_status(key, "qr")
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/create-group", methods=["POST"])
def create_group(inf_id):
    key = WA_SESSION_KEY(inf_id)
    participant = request.form.get("participant", "").strip()  # e.g. 9198...@s.whatsapp.net
    subject = request.form.get("subject") or f"{db.get_influencer(inf_id)['name']} Deals"
    try:
        res = _run(whatsapp_client.create_group(key, subject, participant))
        jid = res.get("jid")
        if jid:
            _save_whatsapp_destination(
                inf_id,
                {"platform": "whatsapp_group", "identifier": jid, "invite_link": ""},
                role="whatsapp",
                wa_session_key=key,
            )
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/wa-connect-chat", methods=["POST"])
def wa_connect_chat(inf_id):
    """Resolve a WhatsApp group/channel link or save a detected JID as pending."""
    chat_jid = request.form.get("chat_jid", "").strip()
    invite_link = request.form.get("invite_link", "").strip()
    raw_destination = invite_link or chat_jid
    allowed_src = request.form.get("allowed_sources", "").strip()

    destination, error = _resolve_whatsapp_destination(inf_id, raw_destination)
    if not destination:
        return redirect(url_for(
            "influencer_detail", inf_id=inf_id,
            wa_link_status="failed", wa_link_error=error,
        ))

    _save_whatsapp_destination(
        inf_id,
        destination,
        role="whatsapp",
        allowed_sources=allowed_src,
        wa_session_key=WA_SESSION_KEY(inf_id),
    )
    return redirect(url_for(
        "influencer_detail", inf_id=inf_id, wa_link_status="pending"
    ))


@app.route("/api/test-render-deal", methods=["POST"])
def api_test_render_deal():
    """Preview affiliate routing without posting to a Telegram/WhatsApp channel.

    EarnKaro is contacted only for supported merchant URLs other than Amazon
    and Meesho. HYPD is the selected Meesho route: existing HYPD affiliate
    tokens can be retagged, while raw Meesho URLs stay unchanged until an
    official HYPD generation contract is configured.
    """
    sample_text = request.form.get("sample_text", "").strip()
    amz_tag = request.form.get("amazon_tag", config.AMAZON_ASSOCIATE_TAG).strip()
    hypd_store = _routing_hypd_store(request.form.get("hypd_store_id", ""))
    role = request.form.get("role", "broadcast").strip().lower()
    allow_amazon = _form_flag("allow_amazon", default=True)
    allow_earnkaro = _form_flag("allow_earnkaro", default=True)
    allow_hypd = _form_flag("allow_hypd", default=True)
    only_amazon = bool(_form_flag("only_amazon", default=False))

    if not sample_text:
        sample_text = (
            "🔥 Loot Deals Today!\n"
            "1. HYPD Meesho: https://hypd.store/12345/afflink/daol5bac45l0tc0oo5rg\n"
            "2. Amazon Earbuds: https://www.amazon.in/dp/B08XYZ1234?tag=oldcreator-21\n"
            "3. Flipkart Shoes: https://www.flipkart.com/shoes/p/itm123456"
        )

    from influencer_hub import earnkaro, link_router

    if only_amazon:
        allow_amazon, allow_earnkaro, allow_hypd = True, False, False
    if role == "approval":
        allow_earnkaro = allow_hypd = False

    allowed_kinds = set()
    if allow_amazon:
        allowed_kinds.add("amazon")
    if allow_earnkaro:
        allowed_kinds.add("merchant")
    if allow_hypd:
        allowed_kinds.update({"hypd", "meesho"})
    if not only_amazon and role != "approval":
        allowed_kinds.add("lehlah")

    filtered_text = link_router.filter_disallowed_affiliate_links(sample_text, allowed_kinds)
    detected_links = link_router.collect_links(sample_text)
    merchant_urls = {
        url for url, kind in detected_links.items()
        if kind == "merchant" and allow_earnkaro and role != "approval"
    }
    earnkaro_links = {}
    warnings = []
    if merchant_urls:
        try:
            earnkaro_links = _run(earnkaro.convert_links(merchant_urls))
        except Exception:
            warnings.append("EarnKaro preview conversion failed; original eligible links are shown.")
        if not (db.get_global_setting("earnkaro_api_key") or config.EARNKARO_API_KEY):
            warnings.append("EarnKaro credentials are not configured; these links will not earn through EarnKaro yet.")

    raw_meesho = [url for url, kind in detected_links.items() if kind == "meesho"]
    if allow_hypd and raw_meesho:
        warnings.append(
            "Meesho is reserved for HYPD, not EarnKaro. Raw Meesho links stay unchanged until HYPD's official link generator is configured."
        )

    rendered = link_router.render_for_influencer(
        filtered_text,
        amazon_tag=amz_tag or config.AMAZON_ASSOCIATE_TAG,
        earnkaro_links=earnkaro_links,
        role=role,
        hypd_store_id=hypd_store,
    )
    return jsonify({
        "ok": True,
        "input": sample_text,
        "rendered": rendered,
        "detected_links": detected_links,
        "warnings": warnings,
        "network_settings": {
            "amazon": bool(allow_amazon),
            "earnkaro": bool(allow_earnkaro),
            "hypd": bool(allow_hypd),
            "only_amazon": bool(only_amazon),
        },
    })


@app.route("/trigger-all-hourly-loot", methods=["POST"])
def trigger_all_hourly_loot():
    """Trigger 'Loot of the Hour' highlight across all active channels."""
    try:
        from influencer_hub import pipeline
        res = _run(pipeline.run_hourly_loot_highlight())
        print(f"All channels hourly loot highlight triggered: {res}")
    except Exception as e:
        print(f"All hourly loot highlight failed: {e}")
    return redirect(url_for("index"))


@app.route("/influencer/<int:inf_id>/trigger-hourly-loot", methods=["POST"])
def trigger_hourly_loot(inf_id):
    """Manually or cron-triggered 'Loot of the Hour' highlight for this influencer."""
    try:
        from influencer_hub import pipeline
        res = _run(pipeline.run_hourly_loot_highlight(influencer_ids=[inf_id]))
        print(f"Hourly loot highlight triggered for inf_id {inf_id}: {res}")
    except Exception as e:
        print(f"Hourly loot highlight failed: {e}")
    return redirect(url_for("influencer_detail", inf_id=inf_id, hourly_loot=1))


@app.route("/channel/<int:channel_id>/send-test", methods=["POST"])
def send_test_message(channel_id):
    """Send a visible test post; activate a pending WhatsApp destination only on success."""
    inf_id = int(request.form.get("inf_id", 0))
    inf = db.get_influencer(inf_id)
    channels = db.list_channels(inf_id)
    ch = next((c for c in channels if c["id"] == channel_id), None)

    status = "failed"
    err_msg = "Channel or profile was not found."
    if ch and inf:
        test_payload = (
            f"✅ Test Alert: {inf['name']} Channel Connected Successfully!\n"
            f"Role: {ch['role'].upper()}\n"
            f"Channel: {ch['identifier']}\n"
            f"Timestamp: Auto-verification test."
        )
        try:
            from influencer_hub import pipeline
            result = _run(pipeline.dispatch_to_channel(inf, ch, test_payload))
            if result == "posted":
                status = "posted"
                err_msg = ""
                if ch.get("platform") in {"whatsapp_group", "whatsapp_channel"} and ch.get("status") == "pending":
                    db.update_channel_details(channel_id, status="ready")
            else:
                err_msg = result.removeprefix("failed:") or "The destination did not accept the test post."
        except Exception as exc:
            err_msg = str(exc)
            print(f"Test dispatch failed: {exc}")

    return redirect(url_for(
        "influencer_detail", inf_id=inf_id, test_status=status,
        test_err=err_msg, ch_name=ch["identifier"] if ch else "",
    ))


@app.route("/influencer/<int:inf_id>/create-newsletter", methods=["POST"])
def create_newsletter(inf_id):
    key = WA_SESSION_KEY(inf_id)
    name = request.form.get("name") or f"{db.get_influencer(inf_id)['name']} Channel"
    desc = request.form.get("description", "")
    try:
        res = _run(whatsapp_client.create_channel(key, name, desc))
        jid = res.get("jid")
        if jid:
            _save_whatsapp_destination(
                inf_id,
                {"platform": "whatsapp_channel", "identifier": jid, "invite_link": res.get("invite", "")},
                role="whatsapp",
                wa_session_key=key,
            )
    except Exception as e:  # pragma: no cover
        res = {"error": str(e)}
    return redirect(url_for("influencer_detail", inf_id=inf_id))


def _wa_hub_get(path: str) -> dict:
    """Authenticated read-only proxy to the loopback WhatsApp hub."""
    headers = {"Accept": "application/json"}
    if config.WA_HUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.WA_HUB_TOKEN}"
    req = urllib.request.Request(
        f"{config.WA_HUB_URL.rstrip('/')}{path}",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())


@app.route("/api/wa/<key>/qr")
def wa_qr(key):
    try:
        return jsonify(_wa_hub_get(f"/sessions/{key}/qr"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "qr": None})


@app.route("/api/wa/<key>/status")
def wa_status(key):
    try:
        return jsonify(_wa_hub_get(f"/sessions/{key}"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@app.route("/vm")
def vm():
    vm = db.latest_vm()
    return render_template("vm.html", vm=vm)


@app.route("/api/vm")
def api_vm():
    return jsonify(db.latest_vm() or {})


if __name__ == "__main__":
    db.init()
    app.run(host="0.0.0.0", port=5000, debug=False)
