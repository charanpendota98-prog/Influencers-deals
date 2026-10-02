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
import re
import secrets
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for, session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from influencer_hub import (
    config, db, hypd_shortlinks, lehlah_shortlinks, puller, telegram_ops, whatsapp_client,
)  # noqa: E402

app = Flask(__name__)
app.secret_key = config.DASHBOARD_SECRET_KEY or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_NAME="influencer_hub_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.DASHBOARD_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
WA_SESSION_KEY = lambda influencer_id: f"inf-{influencer_id}-wa"
_LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
REAUTH_WINDOW_SECONDS = 90
REAUTH_REQUIRED_ENDPOINTS = frozenset({
    "seed_default_sources", "add_deal_source", "delete_deal_source",
    "toggle_deal_source", "update_global_settings", "quick_add",
    "update_profile", "update_channel_route", "add_manual_channel",
    "bulk_import", "delete_channel", "toggle_influencer_active",
    "toggle_channel_status", "onboard", "set_flags", "onboard_tg",
    "onboard_wa", "create_tg", "pair_wa", "create_group",
    "wa_connect_chat", "create_newsletter", "send_test_message",
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
    """Return the live central HYPD Store ID used for new profiles/channels."""
    configured = db.get_global_setting("hypd_store_id", config.HYPD_STORE_ID)
    return str(configured or config.HYPD_STORE_ID).strip() or config.HYPD_STORE_ID


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


@app.context_processor
def inject_dashboard_security_context():
    return {
        "csrf_token": _get_csrf_token,
        "dashboard_authenticated": bool(session.get("dashboard_authenticated")),
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
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = str(session.get("_csrf_token") or "")
        supplied = str(
            request.form.get("_csrf_token")
            or request.headers.get("X-CSRF-Token")
            or ""
        )
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({"ok": False, "error": "csrf_validation_failed"}), 400

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
        reauthenticated_at = session.get("_recent_reauth_at")
        try:
            reauth_is_fresh = (
                reauthenticated_at is not None
                and 0 <= time.time() - float(reauthenticated_at) <= REAUTH_WINDOW_SECONDS
            )
        except (TypeError, ValueError):
            reauth_is_fresh = False
        if not reauth_is_fresh:
            session.pop("_recent_reauth_at", None)
            return jsonify({"ok": False, "error": "reauthentication_required"}), 428
        # The confirmation is one-use: every protected setup action asks again.
        session.pop("_recent_reauth_at", None)
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


def _dashboard_security_ready() -> bool:
    if not config.DASHBOARD_ADMIN_PASSWORD:
        return False
    if config.HUB_ENV == "production":
        return bool(config.DASHBOARD_SECRET_KEY) and len(config.DASHBOARD_ADMIN_PASSWORD) >= 16
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
    """Issue a one-use confirmation for a single sensitive setup action."""
    if not session.get("dashboard_authenticated"):
        return jsonify({"ok": False, "error": "authentication_required"}), 401

    client_ip = request.remote_addr or "unknown"
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
        return jsonify({"ok": False, "error": "password_confirmation_failed"}), 401

    _LOGIN_FAILURES.pop(bucket_key, None)
    session["_recent_reauth_at"] = time.time()
    return jsonify({"ok": True})


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
    )
    if needs_test:
        return redirect(url_for(
            "influencer_detail", inf_id=owner_id, wa_link_status="pending"
        ))
    return redirect(url_for("influencer_detail", inf_id=owner_id))


@app.route("/influencer/<int:inf_id>/add-manual-channel", methods=["POST"])
def add_manual_channel(inf_id):
    platform = request.form.get("platform", "telegram").strip()
    raw_ident = request.form.get("identifier", "").strip()
    role = request.form.get("role", "broadcast").strip()
    invite = request.form.get("invite", "").strip()
    override_tag = request.form.get("amazon_override_tag", "").strip()
    price_filt = request.form.get("price_filter", "").strip()
    allowed_src = request.form.get("allowed_sources", "").strip()
    wa_key = request.form.get("wa_session_key", "").strip()
    bitly_key = request.form.get("bitly_api_key", "").strip()
    categories = request.form.get("categories", "").strip()
    schedule = request.form.get("posting_schedule", "").strip()
    only_amz = bool(_form_flag("only_amazon", default=False))
    strip_amz = bool(_form_flag("strip_amazon", default=False))
    allow_amz = bool(_form_flag("allow_amazon", default=False))
    allow_ek = bool(_form_flag("allow_earnkaro", default=False))
    allow_hypd = bool(_form_flag("allow_hypd", default=False))
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )

    if raw_ident:
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
    inf_id = request.form.get("inf_id")
    db.delete_channel(channel_id)
    if inf_id:
        return redirect(url_for("influencer_detail", inf_id=int(inf_id)))
    return redirect(url_for("index"))


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
    pwd = request.form.get("admin_password", "").strip()
    if not _password_matches(pwd, config.ADMIN_DELETE_PASSWORD):
        return redirect(url_for("influencer_detail", inf_id=inf_id, err="invalid_password"))
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

    return render_template(
        "influencer.html", inf=inf, channels=channels,
        wa_sessions=wa_sessions, stats=stats, wa_key=wa_key,
        wa_hub_url=config.WA_HUB_URL, wa_status_info=wa_status_info,
        demo_approval=demo_approval, demo_broadcast=demo_broadcast,
        current_hypd_store=_effective_hypd_store_id(),
        current_ek_pubid=_effective_earnkaro_publisher_id(),
        earnkaro_configured=bool(
            db.get_global_setting("earnkaro_api_key") or config.EARNKARO_API_KEY
        ),
    )


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
    hypd_store = request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
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
