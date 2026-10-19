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
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from flask import (
    Flask, Response, abort, flash, jsonify, redirect, render_template, request,
    url_for, session,
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from influencer_hub import (
    age, amazon_shortlinks, amazon_watch, config, creators, db, guard, hypd_shortlinks,
    inventory, lehlah_shortlinks, link_router, onboarding, ops, presets, security,
    polls, pipeline, puller, readiness, selfcheck, sources, tag_proof,
    telegram_ops, whatsapp_client, channel_navigation,
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


def _safe_next_url(default: str) -> str:
    """Return the form's ``next`` path when it is a safe, local URL.

    Lets one screen (Easy Setup) trigger an action that normally lives on the
    creator page and come straight back, without ever redirecting off-site or
    accepting a scheme-relative URL.
    """
    candidate = (request.form.get("next") or "").strip()
    if candidate.startswith("/") and not candidate.startswith("//"):
        if "\r" not in candidate and "\n" not in candidate:
            return candidate
    return default


def _redirect_after_action(default: str, **params) -> "Response":
    """Redirect to ``next`` when given, else back to ``default`` — status kept.

    Query parameters from the caller (``wa_pair_status``, ``test_status``, …) are
    merged into whichever URL wins, so the status is never lost when an action
    is triggered from Easy Setup instead of the creator page.
    """
    def _with_params(url: str) -> str:
        if not params:
            return url
        parsed = urlparse(url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({key: str(value) for key, value in params.items()})
        return parsed._replace(query=urlencode(query)).geturl()

    next_url = _safe_next_url("")
    return redirect(_with_params(next_url) if next_url else _with_params(default))


def _form_flag_or_none(name: str) -> bool | None:
    """Checkbox/hidden pair -> True/False, or None when the field was not sent.

    ``None`` means "this form does not manage the option". A caller that only
    sends the channel identifier must inherit the creator's routing instead of
    silently switching Amazon/EarnKaro/HYPD off (which used to leave a brand-new
    channel with every network disabled, so nothing was ever posted to it).
    """
    if not request.form.getlist(name):
        return None
    return _form_flag(name, default=False)


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


def _custom_button_urls_valid(*urls: str | None) -> bool:
    """Custom post CTAs require HTTPS URLs or supported Telegram @handles."""
    return all(
        not str(url or "").strip()
        or bool(channel_navigation.normalize_custom_button_url(str(url)))
        for url in urls
    )


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


def _telegram_destination_info(identifier: str) -> dict:
    """Read-only Telegram peek: the destination's real name, and our rights there.

    The operator's rule for both messengers is the same — "paste it, and the hub
    should take it, name it and keep posting into it". A Telegram destination
    therefore gets the same treatment as the WhatsApp picker: Telegram itself
    reports the channel's title and whether this account may post there
    (`telegram_ops.inspect_channel` is read-only — it never joins, never posts).

    Never raises and never blocks an add: with no session on the box, or with
    Telegram unreachable, the caller saves exactly what was pasted.
    """
    try:
        if not telegram_ops.session_ready():
            return {"ok": False, "error": "no Telegram session on this box yet"}
        info = _run(telegram_ops.inspect_channel(identifier)) or {}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}
    return info if isinstance(info, dict) else {"ok": False, "error": "unexpected reply"}


def _telegram_name_note(info: dict, ident: str) -> tuple[str, str]:
    """``(saved name, operator note)`` from a Telegram peek.

    The name is what Telegram says the destination is called (kept only when it
    really answered). The note warns when the account we would post with is not
    an admin there — the one failure that otherwise shows up as a silent
    "nothing happens" on delivery day.
    """
    if not info.get("ok"):
        return "", ""
    title = str(info.get("title") or "").strip()[:120]
    if info.get("can_post") is False:
        label = title or ident
        return title, (f" Saved as “{label}”, but this account cannot post there yet: make one "
                       "of our Telegram accounts an admin of it (or check the invite), or "
                       "leave it as the approval-only destination.")
    return title, ""


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


def _whatsapp_chat_title(inf_id: int, jid: str) -> str:
    """The name WhatsApp shows for a destination, from the paired phone's list.

    Used when a link is pasted instead of picked: the operator should still end
    up with a readable name beside the JID. Best-effort — a hub that cannot be
    reached leaves the title empty rather than failing the save.
    """
    key = WA_SESSION_KEY(inf_id)
    for lister in ("list_admin_chats", "list_chats"):
        try:
            chats = _run(getattr(whatsapp_client, lister)(key))
        except Exception:
            continue
        for chat in chats or []:
            if str(chat.get("jid") or "") == jid:
                return str(chat.get("name") or "").strip()[:120]
    return ""


def _save_whatsapp_destination(
    inf_id: int,
    destination: dict,
    *,
    role: str = "whatsapp",
    allowed_sources: str = "",
    wa_session_key: str = "",
    channel_settings: dict | None = None,
    title: str = "",
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
            title=title or existing.get("title") or "",
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
        title=title,
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
    if request.endpoint == "team_login":
        return render_template(
            "team_login.html", next_url=request.values.get("next", ""),
            login_error="Your sign-in session expired. Enter the team password again.",
            team_configured=security.team_access_configured(),
        ), 400
    if request.endpoint == "login":
        # A lost/expired session on the sign-in screen: hand back a fresh page
        # instead of a bare 400 so the operator can simply sign in again.
        return render_template(
            "login.html",
            next_url=request.values.get("next", ""),
            login_error="Your sign-in session expired. Enter the admin password again.",
            totp_required=security.totp_enabled(),
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
        "dashboard_role": _session_role(),
        "is_team_session": _is_team(),
        "team_name": session.get("team_name") or "",
        "team_access_configured": security.team_access_configured(),
        "admin_totp_enabled": security.totp_enabled(),
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
        "static", "login", "team_login", "healthz", "amazon_short_link",
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
        if request.path == "/team" or request.path.startswith("/team/") \
                or request.path.startswith("/api/team"):
            if request.path.startswith("/api/team"):
                return jsonify({"ok": False, "error": "authentication_required"}), 401
            return redirect(url_for("team_login", next=request.path))
        destination = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=destination))

    # The team role may reach the team pages and nothing else. This is checked
    # before any view runs, so a bookmarked admin URL, a crafted form post and
    # an API call all fail the same way — editing is not hidden, it is refused.
    if _is_team() and endpoint not in security.TEAM_ALLOWED_ENDPOINTS:
        security.audit(_session_actor(), security.TEAM, "team_blocked_route",
                       target=request.path, detail=f"{request.method} {endpoint or ''}",
                       ip=_client_ip())
        if request.path.startswith("/api/"):
            return jsonify({
                "ok": False,
                "error": "admin_only",
                "detail": "Editing needs the admin sign-in. Ask the owner to make the change.",
            }), 403
        flash("That page is admin-only. You can add creators on this page — ask the owner "
              "to edit anything that is already saved.", "warning")
        return redirect(url_for("team_home"))

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


# Mutations that already write their own, more specific audit line.
_AUDIT_EXEMPT_ENDPOINTS = {
    "static", "login", "logout", "team_login", "team_add", "reauthenticate_setup_change",
    "lock_setup_changes", "amazon_short_link", "meesho_hypd_short_link",
    "lehlah_meesho_short_link", "healthz",
    "category_preview_api",  # read-only POST: product text stays out of URLs/logs
    "save_product_preferences",  # writes its own creator-scoped audit event
}


@app.after_request
def audit_admin_changes(response):
    """One audit line for every change the admin really made.

    This is deliberately generic: any future POST/PUT/PATCH/DELETE endpoint is
    recorded without remembering to add a call, and a request that was stopped
    for re-authentication (a redirect to /reauth) is *not* recorded, because it
    changed nothing.
    """
    try:
        # Only the admin role can change anything, so only the admin role can
        # produce a change line — a refused team request must not look like one.
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} \
                and _session_role() == security.ADMIN:
            endpoint = request.endpoint or ""
            location = str(response.headers.get("Location") or "")
            blocked_for_reauth = "/reauth" in location or location.endswith("/login")
            if (endpoint not in _AUDIT_EXEMPT_ENDPOINTS
                    and 200 <= response.status_code < 400
                    and not blocked_for_reauth):
                security.audit(_session_actor(), _session_role(), f"change:{endpoint}",
                               target=request.path, ip=_client_ip())
    except Exception:  # pragma: no cover - auditing must never break a response
        pass
    return response


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


def _session_role() -> str:
    """The role of the signed-in session.

    A session created before roles existed (``dashboard_authenticated`` with no
    role) is the admin's, so an upgrade never locks the owner out.
    """
    if not session.get("dashboard_authenticated"):
        return ""
    role = str(session.get("dashboard_role") or "").strip().lower()
    return role if role in {security.ADMIN, security.TEAM} else security.ADMIN


def _session_actor() -> str:
    """The name that goes into the audit log for this session."""
    if _session_role() == security.TEAM:
        return str(session.get("team_name") or "team").strip() or "team"
    return "admin"


def _is_team() -> bool:
    return _session_role() == security.TEAM


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
        return render_template("login.html", next_url=next_url, login_error="",
                               totp_required=security.totp_enabled())

    client_ip = request.remote_addr or "unknown"
    state = security.block_state(security.ADMIN, client_ip)
    if state["blocked"]:
        security.audit("admin", security.ADMIN, "admin_login_blocked", ip=client_ip,
                       detail=f"{state['failures']} failed attempts")
        return render_template(
            "login.html", next_url=next_url, totp_required=security.totp_enabled(),
            login_error="Too many sign-in attempts. Try again in 15 minutes.",
        ), 429

    supplied = request.form.get("password", "")
    password_ok = security.admin_password_matches(supplied)
    code_ok = True
    if security.totp_enabled():
        code_ok = security.verify_totp(request.form.get("totp", ""))
    if not (password_ok and code_ok):
        security.note_login(security.ADMIN, client_ip, ok=False)
        security.audit("admin", security.ADMIN, "admin_login_failed", ip=client_ip,
                       detail="password" if not password_ok else "two-factor code")
        message = ("Sign-in failed. Check the password and try again."
                   if not password_ok else
                   "The password was right but the six-digit code did not match. "
                   "Codes change every 30 seconds — enter the current one.")
        return render_template(
            "login.html", next_url=next_url, totp_required=security.totp_enabled(),
            login_error=message,
        ), 401

    security.note_login(security.ADMIN, client_ip, ok=True)
    security.audit("admin", security.ADMIN, "admin_login_ok", ip=client_ip)
    session.clear()
    session.permanent = True
    session["dashboard_authenticated"] = True
    session["dashboard_role"] = security.ADMIN
    session["auth_at"] = int(time.time())
    session["_csrf_token"] = secrets.token_urlsafe(32)
    return redirect(_safe_local_redirect(next_url))


# --------------------------------------------------------------------------
# Team access: add-only pages with their own password
# --------------------------------------------------------------------------
@app.route("/team/login", methods=["GET", "POST"])
def team_login():
    """Team sign-in: their own name and their own password (never the admin's)."""
    if not security.team_access_configured():
        return render_template(
            "team_login.html", next_url="", team_configured=False,
            login_error="Team access is not switched on yet — ask the owner to set "
                        "DASHBOARD_TEAM_PASSWORD in the server environment.",
        ), 503
    if _session_role() == security.ADMIN:
        return redirect(url_for("team_home"))
    if _session_role() == security.TEAM:
        return redirect(url_for("team_home"))

    next_url = request.values.get("next", "")
    if request.method == "GET":
        return render_template("team_login.html", next_url=next_url, login_error="",
                               team_configured=True)

    client_ip = _client_ip()
    state = security.block_state(security.TEAM, client_ip)
    if state["blocked"]:
        security.audit("team", security.TEAM, "team_login_blocked", ip=client_ip,
                       detail=f"{state['failures']} failed attempts")
        return render_template(
            "team_login.html", next_url=next_url, team_configured=True,
            login_error="Too many attempts. Try again in 15 minutes.",
        ), 429

    member = " ".join(str(request.form.get("name", "")).split())[:40]
    if not member:
        security.note_login(security.TEAM, client_ip, ok=False)
        return render_template(
            "team_login.html", next_url=next_url, team_configured=True,
            login_error="Type your name first (it is recorded with every creator you add).",
        ), 400
    if not security.team_password_matches(request.form.get("password", "")):
        security.note_login(security.TEAM, client_ip, ok=False)
        security.audit(member, security.TEAM, "team_login_failed", ip=client_ip)
        return render_template(
            "team_login.html", next_url=next_url, team_configured=True,
            login_error="Sign-in failed. Check the team password with the owner.",
        ), 401

    security.note_login(security.TEAM, client_ip, ok=True)
    security.audit(member, security.TEAM, "team_login_ok", ip=client_ip)
    session.clear()
    session.permanent = True
    session["dashboard_authenticated"] = True
    session["dashboard_role"] = security.TEAM
    session["team_name"] = member
    session["auth_at"] = int(time.time())
    session["_csrf_token"] = secrets.token_urlsafe(32)
    return redirect(_safe_local_redirect(next_url) if next_url else url_for("team_home"))


@app.route("/team")
def team_home():
    """What the team sees: add creators, and the ones they already added."""
    member = _session_actor()
    quota = security.team_quota(member, limit=config.TEAM_ADDS_PER_HOUR)
    mine = db.list_influencers(added_by=member)
    plan = None
    try:
        if mine:
            plan = readiness.overview(
                influencers=mine,
                heartbeat=db.get_worker_heartbeat(),
                sources=db.list_sources(active_only=False),
                limit=100,
            )
    except Exception as exc:  # pragma: no cover - the page must still render
        app.logger.warning("team readiness failed: %s", exc)
    audit_rows = db.list_audit(limit=25, actor=member)
    return render_template(
        "team.html",
        member=member,
        quota=quota,
        plan=plan,
        added_total=len(mine),
        audit_rows=audit_rows,
        bulk_max=BULK_MAX_ROWS,
        add_defaults=presets.resolved(),
        add_defaults_line=presets.summary_line(),
    )


@app.route("/team/add", methods=["POST"])
def team_add():
    """Add creators (one form or a paste). No edit, no delete, no settings."""
    member = _session_actor()
    quota = security.team_quota(member, limit=config.TEAM_ADDS_PER_HOUR)
    if not quota["allowed"]:
        security.audit(member, security.TEAM, "team_quota_blocked", ip=_client_ip(),
                       detail=f"{quota['used']} adds in the last hour")
        flash(f"You have added {quota['used']} creators in the last hour (limit "
              f"{quota['limit']}). Ask the owner to raise it, or continue in a while.", "warning")
        return redirect(url_for("team_home"))

    # The owner set these once (👥 New-creator defaults on /setup): every network
    # on, OUR central HYPD store, WhatsApp mirroring on for the main channel.
    defaults = {"name": "", **_new_creator_defaults(),
                "hypd_store_id": (presets.resolved().get("hypd_store_id")
                                  or _effective_hypd_store_id())}
    rows: list[dict] = []
    if str(request.form.get("bulk_lines", "")).strip():
        rows = parse_bulk_creators(request.form.get("bulk_lines", ""))
    else:
        rows = [{
            "line": 1,
            "name": request.form.get("name", ""),
            "tag": request.form.get("amazon_tag", ""),
            "main": request.form.get("main_channel", ""),
            "approval": request.form.get("approval_channel", ""),
            "whatsapp": request.form.get("whatsapp_id", ""),
        }]
    if not rows:
        flash("Fill the form (or paste one creator per line) and try again.", "warning")
        return redirect(url_for("team_home"))

    known_profiles = {
        str(profile.get("name") or "").strip().lower(): profile
        for profile in db.list_influencers()
    }
    # The assignment guard, read once: a row that would put two creators on one
    # Amazon tag (or one channel on two creators) is refused by name, not added.
    index = guard.GuardIndex.from_db()
    results: list[dict] = []
    for row in rows[:BULK_MAX_ROWS]:
        values, approval_ident, main_ident, error = _bulk_prepare_row(row, defaults)
        name = str(row.get("name") or "?").strip() or "?"
        if error:
            results.append({"name": name, "status": "failed", "detail": error})
            continue
        taken = index.preflight(name=values["name"], tag=values["amazon_tag"],
                               approval=approval_ident, main=main_ident,
                               whatsapp=values.get("whatsapp") or "")
        if taken["errors"]:
            results.append({"name": name, "status": "failed",
                            "detail": "⛔ " + index.explain(taken)})
            security.audit(member, security.TEAM, "add_blocked_duplicate",
                           target=name, detail=index.explain(taken)[:180], ip=_client_ip())
            continue
        # The team may only ADD: a name that already exists is reported, never
        # silently updated (that would be an edit through the back door).
        if name.lower() in known_profiles:
            results.append({"name": name, "status": "exists",
                            "detail": "already in the hub — ask the owner to change it"})
            continue
        try:
            inf_id = db.add_influencer(
                values["name"], values["amazon_tag"], added_by=member,
                allow_amazon=values["allow_amazon"],
                allow_earnkaro=values["allow_earnkaro"], allow_hypd=values["allow_hypd"],
                only_amazon=values["only_amazon"], hypd_store_id=values["hypd_store_id"],
                # Whatever the row carried beyond the channels (a phone, a niche,
                # the hours they want) is saved too — never a guess, only values
                # the parser actually recognised.
                phone_number=str(values.get("phone_number") or "").strip(),
                categories=str(values.get("categories") or "").strip(),
                posting_schedule=str(values.get("posting_schedule") or "").strip(),
                price_filter=str(values.get("price_filter") or "").strip() or "all",
                notes=str(values.get("notes") or "").strip(),
            )
        except Exception as exc:  # pragma: no cover - keep the batch going
            results.append({"name": name, "status": "failed", "detail": str(exc)[:200]})
            continue
        detail_bits = []
        for role, identifier in (("approval", approval_ident), ("broadcast", main_ident)):
            if identifier:
                try:
                    db.add_channel(inf_id, "telegram", identifier, status="ready", role=role,
                                   mirror_whatsapp="1" if role == "broadcast" else "")
                    detail_bits.append(identifier)
                except Exception as exc:  # pragma: no cover - report, keep going
                    detail_bits.append(f"{role} failed: {str(exc)[:80]}")
        whatsapp_saved, whatsapp_note = _bulk_whatsapp(values, int(inf_id))
        if whatsapp_saved:
            detail_bits.append(whatsapp_saved[0])
        if whatsapp_note:
            detail_bits.append(f"⚠ {whatsapp_note}")
        known_profiles[name.lower()] = db.get_influencer(inf_id) or {"id": inf_id, "name": name}
        index.note(inf_id, values["amazon_tag"], name=values["name"],
                   channels=[{"platform": "telegram", "identifier": identifier, "role": role}
                             for role, identifier in (("approval", approval_ident),
                                                      ("broadcast", main_ident)) if identifier]
                   + ([{"platform": "whatsapp", "identifier": values["whatsapp"], "role": "whatsapp"}]
                      if values.get("whatsapp") else []))
        security.audit(member, security.TEAM, "creator_added", target=values["name"],
                       detail=f"tag {values['amazon_tag']} · {' · '.join(detail_bits) or 'no channels'}",
                       ip=_client_ip())
        results.append({"name": values["name"], "status": "created",
                        "detail": " · ".join(detail_bits) or "no channels"})

    created = sum(1 for row in results if row["status"] == "created")
    failed = sum(1 for row in results if row["status"] == "failed")
    existing = sum(1 for row in results if row["status"] == "exists")
    summary = (f"✅ {created} added" if created else "Nothing added")
    if existing:
        summary += f" · {existing} already in the hub (not changed)"
    if failed:
        summary += f" · {failed} could not be added"
    flash(summary, "success" if created and not failed else ("error" if failed and not created else "warning"))
    session["team_last_results"] = results[:50]
    return redirect(url_for("team_home"))


@app.route("/api/team/creators")
def team_creators():
    """The creators this member added (read-only, capped)."""
    member = _session_actor()
    mine = db.list_influencers(added_by=member)
    rows = [{
        "id": inf.get("id"), "name": inf.get("name"), "amazon_tag": inf.get("amazon_tag"),
        "active": bool(inf.get("active")),
    } for inf in mine[:500]]
    return jsonify({"ok": True, "member": member, "count": len(mine), "creators": rows})


@app.route("/logout", methods=["POST"])
def logout():
    was_team = _is_team()
    actor, role = _session_actor(), _session_role()
    session.clear()
    if actor and role:
        security.audit(actor, role, "signed_out", ip=_client_ip())
    return redirect(url_for("team_login") if was_team else url_for("login"))


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


@app.route("/a/<code>")
@app.route("/amazon/<code>")  # legacy links stay live after compact /a/ rollout
def amazon_short_link(code: str):
    """Resolve one stored compact Amazon link to its exact tagged product target."""
    if not amazon_shortlinks.is_valid_short_code(code):
        abort(404)
    record = db.get_amazon_short_link(code)
    if not record:
        abort(404)

    requested_tags = request.args.getlist("tag")
    tag = str(record.get("associate_tag") or "")
    if len(requested_tags) != 1 or requested_tags[0].strip() != tag:
        abort(404)

    target = str(record.get("target_url") or "")
    if not amazon_shortlinks.is_valid_amazon_target(target, tag):
        # Only redirect to a generated canonical Amazon product URL with this
        # exact creator tag; never treat the DB or query string as an open
        # redirect instruction.
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
    # Read-only checklist for every creator: what is done and what the single
    # next action is. Database only — the live WhatsApp overlay is fetched by
    # /api/readiness?live=1 so a slow hub can never hold this page open.
    #
    # A hub with a thousand creators renders one page of the table, never the
    # whole list: search, state filter and paging are server-side and the live
    # refresh asks for exactly the rows on screen.
    query = (request.args.get("q") or "").strip()
    state = (request.args.get("state") or "").strip().lower()
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    page_size = max(5, min(200, request.args.get("page_size", 25, type=int) or 25))
    try:
        plan = readiness.overview(
            influencers=influencers,
            heartbeat=db.get_worker_heartbeat(),
            sources=sources,
            query=query,
            state=state,
            limit=page_size,
            offset=offset,
        )
    except Exception as exc:  # pragma: no cover - the dashboard must still render
        app.logger.warning("readiness overview failed: %s", exc)
        plan = None
    # The self-check panel: same checks the API returns, minus the one network
    # call (the browser asks /api/self-check?live=1 for that).
    heartbeat = db.get_worker_heartbeat()
    try:
        report = selfcheck.run_checks(
            live=False, influencers=influencers, sources=sources,
            heartbeat=heartbeat, plan=plan,
        )
    except Exception as exc:  # pragma: no cover - the dashboard must still render
        app.logger.warning("self-check failed: %s", exc)
        report = None

    # Ops digest: the same numbers the ops-channel card and `cli digest` print.
    digest = None
    try:
        digest = ops.digest(hours=24, plan=plan, sources=sources, influencers=influencers,
                            heartbeat=heartbeat, report=report, insights=insights)
    except Exception as exc:  # pragma: no cover - the dashboard must still render
        app.logger.warning("ops digest failed: %s", exc)
        digest = None
    paging = {"offset": offset, "page_size": page_size, "prev": max(0, offset - page_size),
              "next": offset + page_size}
    if plan:
        paging["next"] = paging["next"] if paging["next"] < plan["matched"] else None
        paging["prev"] = paging["prev"] if offset else None
        paging["first_shown"] = offset + 1 if plan["returned"] else 0
        paging["last_shown"] = offset + plan["returned"]

    ready_count = int((plan or {}).get("summary", {}).get("ready", 0))
    worker_ready = bool((plan or {}).get("globals", {}).get("worker", {}).get("done"))
    launch_steps = [
        {"label": "Add a creator", "done": bool(influencers), "url": url_for("easy_setup"),
         "detail": "Create a creator profile and assign their own Amazon tag."},
        {"label": "Connect deal sources", "done": active_sources > 0,
         "url": url_for("setup") + "#source-pool",
         "detail": f"{active_sources} active source(s) · sources feed every creator."},
        {"label": "Verify a destination", "done": ready_count > 0,
         "url": url_for("index") + "#command-center",
         "detail": f"{ready_count} creator(s) ready to receive deals."},
        {"label": "Start the worker", "done": worker_ready, "url": url_for("vm"),
         "detail": "The background worker moves deals from sources to channels."},
    ]
    launch_done = sum(step["done"] for step in launch_steps)
    if not influencers:
        quick_action = {"label": "Create your first creator", "detail": "Use the guided flow, or fill the one-screen Easy Setup.",
                        "url": url_for("guided_setup")}
    elif not active_sources:
        quick_action = {"label": "Connect deal sources", "detail": "Choose already-joined Telegram sources; this does not join invite links.",
                        "url": url_for("setup") + "#source-pool"}
    elif plan and plan.get("actions"):
        next_action = plan["actions"][0]
        quick_action = {"label": next_action["label"], "detail": next_action["action"],
                        "url": next_action.get("url") or (url_for("index") + "#self-check")}
    elif report and report.get("verdict") != "ready":
        quick_action = {"label": "Review system health", "detail": "Open the read-only self-check to see what needs attention.",
                        "url": url_for("index") + "#self-check"}
    else:
        quick_action = {"label": "Add another creator", "detail": "Your current creators and worker look ready.",
                        "url": url_for("easy_setup")}

    return render_template(
        "index.html",
        creator_count=len(influencers),
        stats=stats,
        vm=vm,
        insights=insights,
        money=_money_summary(),
        source_count=len(sources),
        active_source_count=active_sources,
        plan=plan,
        report=report,
        digest=digest,
        query=query,
        state=state,
        paging=paging,
        launch_steps=launch_steps,
        launch_done=launch_done,
        launch_action=quick_action,
        # How long the fleet has been running, for the command-center chips.
        hub_age=age.summarise(influencers),
        recent_creators=_recent_creators(age_since_days=30, influencers=influencers),
        # Whose Amazon tag signs which channel, for the command-center chip.
        tag_fleet=_tag_fleet_snapshot(influencers),
    )


def _tag_fleet_snapshot(influencers: list[dict] | None = None) -> dict:
    """The fleet tag proof, cheap enough for the dashboard home.

    ``sample=False``: the chip only needs the counts, not a rendered link per
    channel, so a 1000-creator hub pays for one pass over rows it already has.
    """
    try:
        fleet = tag_proof.fleet(influencers=influencers, sample=False)
    except Exception as exc:  # pragma: no cover - the dashboard must render
        return {"error": str(exc)[:160], "ok": True, "blocked": [], "channels": 0,
                "own": 0, "fallback": 0, "summary": "", "fix": ""}
    fleet["fix"] = tag_proof.describe_fix(fleet.get("blocked") or [])
    return fleet


def _recent_creators(age_since_days: int, *, influencers: list[dict] | None = None) -> dict:
    """The creators set up in the last N days: when, who, and how many.

    This is the answer to “when many people are adding creators, how does the
    owner see it?” — newest first, with the member's name next to each row.
    """
    rows_source = influencers if influencers is not None else db.list_influencers()
    recent: list[dict] = []
    for profile in rows_source:
        days = age.days_since(profile.get("created_at"))
        if days is None or days > age_since_days:
            continue
        recent.append({
            "id": int(profile.get("id") or 0),
            "name": str(profile.get("name") or ""),
            "added_by": str(profile.get("added_by") or ""),
            "created_date": str(profile.get("created_at") or "")[:10],
            "age": age.human_age(profile.get("created_at")),
            "days": days,
        })
    recent.sort(key=lambda row: (row["days"] or 0))
    return {"count": len(recent), "window_days": age_since_days, "rows": recent[:12]}


@app.route("/api/readiness")
def api_readiness():
    """The same checklist as JSON; ``?live=1`` adds the hub's real session state.

    Read-only: it never creates a session, sends a message or flips a switch.
    """
    inf_id = request.args.get("inf_id", type=int)
    live = str(request.args.get("live", "")).strip() in {"1", "true", "yes"}
    query = (request.args.get("q") or "").strip()
    state = (request.args.get("state") or "").strip().lower()
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    limit = max(0, request.args.get("limit", 0, type=int) or 0)
    live_wa: dict[str, str] = {}
    live_error = ""
    if live:
        try:
            snapshot = _wa_hub_get("/sessions")
            for row in snapshot.get("sessions") or []:
                key = str(row.get("key") or row.get("session_key") or "")
                status = str(row.get("status") or "")
                if key and status:
                    live_wa[key] = status
        except Exception as exc:
            live_error = f"{type(exc).__name__}: {exc}".strip()[:200]
    try:
        if inf_id:
            inf = db.get_influencer(inf_id)
            if not inf:
                return jsonify({"ok": False, "error": "unknown creator"}), 404
            payload = readiness.overview(
                influencers=[inf], live_wa=live_wa or None,
                query=query, state=state, limit=limit, offset=offset,
            )
        else:
            payload = readiness.overview(
                live_wa=live_wa or None,
                query=query, state=state, limit=limit, offset=offset,
            )
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    payload["ok"] = True
    payload["live"] = live
    payload["live_error"] = live_error
    return jsonify(payload)


@app.route("/api/self-check")
def api_self_check():
    """Read-only "does the hub work?" report; ``?live=1`` also pings the hub.

    Contains booleans, counts and short messages only — never a key, token or
    session string.
    """
    live = str(request.args.get("live", "")).strip() in {"1", "true", "yes"}
    inf_id = request.args.get("inf_id", type=int)
    try:
        if inf_id:
            inf = db.get_influencer(inf_id)
            if not inf:
                return jsonify({"ok": False, "error": "unknown creator"}), 404
            report = selfcheck.run_checks(live=live, influencers=[inf])
        else:
            report = selfcheck.run_checks(live=live)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    report["ok"] = bool(report.get("ok"))
    if str(request.args.get("preview_alert", "")).strip() in {"1", "true", "yes"}:
        # "What would the ops channel be told right now?" — computed with the
        # pure policy functions, so it neither sends a message nor moves the
        # cooldown/incident state.
        try:
            last_verdict = db.get_global_setting(ops.LAST_VERDICT_KEY, "")
            last_alert_at = db.get_global_setting(ops.LAST_ALERT_KEY, "")
            decision = ops.alert_decision(report, last_verdict=last_verdict,
                                          last_alert_at=last_alert_at)
            report["alert_preview"] = {
                "decision": decision,
                "message": ops.render_alert(report, decision, ops._incident(datetime.now(timezone.utc))),
                "sends": bool(decision.get("alert")),
                "ops_channel_configured": bool(str(config.OPS_TELEGRAM_CHANNEL or "").strip()),
            }
        except Exception as exc:  # pragma: no cover - defensive
            report["alert_preview"] = {"error": str(exc)[:200]}
    return jsonify(report)


@app.route("/api/readiness.csv")
def api_readiness_csv():
    """The readiness checklist as a spreadsheet (same filters as /api/readiness)."""
    query = (request.args.get("q") or "").strip()
    state = (request.args.get("state") or "").strip().lower()
    try:
        plan = readiness.overview(query=query, state=state)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    csv_text = ops.readiness_csv(plan)
    return Response(
        csv_text,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=creator-readiness.csv"},
    )


@app.route("/api/easy-setup/template.csv")
def api_easy_setup_template():
    """The exact columns the bulk paste accepts, with example rows."""
    return Response(
        ops.bulk_template_csv(),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=creators-template.csv"},
    )


@app.route("/api/digest")
def api_digest():
    """Operational summary (read-only). ``?hours=`` sets the window."""
    try:
        hours = max(1, min(24 * 30, request.args.get("hours", 24, type=int) or 24))
    except (TypeError, ValueError):
        hours = 24
    try:
        data = ops.digest(hours=hours)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    data["ok"] = True
    data["card"] = ops.render_digest(data)
    return jsonify(data)


@app.route("/api/inventory.csv")
def api_inventory_csv():
    """Everything about every creator — one click, one spreadsheets.

    Admin-only by the same route guard as the rest of the console: this file has
    phone numbers, tags and settings, so it is never something the add-only team
    session can pull.
    """
    member = (request.args.get("member") or "").strip()
    try:
        body = inventory.csv_text(member=member)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    filename = "creator-inventory.csv" if not member else "creator-inventory-team.csv"
    security.audit(_session_actor(), _session_role(), "inventory_exported",
                   target=member or "all", detail=f"{len(body.splitlines()) - 1} row(s)",
                   ip=_client_ip())
    return Response(
        body,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/setup/inventory/import", methods=["POST"])
def setup_inventory_import():
    """Apply an edited inventory file: preview first, then one click to apply."""
    actor = _session_actor()
    raw = request.form.get("inventory_lines", "")
    upload = request.files.get("inventory_file")
    if upload and upload.filename:
        try:
            raw = upload.stream.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # pragma: no cover - unreadable upload
            flash(f"⚠ Could not read the uploaded file: {exc}", "error")
            return redirect(url_for("setup") + "#inventory")
    apply_now = str(request.form.get("apply", "")).strip().lower() in {"1", "on", "yes"}
    if not str(raw or "").strip() and apply_now:
        # "Apply the changes" only sends the button: the file being applied is
        # the one the preview above was built from, read back from its stash.
        raw = inventory.stash_read(str(session.get("inventory_plan_token") or ""))
        if not str(raw or "").strip():
            flash("That preview has expired — paste the file again and press Preview.",
                  "warning")
            return redirect(url_for("setup") + "#inventory")
    if not str(raw or "").strip():
        flash("Paste the inventory file (or choose a CSV) and try again.", "warning")
        return redirect(url_for("setup") + "#inventory")

    marked = str(request.form.get("create_missing", "")).strip().lower() in {"1", "on", "yes"}
    create_missing = marked or (apply_now and bool(session.get("inventory_plan_create")))
    try:
        plan = inventory.plan_import(raw, create=create_missing)
    except Exception as exc:
        flash(f"⚠ That file could not be read: {exc}", "error")
        return redirect(url_for("setup") + "#inventory")

    if not apply_now:
        # A 1000-creator file is ~250 KB: far past what a browser keeps in a
        # cookie, so the file itself stays in a private server-side stash and
        # only its token travels in the session.
        token = inventory.stash_write(str(raw))
        if not token:
            flash("⚠ That file is too large to preview (limit 8 MB). Split it and try again.",
                  "error")
            return redirect(url_for("setup") + "#inventory")
        session["inventory_plan_token"] = token
        session["inventory_plan_create"] = bool(create_missing)
        session["inventory_plan_preview"] = {
            "changed": plan["changed"], "created": plan["created"],
            "unchanged": plan["unchanged"], "issues": plan["issues"][:20],
        }
        flash(f"🔎 Preview only — {plan['changed']} row(s) would change"
              + (f", {plan['created']} would be created" if plan["created"] else "")
              + (f", {len(plan['issues'])} row(s) cannot be applied" if plan["issues"] else "")
              + ". Press “Apply the changes” to write them.", "success")
        return redirect(url_for("setup") + "#inventory")

    if not plan["changed"]:
        flash("Nothing to do: the file matches the hub exactly (no row changed).", "success")
        return redirect(url_for("setup") + "#inventory")

    report = inventory.apply_import(plan, actor=actor, ip=_client_ip())
    # The rows that really happened first: a 1000-row file must never bury the
    # one edit the owner made under sixty "unchanged" lines.
    interesting = [row for row in report["results"] if row["status"] != "unchanged"]
    session["inventory_last_report"] = (interesting
                                       + [row for row in report["results"]
                                          if row["status"] == "unchanged"])[:60]
    session.pop("inventory_plan_token", None)
    session.pop("inventory_plan_preview", None)
    security.audit(actor, _session_role(), "inventory_imported",
                   detail=f"{report['updated']} updated, {report['created']} created, "
                          f"{report['failed']} failed, {report['unchanged']} unchanged",
                   ip=_client_ip())
    flash("✅ " + inventory.render_report(report)
          + (f" · {len(plan['issues'])} row(s) skipped" if plan["issues"] else ""),
          "success" if not report["failed"] else "warning")
    return redirect(url_for("setup") + "#inventory")


@app.route("/api/assignments")
def api_assignments():
    """The money map: who owns which tag / channel, and what is mixed (read-only)."""
    try:
        findings = guard.scan()
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    return jsonify({
        "ok": guard.is_clear(findings),
        "counts": guard.counts(findings),
        "summary": guard.summary_line(findings),
        "findings": findings,
    })


@app.route("/setup/add-defaults", methods=["POST"])
def save_add_defaults():
    """Save what every NEW creator starts with (the team never has to decide)."""
    updates = {}
    for key in presets.FLAGS:
        if key == "mirror_whatsapp":
            # A checkbox: present in the form means on, absent means off.
            updates[key] = str(request.form.get(key, "")).strip().lower() in {"1", "on", "yes"}
        else:
            updates[key] = str(request.form.get(key, "")).strip().lower() in {"1", "on", "yes"}
    for key in presets.TEXTS:
        if key in request.form:
            updates[key] = request.form.get(key, "")
    try:
        presets.save(updates, actor=_session_actor(), ip=_client_ip())
    except Exception as exc:  # pragma: no cover - storage problem
        flash(f"⚠ Could not save the new-creator defaults: {exc}", "error")
        return redirect(url_for("setup") + "#add-defaults")
    flash("✅ New-creator defaults saved: " + presets.summary_line(), "success")
    return redirect(url_for("setup") + "#add-defaults")


@app.route("/team/preview", methods=["POST"])
def team_preview():
    """Read-only: what the hub understood from a paste, before anything is added.

    The team uses it to check their lines; it writes nothing, needs no unlock and
    is the same parser the add itself uses, so the preview cannot lie.
    """
    text = request.form.get("bulk_lines", "") or request.form.get("inventory_lines", "")
    try:
        result = creators.preview(text, limit=100)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 400
    result["ok"] = True
    result["defaults"] = presets.summary_line()
    return jsonify(result)


@app.route("/api/amazon-report")
def api_amazon_report():
    """Amazon attribution in the posts we really sent (read-only)."""
    try:
        hours = max(1, min(24 * 30, request.args.get("hours", 24, type=int) or 24))
    except (TypeError, ValueError):
        hours = 24
    try:
        data = amazon_watch.attribution_report(hours=hours)
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    data["ok"] = bool(data.get("ok"))
    data["card"] = amazon_watch.render_report(data)
    return jsonify(data)


@app.route("/api/amazon-scan")
def api_amazon_scan():
    """What a source post carries and what each creator would post (read-only).

    ``?text=`` scans the source post, ``?tag=`` renders one creator's version,
    ``?fleet=1`` checks every creator; nothing is written and no message is sent.
    """
    text = (request.args.get("text") or "")[:4000]
    if not text:
        return jsonify({"ok": False, "error": "pass ?text=<source post>"}), 400
    try:
        payload = {"ok": True, "scan": amazon_watch.scan(text)}
        tag = (request.args.get("tag") or "").strip()
        creator_id = request.args.get("creator", type=int)
        if creator_id:
            inf = db.get_influencer(creator_id)
            if not inf:
                return jsonify({"ok": False, "error": "unknown creator"}), 404
            tag = inf.get("amazon_tag") or ""
            payload["creator"] = inf.get("name")
        if tag:
            payload["plan"] = amazon_watch.plan(text, tag=tag)
        if str(request.args.get("fleet", "")).strip() in {"1", "true", "yes"}:
            payload["fleet"] = amazon_watch.fleet_plan(
                text, db.list_influencers(),
                limit=min(2000, max(1, request.args.get("limit", 1000, type=int) or 1000)),
            )
    except Exception as exc:  # pragma: no cover - defensive
        return jsonify({"ok": False, "error": str(exc)[:200]}), 500
    return jsonify(payload)


@app.route("/api/posts.csv")
def api_posts_csv():
    """Every delivery attempt in the window — the audit log behind the counts."""
    try:
        hours = max(1, min(24 * 30, request.args.get("hours", 24, type=int) or 24))
    except (TypeError, ValueError):
        hours = 24
    csv_text = ops.posts_csv(hours=hours)
    return Response(
        csv_text,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename=posts-last-{hours}h.csv"},
    )


@app.route("/api/metrics")
def api_metrics():
    """The digest as Prometheus text — point any scraper at this."""
    try:
        body = ops.metrics_text()
    except Exception as exc:  # pragma: no cover - defensive
        return Response(f"# metrics unavailable: {exc}\n", content_type="text/plain; charset=utf-8", status=500)
    return Response(body, content_type="text/plain; charset=utf-8")


@app.route("/setup")
def setup():
    """Dedicated protected setup center for profiles, routes, and operations."""
    q = request.args.get("q", "").strip()
    dir_offset = max(0, request.args.get("dir_offset", 0, type=int) or 0)
    dir_sort = (request.args.get("sort") or "").strip().lower()
    dir_since = (request.args.get("since") or "").strip()
    dir_until = (request.args.get("until") or "").strip()
    influencers = db.search_influencers(q) if q else db.list_influencers()
    influencers = _filter_by_setup_age(influencers, since=dir_since, until=dir_until)
    influencers = _sort_by_setup_age(influencers, dir_sort)
    settings = db.get_all_global_settings()
    sources = db.list_sources(active_only=False)
    current_ek_key = settings.get("earnkaro_api_key") or config.EARNKARO_API_KEY or ""
    current_ek_pubid = settings.get("earnkaro_publisher_id") or _effective_earnkaro_publisher_id()
    current_hypd_store = settings.get("hypd_store_id") or _effective_hypd_store_id()
    # Advanced source diagnostics: last 24h posting stats + per-source activity
    try:
        source_activity = db.list_source_activity()
    except Exception:
        source_activity = []
    try:
        recent_posts = db.list_recent_posts(hours=24, limit=500)
        # Aggregate skip reasons
        from collections import Counter
        skip_counter: Counter = Counter()
        for post in recent_posts:
            status = str(post.get("status") or "")
            if status.startswith("skipped"):
                skip_counter[status] += 1
        skip_reasons = [{"status": k, "count": v} for k, v in skip_counter.most_common(8)]
    except Exception:
        recent_posts, skip_reasons = [], []
    try:
        channel_activity = db.channel_post_activity(hours=24)
    except Exception:
        channel_activity = []
    return render_template(
        "setup.html",
        influencers=influencers,
        settings=settings,
        sources=sources,
        readiness=_setup_readiness_snapshot(),
        creator_plan=_creators_plan(
            influencers, query=q, limit=DIRECTORY_PAGE_SIZE, offset=dir_offset,
        ),
        directory_page_size=DIRECTORY_PAGE_SIZE,
        directory_offset=dir_offset,
        directory_shown=[inf["id"] for inf in influencers[dir_offset:dir_offset + DIRECTORY_PAGE_SIZE]],
        search_query=q,
        directory_sort=dir_sort,
        directory_since=dir_since,
        directory_until=dir_until,
        age_summary=age.summarise(influencers),
        creator_age={int(inf["id"]): age.human_age(inf.get("created_at"))
                     for inf in influencers},
        current_ek_key=current_ek_key,
        current_ek_pubid=current_ek_pubid,
        current_hypd_store=current_hypd_store,
        short_links=amazon_shortlinks.short_link_status(),
        default_amazon_tag=config.AMAZON_ASSOCIATE_TAG,
        sources_added=request.args.get("sources_added", type=int),
        source_saved=request.args.get("source_saved", type=int),
        source_updated=request.args.get("source_updated", type=int),
        source_deleted=request.args.get("source_deleted", type=int),
        source_error=request.args.get("source_error", ""),
        imported=request.args.get("imported", type=int),
        import_error=request.args.get("import_error", ""),
        team_access=_team_access_snapshot(),
        inventory_card=_inventory_snapshot(),
        assignments=_assignments_snapshot(),
        tag_proof_card=_tag_proof_snapshot(),
        source_default=_default_source_state(sources),
        source_usage=_source_usage(influencers),
        source_rows=_source_rows(),
        source_activity=source_activity,
        skip_reasons=skip_reasons,
        channel_activity=channel_activity,
    )


# "2 months ago" reads wrong for a creator set up today, so every template says
# the age through this one helper rather than gluing "ago" on by hand.
app.jinja_env.globals["age_phrase"] = age.phrase


def _filter_by_setup_age(influencers: list[dict], *, since: str = "",
                         until: str = "") -> list[dict]:
    """Narrow a creator list by when it was set up (``YYYY-MM-DD``, inclusive).

    A creator whose stored date cannot be read is kept — the point of the filter
    is to find people, never to hide them.
    """
    def keep(profile: dict) -> bool:
        day = str(profile.get("created_at") or "")[:10]
        if not day:
            return True
        if since and day < since:
            return False
        if until and day > until:
            return False
        return True

    return [profile for profile in influencers if keep(profile)] if (since or until) else influencers


def _sort_by_setup_age(influencers: list[dict], sort: str = "") -> list[dict]:
    """Order the directory: what was set up when, and what is still not ready."""
    key = (sort or "").strip().lower()
    if key in {"name", "name_desc"}:
        return sorted(influencers, key=lambda inf: str(inf.get("name") or "").lower(),
                      reverse=key.endswith("_desc"))
    if key == "newest":
        return sorted(influencers, key=lambda inf: str(inf.get("created_at") or ""),
                      reverse=True)
    if key == "oldest":
        return sorted(influencers, key=lambda inf: str(inf.get("created_at") or ""))
    return influencers


def _default_source_state(configured: list[dict] | None = None) -> dict:
    """The hub's default deal source, and whether the pool already has it."""
    spec = sources.DEFAULT_SPEC
    token = sources.default_token()
    rows = configured if configured is not None else db.list_sources(active_only=False)
    have = [row for row in (rows or [])
            if str(row.get("spec") or "").strip() == spec]
    return {
        "name": sources.DEFAULT_NAME or token,
        "label": sources.DEFAULT_NAME or token or spec,
        "spec": spec,
        "token": token,
        "configured": bool(have),
        "active": bool(have and have[0].get("active")),
        "source_id": int(have[0]["id"]) if have else 0,
    }


def _source_rows() -> list[dict]:
    """The picker rows: every configured source plus the tokens already in use.

    Built at module level because inside ``setup()`` the name *sources* is the
    configured pool, which would shadow the module that knows the names.
    """
    try:
        return sources.choice_rows()
    except Exception:  # pragma: no cover - display only
        return []


def _source_selection_from_form(*, use_default_when_empty: bool = False) -> str | None:
    """Read the shared source picker: tick boxes, the paste box, or "every source".

    Returns ``None`` when the form carried none of those fields at all, so a
    partial form (a rename, a flag change) never rewrites a creator's sources.
    """
    present = {"sources", "sources_text", "sources_all", "allowed_sources"}.intersection(
        request.form.keys())
    if not present:
        return None
    if _form_flag("sources_all", default=False):
        return ""      # the explicit "every source in the hub"
    picked = [token.strip() for token in request.form.getlist("sources") if token.strip()]
    typed = str(request.form.get("sources_text") or "").strip()
    raw = ", ".join(picked + ([typed] if typed else [])) \
        or str(request.form.get("allowed_sources") or "").strip()
    if not raw:
        return sources.default_token() if use_default_when_empty else ""
    return sources.normalize_selection(raw)


def _source_usage(profiles=None) -> dict:
    """How the fleet reads deal sources today, for the setup page.

    A module-level helper on purpose: inside ``setup()`` the name *sources* is the
    configured pool, which would shadow the module.
    """
    try:
        return sources.usage(profiles)
    except Exception:  # pragma: no cover - display only
        return {}


def _tag_proof_snapshot() -> dict:
    """The setup card: whose Amazon tag signs which active channel, today.

    One batched read (the fleet pass), so a 1000-creator hub renders this card
    without a query per creator. The blocked list is what the owner acts on:
    without an own tag, that creator's Amazon deals are held, not signed with
    the shared id.
    """
    try:
        fleet = tag_proof.fleet()
    except Exception as exc:  # pragma: no cover - display only
        return {"error": str(exc)[:200], "ok": False, "blocked": [], "channels": 0,
                "own": 0, "fallback": 0, "broken_links": [], "summary": "",
                "fix": "", "hold_reason": tag_proof.HOLD_REASON,
                "require_own_tag": bool(getattr(config, "REQUIRE_OWN_AMAZON_TAG", True)),
                "samples": []}
    samples: list[dict] = []
    for row in (fleet.get("blocked") or [])[:6]:
        samples.append({
            "creator_id": row["creator_id"],
            "name": row["name"],
            "channel": (row.get("channels") or [{}])[0].get("identifier", ""),
            # The link their posts would carry today (the shared id, i.e. our
            # money, not theirs) and the shape of the link they get once the tag
            # is saved.
            "would_post": tag_proof.url_summary(
                f"https://www.amazon.in/dp/{tag_proof.SAMPLE_ASIN}?tag={row.get('fallback_tag') or ''}"
            ),
        })
    return {
        **fleet,
        "fix": tag_proof.describe_fix(fleet.get("blocked") or []),
        "hold_reason": tag_proof.HOLD_REASON,
        "samples": samples,
        "shared_default_tag": link_router.effective_amazon_tag(""),
    }


@app.route("/api/tag-proof")
def api_tag_proof():
    """Whose Amazon tag signs which active channel — the owner's money check.

    Read-only JSON, admin only. ``?creator=<id>`` returns the single-creator
    proof: the channel list, the tag that will sign each one, and the exact link
    that creator's posts will carry.
    """
    inf_id = request.args.get("creator", type=int)
    if inf_id:
        profile = db.get_influencer(inf_id)
        if not profile:
            return jsonify({"ok": False, "error": "creator_not_found"}), 404
        channels = db.list_channels(inf_id)
        rows = []
        for channel in channels:
            info = tag_proof.signing(profile, channel)
            rows.append({
                "channel_id": info["channel_id"],
                "platform": str(channel.get("platform") or ""),
                "identifier": str(channel.get("identifier") or ""),
                "role": str(channel.get("role") or ""),
                "status": str(channel.get("status") or ""),
                "tag": info["tag"],
                "own": info["own"],
                # Where the value came from (channel override / creator row /
                # hub-wide default) and whether it *is* the hub-wide default —
                # the two together are why a channel is held.
                "source": info["source"],
                "shared_default": info["shared_default"],
                "usable": info["usable"],
                "hold_reason": info["reason"],
            })
        link = tag_proof.tagged_link(profile)
        proof = tag_proof.verify(link, profile) if link else {"ok": False, "issues": []}
        return jsonify({
            "ok": bool(link) and proof["ok"],
            "creator_id": inf_id,
            "name": str(profile.get("name") or ""),
            "own_tag": tag_proof.own_tag(profile),
            "link": link,
            "link_summary": tag_proof.url_summary(link) if link else "",
            "verify": proof,
            "channels": rows,
            "hold_reason": tag_proof.HOLD_REASON,
        })
    snapshot = _tag_proof_snapshot()
    return jsonify({
        "ok": bool(snapshot.get("ok")),
        "counts": {"creators": snapshot.get("creators", 0),
                   "channels": snapshot.get("channels", 0),
                   "own": snapshot.get("own", 0),
                   "fallback": snapshot.get("fallback", 0)},
        "summary": snapshot.get("summary", ""),
        "fix": snapshot.get("fix", ""),
        "blocked": snapshot.get("blocked", []),
        "broken_links": snapshot.get("broken_links", []),
        "hold_reason": tag_proof.HOLD_REASON,
        "require_own_tag": snapshot.get("require_own_tag", True),
    })


def _assignments_snapshot() -> dict:
    """The setup card's money map: findings + the add defaults, in one read."""
    try:
        findings = guard.scan()
    except Exception as exc:  # pragma: no cover - display only
        return {"error": str(exc)[:200], "findings": [], "counts": guard.counts([]),
                "summary": "", "defaults": [], "defaults_line": ""}
    return {
        "findings": findings[:40],
        "counts": guard.counts(findings),
        "summary": guard.summary_line(findings),
        "clear": guard.is_clear(findings),
        "defaults": presets.describe(),
        "defaults_line": presets.summary_line(),
        "defaults_values": presets.resolved(),
        "creators": len(db.list_influencers()),
    }


def _inventory_snapshot() -> dict:
    """The export/import card on /setup: what would change, and the last report."""
    preview = None
    plan_text = inventory.stash_read(str(session.get("inventory_plan_token") or ""))
    if session.get("inventory_plan_token") and not plan_text:
        preview = {"expired": True}
    if plan_text:
        try:
            plan = inventory.plan_import(plan_text,
                                         create=bool(session.get("inventory_plan_create")))
            preview = {
                "changed": plan["changed"], "unchanged": plan["unchanged"],
                "created": plan["created"], "issues": plan["issues"][:20],
                "operations": [{
                    "line": op["line"], "name": op["name"],
                    "detail": ", ".join(sorted(list(op["changes"]) + list(op["channels"]))),
                    "create": op["create"],
                    "edits": inventory.edits(op),
                } for op in plan["operations"] if op["changes"] or op["channels"] or op["create"]][:40],
            }
        except Exception as exc:  # pragma: no cover - display only
            preview = {"error": str(exc)[:200]}
    try:
        team_counts = {
            member: len(db.list_influencers(added_by=member))
            for member in {str(row.get("actor") or "") for row in db.list_audit(limit=200)
                           if row.get("role") == security.TEAM and row.get("action") == "creator_added"}
            if member
        }
        total = len(db.list_influencers())
    except Exception:  # pragma: no cover - display only
        team_counts, total = {}, 0
    return {
        "columns": inventory.COLUMNS,
        "editable": sorted(inventory.EDITABLE),
        "preview": preview,
        "last_report": session.get("inventory_last_report"),
        "total": total,
        "team_counts": dict(sorted(team_counts.items())),
        "team_added": sum(team_counts.values()),
    }


def _team_access_snapshot() -> dict:
    """Who may do what, who added what, and the last lines of the audit log."""
    try:
        audit_rows = db.list_audit(limit=40)
    except Exception:  # pragma: no cover - the page must still render
        audit_rows = []
    members: dict[str, dict] = {}
    for row in audit_rows:
        if row.get("role") != security.TEAM:
            continue
        member = str(row.get("actor") or "team")
        entry = members.setdefault(member, {"member": member, "adds": 0, "last_seen": row.get("at")})
        if row.get("action") == "creator_added":
            entry["adds"] += 1
    team_added = 0
    try:
        conn = db._connect()
        try:
            team_added = int(conn.execute(
                "SELECT COUNT(*) AS n FROM influencers WHERE TRIM(COALESCE(added_by,'')) <> ''"
            ).fetchone()["n"])
        finally:
            conn.close()
    except Exception:  # pragma: no cover - display only
        team_added = 0
    return {
        "team_configured": security.team_access_configured(),
        "totp_enabled": security.totp_enabled(),
        "team_url": url_for("team_login", _external=True),
        "team_added_creators": team_added,
        "members": sorted(members.values(), key=lambda item: item["member"].lower()),
        "audit": audit_rows,
        "limit_per_hour": config.TEAM_ADDS_PER_HOUR,
        "admin_password_strong": security.strong_password(config.DASHBOARD_ADMIN_PASSWORD, minimum=16),
    }


# ---------- Money Radar: where commission leaks out of posted deals ----------
# Each switch is a global setting; the pipeline reads it on every render.
MONEY_SWITCHES: dict[str, tuple[str, str]] = {
    "strict_commission_mode": (
        "Strict attribution lock (recommended)",
        "Default ON. Hold every post with an unverified, mixed, unknown-store, or unresolved short link; retry with backoff and never force-post raw links after retries. This can reduce posts and cannot guarantee a network payout.",
    ),
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
    "allow_unconverted_posts": (
        "Post unconverted links anyway",
        "Legacy volume option: post a raw merchant link immediately when strict "
        "attribution lock is disabled. The default-on strict lock takes priority "
        "and prevents this switch from publishing unverified links.",
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


DIRECTORY_PAGE_SIZE = 60


def _creators_plan(influencers: list[dict], *, query: str = "", state: str = "",
                   limit: int = 0, offset: int = 0) -> dict | None:
    """Readiness for a listed set of creators (Setup Center directory).

    Never raises: a checklist is a convenience, and the Setup Center must keep
    working on a database that has not been migrated or is being written to.
    """
    if not influencers:
        return None
    try:
        return readiness.overview(
            influencers=influencers, query=query, state=state,
            limit=limit, offset=offset,
        )
    except Exception as exc:  # pragma: no cover - defensive
        app.logger.warning("creator readiness failed: %s", exc)
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
        "allow_unconverted_posts": bool(config.ALLOW_UNCONVERTED_POSTS),
        "strict_commission_mode": bool(config.STRICT_COMMISSION_MODE),
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

    try:
        data["commission_holds"] = db.list_deferred_deals(limit=50)
        data["commission_hold_count"] = db.count_deferred_deals()
    except Exception:
        app.logger.exception("could not load pending commission holds")
        data["commission_holds"] = []
        data["commission_hold_count"] = 0

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
                configured_sources = int(source_report.get("configured_sources", 0) or 0)
                selected_sources = int(source_report.get("selected_sources", 0) or 0)
                selection_mode = str(source_report.get("selection_mode") or "")
                source_state = (
                    "no_sources" if not configured_sources else
                    "fallback" if selected_sources and selection_mode == "joined_dialog_fallback" else
                    "matched" if selected_sources else
                    "no_matches"
                )
                details = (
                    f"{selected_sources} joined dialog(s) selected from "
                    f"{configured_sources} production selector(s); "
                    f"{source_report.get('joined_group_channels', 0)} joined group/channel dialog(s) were visible."
                )
                if selection_mode == "joined_dialog_fallback":
                    details += (
                        f" {source_report.get('unresolved_private_invites', 0)} private invite label(s) "
                        "did not match an exact joined-dialog title; the worker falls back to the "
                        "already-joined eligible dialog allowlist, with output channels excluded."
                    )
                    if not selected_sources:
                        details += " No eligible joined dialogs were found, so there is nothing to read."
                elif source_state == "no_sources":
                    details += " No production source selectors are configured; add a source to avoid broad joined-dialog ingestion."
                elif source_state == "no_matches":
                    details += " Join the configured source with the Telegram account, or verify its public username / exact private-dialog title."
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


@app.route("/sources/ensure-default", methods=["POST"])
def ensure_default_source():
    """Add the hub's default deal source (LootZoneIndia11) to the pool.

    The default is what a creator falls back to when nothing was selected, so the
    worker must actually be able to read it. Idempotent: pressing it twice changes
    nothing.
    """
    result = sources.ensure_default()
    security.audit(_session_actor(), _session_role(), "default_source_ensured",
                   target=result.get("name") or "", detail=result.get("spec") or "",
                   ip=_client_ip())
    flash(f"{'✅ Added' if result['added'] else 'Already configured'}: "
          f"{result['name']} ({result['spec']})", "success")
    return redirect(url_for("setup", source_saved=1))


@app.route("/sources/apply-default", methods=["POST"])
def apply_default_source():
    """Switch creators that were saved with **no** source to the hub default.

    "Empty allowed_sources" has always meant "every source in the hub", and the
    live hub has creators saved that way. The operator's rule is that nothing
    chosen means the default, so the change is one explicit click — never
    implicit — and it only touches rows that are still blank.
    """
    result = sources.apply_default()
    security.audit(_session_actor(), _session_role(), "default_source_applied",
                   target=result.get("token") or "", detail=str(result.get("updated") or 0),
                   ip=_client_ip())
    if result.get("updated"):
        names = ", ".join(name for name in result["names"] if name)
        flash(f"✅ {result['updated']} creator(s) with no source now post from "
              f"{result['token']}{' (' + names + '…)' if names else ''}", "success")
    else:
        flash("Nothing to change: every creator already selects a source, or asks "
              "for every source explicitly.", "success")
    return redirect(url_for("setup", source_saved=1))


@app.route("/sources/add", methods=["POST"])
def add_deal_source():
    name = request.form.get("name", "").strip()
    spec = request.form.get("spec", "").strip()
    kind = request.form.get("kind", "production").strip().lower()
    from_vault = request.form.get("from_vault") == "1"
    from_setup = request.form.get("from_setup") == "1"
    if kind not in {"production", "dummy"}:
        kind = "production"
    if not spec or len(spec) > 512:
        return redirect(url_for("setup", source_error="invalid_spec"))
    _, _, is_private_invite = puller._source_parts(spec)
    if not name:
        # Invite hashes are secrets/identifiers, not useful display names. A
        # generic label is fine because private invites are resolved only by
        # an exact joined-dialog title; otherwise puller.py uses safe fallback.
        name = "Private Telegram source" if is_private_invite else spec.split("/")[-1].replace("+", "").replace("@", "")
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


def _parse_bulk_source_line(line: str) -> tuple[str, str] | None:
    """One line → (name, spec) for bulk paste.

    Supports:
      • https://t.me/powerloot
      • https://t.me/+AbC123 | My Private Source
      • My Label | @username
      • @username
      •  -1001234567890
    Separator candidates: '|' , ' - ', ' :: ', tab. Comma-separated specs
    on one line are handled by the caller.
    """
    raw = str(line or "").strip()
    if not raw:
        return None
    # Skip comment lines
    if raw.startswith("#"):
        return None
    name = ""
    spec = raw
    # Try explicit separators in priority order
    for sep in (" | ", "|", " :: ", "::", " - ", "\t"):
        if sep in raw:
            left, right = [part.strip() for part in raw.split(sep, 1)]
            if not left or not right:
                continue
            # Heuristic: which side looks like a Telegram selector?
            def _looks_like_spec(value: str) -> bool:
                low = value.lower()
                return (
                    "t.me/" in low
                    or "telegram.me/" in low
                    or value.startswith("@")
                    or value.startswith("+")
                    or value.lstrip("-").isdigit()
                    or value.startswith("https://")
                    or value.startswith("http://")
                )
            if _looks_like_spec(left) and not _looks_like_spec(right):
                spec, name = left, right
            elif _looks_like_spec(right) and not _looks_like_spec(left):
                name, spec = left, right
            else:
                # If both look like specs or neither, treat whole line as spec
                # (e.g. a single URL without label)
                if sep == "|" and "," not in raw:
                    # For single '|' we prefer left=spec right=name fallback
                    spec, name = left, right
                else:
                    continue
            break
    spec = spec.strip()
    name = name.strip()[:120]
    if not spec or len(spec) > 512:
        return None
    # Reject obviously non-Telegram garbage (no @, no t.me, no +, not numeric)
    low = spec.lower()
    if not (
        "t.me/" in low
        or "telegram.me/" in low
        or spec.startswith("@")
        or spec.startswith("+")
        or spec.lstrip("-").isdigit()
        or low.startswith("https://")
        or low.startswith("http://")
        or spec.lstrip("@").replace("_", "").replace("-", "").isalnum()
    ):
        # Still allow bare usernames without @
        if not spec.replace("_", "").replace("-", "").isalnum():
            return None
    if not name:
        try:
            _, _, is_private = puller._source_parts(spec)
        except Exception:
            is_private = False
        if is_private:
            name = "Private Telegram source"
        else:
            # Derive a readable label from spec
            base = spec.split("/")[-1].replace("+", "").replace("@", "").strip()
            name = base[:120] if base else spec[:120]
    return name, spec


@app.route("/sources/bulk-add", methods=["POST"])
def bulk_add_sources():
    """Paste many channel links at once — each line is one source.

    Handles t.me URLs, @usernames, private invite hashes, numeric IDs and
    optional friendly labels separated by '|' . Duplicate specs are skipped.
    """
    raw = str(request.form.get("bulk_specs") or "").strip()
    kind = str(request.form.get("kind") or "production").strip().lower()
    if kind not in {"production", "dummy"}:
        kind = "production"
    if not raw:
        flash("Paste at least one Telegram link or username.", "warning")
        return redirect(url_for("setup"))
    existing = {str(row.get("spec") or "").strip().casefold(): row for row in db.list_sources(active_only=False)}
    added = 0
    skipped = 0
    invalid = 0
    # Split by newlines and also handle comma-separated entries per line
    lines: list[str] = []
    for block in raw.splitlines():
        block = block.strip()
        if not block:
            continue
        # If line has multiple specs separated by commas but no label separator, split
        if "|" not in block and " :: " not in block and " - " not in block:
            if "," in block and "t.me" not in block.lower().split(",")[0]:
                # Actually split by comma only when multiple plain tokens
                parts = [part.strip() for part in block.split(",")]
                # Heuristic: if any part contains t.me/http/@ we keep as separate lines
                if any("t.me" in p.lower() or p.startswith("@") or p.startswith("http") for p in parts):
                    lines.extend(parts)
                    continue
        lines.append(block)
    for line in lines:
        # Also allow comma-separated specs inside a line without label
        sub_parts = [line]
        if "|" not in line and "::" not in line and " - " not in line and "," in line:
            # Only split if comma parts individually look like specs
            maybe = [p.strip() for p in line.split(",") if p.strip()]
            if len(maybe) > 1 and all(len(p) <= 512 for p in maybe):
                sub_parts = maybe
        for part in sub_parts:
            parsed = _parse_bulk_source_line(part)
            if not parsed:
                invalid += 1
                continue
            name, spec = parsed
            key = spec.strip().casefold()
            if key in existing:
                skipped += 1
                continue
            try:
                _, _, is_private = puller._source_parts(spec)
            except Exception:
                is_private = False
            if not name:
                name = "Private Telegram source" if is_private else spec.split("/")[-1].replace("+", "").replace("@", "")[:120]
            try:
                db.add_source(name[:120], spec.strip(), kind=kind)
                existing[key] = {"spec": spec.strip()}
                added += 1
            except Exception:
                invalid += 1
    if added:
        security.audit(_session_actor(), _session_role(), "sources_bulk_added",
                       target=str(added), detail=f"skipped:{skipped} invalid:{invalid}", ip=_client_ip())
        flash(f"✅ Added {added} source(s){f' · {skipped} duplicate(s) skipped' if skipped else ''}{f' · {invalid} invalid ignored' if invalid else ''}. Telegram joining is still manual — open Telegram and join them with the same account.", "success")
    else:
        flash(f"No new source added — {skipped} duplicate(s), {invalid} invalid. Check the links and try again.", "warning")
    return redirect(url_for("setup", source_saved=1 if added else 0))


@app.route("/sources/bulk-delete", methods=["POST"])
def bulk_delete_sources():
    ids = request.form.getlist("source_ids")
    # Also handle comma-separated hidden field fallback
    if not ids:
        raw = str(request.form.get("source_ids_raw") or "").strip()
        if raw:
            ids = [part.strip() for part in raw.split(",") if part.strip()]
    deleted = 0
    for sid in ids:
        try:
            source_id = int(str(sid).strip())
            db.delete_source(source_id)
            deleted += 1
        except Exception:
            continue
    if deleted:
        security.audit(_session_actor(), _session_role(), "sources_bulk_deleted",
                       target=str(deleted), detail=",".join(str(i) for i in ids[:10]), ip=_client_ip())
        flash(f"🗑️ Removed {deleted} source(s). Their queued cursors remain until the worker advances.", "success")
    else:
        flash("No source selected.", "warning")
    return redirect(url_for("setup", source_deleted=1 if deleted else 0))


@app.route("/sources/bulk-toggle", methods=["POST"])
def bulk_toggle_sources():
    ids = request.form.getlist("source_ids")
    if not ids:
        raw = str(request.form.get("source_ids_raw") or "").strip()
        if raw:
            ids = [part.strip() for part in raw.split(",") if part.strip()]
    toggled = 0
    for sid in ids:
        try:
            source_id = int(str(sid).strip())
            db.toggle_source(source_id)
            toggled += 1
        except Exception:
            continue
    if toggled:
        flash(f"🔄 Toggled {toggled} source(s) active/paused.", "success")
    return redirect(url_for("setup", source_updated=1 if toggled else 0))


@app.route("/global-settings/update", methods=["POST"])
def update_global_settings():
    # Each card posts only its own fields; a card that is not on the page must
    # never blank another card's settings.
    if {"nav_button_enabled", "nav_button_text", "nav_button_url"} & set(request.form):
        nav_btn_en = "1" if request.form.get("nav_button_enabled") in ("1", "on", "true") else "0"
        nav_btn_text = request.form.get("nav_button_text", "Join Shpsy Loots ❤️").strip()
        nav_btn_url = request.form.get("nav_button_url", "").strip()

        db.set_global_setting("nav_button_enabled", nav_btn_en)
        db.set_global_setting("nav_button_text", nav_btn_text)
        db.set_global_setting("nav_button_url", nav_btn_url)

    if "mirror_main_to_whatsapp" in request.form:
        # Default of the mirror itself: every creator's ready main Telegram
        # channel is copied to their ready WhatsApp destinations. A per-channel
        # switch on the creator page overrides this.
        mirror_on = "1" if request.form.get("mirror_main_to_whatsapp") in ("1", "on", "true") else "0"
        db.set_global_setting("mirror_main_to_whatsapp", mirror_on)

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
    # This form always sends the source field, pre-filled with the hub default: a
    # creator added while nothing is chosen posts from LootZoneIndia11, and an
    # operator who really wants every source ticks the box (which stores the empty
    # value that has always meant "every source"). Callers that do not send the
    # field at all — scripts, older forms — keep that behaviour unchanged.
    picked = _source_selection_from_form(use_default_when_empty=True)
    if picked is None:                      # a caller that sends no source field
        allowed_src = ""
    else:
        allowed_src = picked
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
    whatsapp_name = request.form.get("whatsapp_name", "").strip()[:120]
    # "Main Telegram channel lo vachinavi anni WhatsApp channel lo kuda" — the
    # mirror is a first-class choice on the add form, not a hidden default.
    mirror_main = db._clean_mirror_flag(request.form.get("mirror_whatsapp", "")) or "1"
    strip_amz_all = request.form.get("strip_amazon_broadcast") == "1"

    # Custom button fields on quick add
    btn_enabled = request.form.get("custom_button_enabled") == "1"
    btn_text = request.form.get("custom_button_text", "Join Shpsy Loots ❤️").strip()
    btn_url = request.form.get("custom_button_url", "").strip()
    btn2_text = request.form.get("custom_button2_text", "").strip()
    btn2_url = request.form.get("custom_button2_url", "").strip()
    if not _custom_button_urls_valid(btn_url, btn2_url):
        flash("Post-button links must be HTTPS URLs or Telegram @handles.", "error")
        return redirect(url_for("setup"))

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

    if btn_enabled or btn_url or btn2_url:
        db.update_influencer(
            iid, custom_button_enabled=btn_enabled,
            custom_button_text=btn_text, custom_button_url=btn_url,
            custom_button2_text=btn2_text, custom_button2_url=btn2_url,
        )

    # 1. Approval Channel — Telegram names it for us (read-only peek; never joins)
    add_notes: list[str] = []
    if approval_tg:
        ident = clean_identifier(approval_tg)
        info = _telegram_destination_info(ident)
        title, rights_note = _telegram_name_note(info, ident)
        db.add_channel(iid, "telegram", ident, role="approval", status="ready", title=title,
                       allowed_sources=allowed_src, categories=categories, posting_schedule=schedule,
                       only_amazon=True, allow_amazon=True, allow_earnkaro=False,
                       allow_hypd=False, hypd_store_id=hypd_store_id)
        if rights_note:
            add_notes.append(ident + ":" + rights_note)

    # 2. Broadcast Channel — same smart handling ("nakunna Telegram channel ki kuda")
    if broadcast_tg:
        ident = clean_identifier(broadcast_tg)
        info = _telegram_destination_info(ident)
        title, rights_note = _telegram_name_note(info, ident)
        if rights_note:
            add_notes.append(ident + ":" + rights_note)
        db.add_channel(iid, "telegram", ident, role="broadcast", status="ready", title=title,
                       strip_amazon=strip_amz_all,
                       price_filter=price_filt if price_filt != "all" else "",
                       allowed_sources=allowed_src, bitly_api_key=bitly_key,
                       categories=categories, posting_schedule=schedule,
                       only_amazon=only_amazon, allow_amazon=allow_amazon,
                       allow_earnkaro=allow_earnkaro, allow_hypd=allow_hypd,
                       hypd_store_id=hypd_store_id,
                       mirror_whatsapp=mirror_main)

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
                title=whatsapp_name or _whatsapp_chat_title(iid, destination["identifier"]),
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

    for note in add_notes:
        flash("⚠️ Telegram: " + note, "warning")

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
    # The source picker (tick boxes + paste box + "every source") when present,
    # otherwise the plain field; a form that carries neither keeps the value.
    picked_src = _source_selection_from_form()
    allowed_src = (request.form.get("allowed_sources", "").strip()
                   if picked_src is None else picked_src)
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

    # Creator-scoped smart navigation links each ready Telegram destination to
    # the other ready channels owned by this same influencer.
    smart_channel_nav = request.form.get("smart_channel_nav") == "1"
    btn_enabled = request.form.get("custom_button_enabled") == "1" or smart_channel_nav
    btn_text = request.form.get("custom_button_text", "").strip()
    btn_url = (channel_navigation.SMART_CHANNEL_NAV if smart_channel_nav
               else request.form.get("custom_button_url", "").strip())
    btn2_text = request.form.get("custom_button2_text", "").strip()
    btn2_url = request.form.get("custom_button2_url", "").strip()
    if not _custom_button_urls_valid(
        "" if btn_url == channel_navigation.SMART_CHANNEL_NAV else btn_url,
        btn2_url,
    ):
        flash("Post-button links must be HTTPS URLs or Telegram @handles.", "error")
        return redirect(url_for("influencer_detail", inf_id=inf_id))

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
        custom_button2_text=btn2_text,
        custom_button2_url=btn2_url,
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
    btn2_text = request.form.get("custom_button2_text")
    btn2_url = request.form.get("custom_button2_url")
    if not _custom_button_urls_valid(btn_url, btn2_url):
        flash("Post-button links must be HTTPS URLs or Telegram @handles.", "error")
        return redirect(url_for("influencer_detail", inf_id=owner_id))
    invite_link = None
    needs_test = False

    # Deal quality floor for this channel; empty inherits the creator's value.
    channel_min_tier = request.form.get("min_deal_tier")
    channel_min_tier = channel_min_tier.strip() if channel_min_tier is not None else None

    # The channel's own name, editable by hand and set automatically when the
    # operator picks a chat from the paired phone's list.
    channel_title = request.form.get("title")
    channel_title = channel_title.strip() if channel_title is not None else None

    # Main-channel mirror switch ('' inherit the global setting, 1 on, 0 off).
    # Only the main Telegram channels show this control, so an absent field
    # means "this destination has no mirror switch to change".
    mirror_whatsapp = request.form.get("mirror_whatsapp")
    mirror_whatsapp = mirror_whatsapp.strip() if mirror_whatsapp is not None else None

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
    tag_value, tag_note, tag_blocked = _normalize_amazon_tag(override_tag)
    if tag_blocked:
        flash(tag_note, "error")
        return redirect(url_for("influencer_detail", inf_id=owner_id))
    override_tag = tag_value
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
        custom_button2_text=btn2_text,
        custom_button2_url=btn2_url,
        min_deal_tier=channel_min_tier,
        mirror_whatsapp=mirror_whatsapp,
        title=channel_title,
    )
    if needs_test:
        return redirect(url_for(
            "influencer_detail", inf_id=owner_id, wa_link_status="pending"
        ))
    if tag_note:
        flash("⚠ " + tag_note, "warning")
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


#: The Amazon link the "what gets posted" preview proves the tag against.
ROUTING_AMAZON_SAMPLE = ROUTING_SAMPLES[0][1]

_LIVE_SELECTION_CACHE: dict = {"checked_at": 0.0, "report": None}
LIVE_SELECTION_TTL_SECONDS = 45


def _live_source_selection(ttl: int = LIVE_SELECTION_TTL_SECONDS) -> dict:
    """Which joined dialogs would the worker read right now? Cached, read-only.

    This calls the same inspect_source_selection() the Setup Center uses, so the
    flow board can prove the *real* selection instead of only repeating the
    configured selectors. It never joins or checks an invite, never reads
    message history and never sends anything. Results are cached briefly so
    opening the dashboard cannot hammer Telegram; when the check is unavailable
    the board simply reports the last known state.
    """
    if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
        return {"checked": False, "reason": "telegram_not_configured"}
    if _running_under_pytest():
        # Tests monkeypatch the inspector; a cached mock must not leak onward.
        ttl = 0
    now = time.time()
    cached = _LIVE_SELECTION_CACHE.get("report")
    if cached is not None and ttl > 0 and (
        now - float(_LIVE_SELECTION_CACHE.get("checked_at") or 0)
    ) < ttl:
        return cached
    try:
        report = _run(asyncio.wait_for(
            puller.inspect_source_selection(include_dialog_names=True), timeout=20
        ))
    except Exception as exc:
        if cached is not None:
            return {**cached, "stale": True, "error": type(exc).__name__}
        return {"checked": False, "reason": "inspection_failed", "error": type(exc).__name__}

    result = {
        "checked": True,
        "authorized": bool(report.get("authorized")),
        "selection_mode": str(report.get("selection_mode") or ""),
        "fallback_reason": str(report.get("fallback_reason") or ""),
        "configured_sources": int(report.get("configured_sources", 0) or 0),
        "matched_source_selectors": int(report.get("matched_source_selectors", 0) or 0),
        "joined_group_channels": int(report.get("joined_group_channels", 0) or 0),
        "eligible_joined_dialogs": int(report.get("eligible_joined_dialogs", 0) or 0),
        "selected_sources": int(report.get("selected_sources", 0) or 0),
        "unresolved_private_invites": int(report.get("unresolved_private_invites", 0) or 0),
        "dialogs": list(report.get("selected_dialogs") or []),
        "dialogs_truncated": int(report.get("selected_dialogs_truncated", 0) or 0),
        "stale": False,
        "error": "",
    }
    if ttl > 0:
        _LIVE_SELECTION_CACHE.update({"checked_at": now, "report": result})
    return result


def _wants_live_probe() -> bool:
    """The Telegram selection probe runs unless the caller opts out (?live=0)."""
    value = str(request.args.get("live", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _flow_snapshot(
    stall_seconds: int = 3 * 3600, window_hours: int = 24, live_probe: bool = True
) -> dict:
    """Is posting still flowing from the sources? One honest answer.

    Sources that have produced a deal before but nothing for `stall_seconds` are
    marked stalled, channels show their last successful post, and the worker's
    heartbeat says whether anything is pulling at all. Database reads are local;
    the optional Telegram selection probe is read-only and briefly cached.
    """
    now = time.time()
    note_list: list[str] = []

    worker = db.get_worker_heartbeat()
    worker_age = None
    if worker and worker.get("heartbeat_at"):
        worker_age = max(0.0, now - float(worker["heartbeat_at"]))
    worker_alive = worker_age is not None and worker_age <= max(
        120, 3 * int(getattr(config, "DEAL_WORKER_POLL_INTERVAL", 30) or 30)
    )
    if worker is None:
        note_list.append(
            "The deal worker has never reported in. Posts cannot flow until the "
            "influencer-deal-worker service is running."
        )
    elif not worker_alive:
        note_list.append(
            f"No fresh worker heartbeat ({int(worker_age or 0)}s ago). Check "
            "systemctl status influencer-deal-worker."
        )

    try:
        sources = db.list_sources(active_only=False)
    except Exception:
        sources = []
    active_specs = [
        str(source.get("spec") or "").strip()
        for source in sources
        if source.get("active")
    ]
    activity = {row["source_key"]: row for row in db.list_source_activity()}
    flow_sources: list[dict] = []
    for spec in active_specs:
        row = activity.get(spec) or {}
        last_seen = float(row.get("last_seen_at") or 0)
        delivered = bool(row)
        flow_sources.append({
            "spec": spec,
            "source_name": str(row.get("source_name") or ""),
            "deals_seen": int(row.get("deals_seen") or 0),
            "posts_dispatched": int(row.get("posts_dispatched") or 0),
            "failures": int(row.get("failures") or 0),
            "last_error": str(row.get("last_error") or ""),
            "last_seen_at": last_seen or None,
            "age_seconds": int(now - last_seen) if last_seen else None,
            "never_delivered": not delivered,
            "stalled": delivered and (now - last_seen) > stall_seconds,
        })
    flow_sources.sort(
        key=lambda item: (
            item["never_delivered"],
            item["age_seconds"] if item["age_seconds"] is not None else 0,
        ),
        reverse=True,
    )
    stalled = [row for row in flow_sources if row["stalled"]]
    never = [row for row in flow_sources if row["never_delivered"]]
    if active_specs and never:
        note_list.append(
            f"{len(never)} of {len(active_specs)} active sources have not delivered a "
            "deal yet — they may not be joined by the Telegram account, or the "
            "source is quiet."
        )
    if stalled:
        note_list.append(
            f"{len(stalled)} source(s) delivered before but nothing for over "
            f"{stall_seconds // 3600}h."
        )

    live = _live_source_selection() if live_probe else {
        "checked": False, "reason": "probe_disabled"
    }
    if live.get("checked"):
        if live.get("selection_mode") == "joined_dialog_fallback":
            note_list.append(
                f"Live check: {live.get('unresolved_private_invites', 0)} private invite label(s) did "
                f"not match an exact joined-dialog title, so the worker reads "
                f"{live.get('selected_sources', 0)} eligible already-joined dialog(s) instead. "
                "Invites are never checked or joined."
            )
        if not live.get("selected_sources"):
            note_list.append(
                "Live check: the worker currently has no joined dialog to read. Join the configured "
                "source with the Telegram account or use its exact dialog title."
            )
        if live.get("stale"):
            note_list.append(
                f"Live check is showing the last good result ({live.get('error') or 'inspection error'}); "
                "the Telegram selection probe could not run just now."
            )
    elif live.get("reason") == "inspection_failed":
        note_list.append(
            "Live Telegram selection probe failed; the configured selectors above are the last known state."
        )

    try:
        channels = db.channel_post_activity(hours=window_hours)
    except Exception:
        channels = []
    live_channels = [
        channel for channel in channels
        if str(channel.get("status") or "").strip().lower() in {"ready", "active"}
    ]
    posted_window = sum(int(channel.get("posted_in_window") or 0) for channel in channels)
    failed_total = sum(int(channel.get("failed_total") or 0) for channel in channels)
    try:
        hourly_loot = str(db.get_global_setting("hourly_loot_enabled", "0")).strip().lower() in {
            "1", "true", "yes", "on"
        }
    except Exception:
        hourly_loot = False
    only_earning = bool(getattr(config, "ONLY_EARNING_DEALS", False))
    try:
        deferred_deals = db.count_deferred_deals()
        defer_reasons = db.deferred_reasons()
        defer_recent = [
            {
                "reason": str(row.get("reason") or "unconverted"),
                "source": str(row.get("source") or ""),
                "attempts": int(row.get("attempts") or 0),
                "max_attempts": int(row.get("max_attempts") or 0),
                "next_in": max(0, int(float(row.get("not_before") or 0) - time.time())),
            }
            for row in db.list_deferred_deals(limit=5)
        ]
    except Exception:
        deferred_deals = 0
        defer_reasons = {}
        defer_recent = []
    retry_seconds = 300
    try:
        from influencer_hub import pipeline as _pipeline

        retry_seconds = max(1, int(getattr(_pipeline, "DEFERRED_RETRY_DELAY_SECONDS", 300)))
    except Exception:  # pragma: no cover - the board must still render
        pass
    if deferred_deals:
        note_list.append(
            f"{deferred_deals} deal(s) are waiting for a retry (a link that pays us is "
            f"missing: EarnKaro has not converted the merchant link yet, or a wrapper "
            f"link such as bit.ly could not be resolved). The worker retries every "
            f"{max(1, retry_seconds // 60)} min, then posts them anyway. None of them "
            "was posted for free."
        )
    if live_channels and not posted_window and worker_alive:
        note_list.append(
            f"No post in the last {window_hours}h across {len(live_channels)} ready "
            "channel(s): either the sources are quiet or every deal was filtered."
        )
        if not hourly_loot:
            note_list.append(
                "The hourly loot sweep is off (global setting hourly_loot_enabled=1 "
                "turns on an extra hourly pass over the sources)."
            )

    return {
        "ok": True,
        "generated_at": now,
        "worker": {
            "state": (worker or {}).get("state") or "not_reporting",
            "alive": worker_alive,
            "age_seconds": int(worker_age) if worker_age is not None else None,
            "last_poll_at": (worker or {}).get("last_poll_at") or None,
            "last_error_code": (worker or {}).get("last_error_code") or "",
            "poll_interval_seconds": int(getattr(config, "DEAL_WORKER_POLL_INTERVAL", 30) or 30),
        },
        "sources": {
            "active": len(active_specs),
            "paused": max(0, len(sources) - len(active_specs)),
            "delivered": len([row for row in flow_sources if not row["never_delivered"]]),
            "stalled": len(stalled),
            "never_delivered": len(never),
            "rows": flow_sources,
        },
        "live": live,
        "channels": {
            "ready": len(live_channels),
            "posted_in_window": posted_window,
            "failed_total": failed_total,
            "window_hours": window_hours,
            "rows": channels,
        },
        "settings": {
            "hourly_loot_enabled": hourly_loot,
            "only_earning_deals": only_earning,
        },
        # Deals parked for a conversion retry (nothing posted for free, nothing
        # dropped): the board shows how many are waiting and how long the retry
        # gap is, so "why is the channel quiet?" has a concrete answer.
        "deferred": {
            "waiting": deferred_deals,
            # Why they are parked: "no_own_amazon_tag" is a creator waiting for
            # their own Amazon tag (their Amazon deals stay held, never signed
            # with the shared id), not a broken source.
            "reasons": defer_reasons,
            "recent": defer_recent,
            "retry_seconds": retry_seconds,
            "allow_unconverted_posts": bool(
                getattr(config, "ALLOW_UNCONVERTED_POSTS", False)
            ),
        },
        "notes": note_list,
        "stall_seconds": stall_seconds,
    }


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
    short_link_status = amazon_shortlinks.short_link_status()
    compact_amazon_active = bool(
        amazon_on and channel_role != "approval" and short_link_status["active"]
    )
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
                if channel_role == "approval":
                    row["note"] = (
                        f"Posted with this creator's OWN tag ({tag}) as a native Amazon URL. "
                        "Approval channels deliberately stay native."
                    )
                elif compact_amazon_active:
                    row["note"] = (
                        f"Posted with this creator's OWN tag ({tag}); broadcast/WhatsApp then "
                        f"uses a verified compact {short_link_status['base_url']}/a/<code>?tag={tag} "
                        "that redirects only to this canonical target."
                    )
                else:
                    row["note"] = (
                        f"Posted with this creator's OWN tag ({tag}) as the canonical Amazon URL. "
                        "Set one public HTTPS branded short-link origin to make it compact."
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
    # Offline proof for the exact Amazon link this tag produces, so the screen
    # shows the result instead of only the rule.
    proof = (
        link_router.apply_amazon_tag(ROUTING_AMAZON_SAMPLE, tag)
        if amazon_on else ""
    )
    tag_usable = link_router.amazon_tag_is_usable(tag)
    return {
        "rows": rows,
        "model": model,
        "summary": summary,
        "strict": strict,
        "amazon_on": amazon_on,
        "tag": tag,
        "tag_usable": tag_usable,
        "tag_proof": proof,
        "short_links": {
            **short_link_status,
            "active_for_this_channel": compact_amazon_active,
            "native_for_approval": bool(amazon_on and channel_role == "approval"),
        },
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


def _normalize_amazon_tag(raw: str) -> tuple[str, str, bool]:
    """Validate a hand-typed Amazon tag: ``(tag, message, blocked)``.

    Delegates to the router, which is also what the renderer uses, so the
    dashboard can never accept a value the pipeline would silently replace.
    """
    from influencer_hub import link_router

    validator = getattr(link_router, "normalize_amazon_tag", None)
    if validator is None:  # pragma: no cover - older checkout without the helper
        value = str(raw or "").strip()
        if not value:
            return "", "", False
        import re as _re

        if not _re.fullmatch(r"[A-Za-z0-9_-]{3,30}", value):
            return "", f"“{value}” is not a usable Amazon tag; nothing was saved.", True
        return value, "", False
    return validator(raw)


def _channel_ready_message(inf_id: int, influencer: dict, ident: str, role: str,
                          override_tag: str = "", tag_note: str = "") -> str:
    """Tell the operator exactly which link the new channel will post.

    Proof is generated offline with the same renderer + guard the pipeline uses,
    so "saved" and "will post with my tag" are the same statement.
    """
    from influencer_hub import commission_guard

    tag = (
        override_tag
        or str(influencer.get("amazon_tag") or "").strip()
        or config.AMAZON_ASSOCIATE_TAG
    )
    proof = commission_guard.amazon_tag_proof(tag, compact=(role != "approval"))
    parts = [
        f"✅ {ident} connected to {influencer['name']} as a "
        f"{'approval' if role == 'approval' else 'broadcast'} channel."
    ]
    parts.append(
        "Amazon deals will post as "
        + proof["posted_link"]
        + (" (verified ✅)" if proof["ok"] else " (⚠ not verified)")
    )
    if proof.get("shortened"):
        parts.append(
            "Click proof: this compact URL redirects only to "
            + proof["redirect_target"]
        )
    elif proof.get("short_link_status", {}).get("active") and proof.get("short_link_error"):
        parts.append("⚠ " + proof["short_link_error"] + " Using the canonical tagged URL instead.")
    if proof["fallback_used"]:
        parts.append(
            "⚠ No usable tag is set for this creator/channel, so the configured "
            f"fallback tag {proof['tag']} will sign the links. Add the creator's own tag "
            "on this page to earn from their id."
        )
    if tag_note:
        parts.append("⚠ " + tag_note)
    parts.append("Send a test post below to confirm the account can post there.")
    return " ".join(parts)


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
    only_amz = _form_flag_or_none("only_amazon")
    strip_amz = _form_flag_or_none("strip_amazon")
    allow_amz = _form_flag_or_none("allow_amazon")
    allow_ek = _form_flag_or_none("allow_earnkaro")
    allow_hypd = _form_flag_or_none("allow_hypd")
    hypd_store_id = (
        request.form.get("hypd_store_id", "").strip() or _effective_hypd_store_id()
    )
    # Validate the tag before anything is saved: the renderer only applies
    # [A-Za-z0-9_-]{3,30}, so a typo would otherwise post under the fallback tag.
    override_tag, tag_note, tag_blocked = _normalize_amazon_tag(
        request.form.get("amazon_override_tag", "")
    )
    if tag_blocked:
        return _channel_feedback(inf_id, "error", tag_note)

    channel_settings = {
        "price_filter": price_filt,
        "bitly_api_key": bitly_key,
        "categories": categories,
        "posting_schedule": schedule,
        "hypd_store_id": hypd_store_id,
    }
    if override_tag:
        channel_settings["amazon_override_tag"] = override_tag
    for key, value in (
        ("only_amazon", only_amz),
        ("strip_amazon", strip_amz),
        ("allow_amazon", allow_amz),
        ("allow_earnkaro", allow_ek),
        ("allow_hypd", allow_hypd),
    ):
        if value is not None:
            channel_settings[key] = bool(value)
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
            dupe_info = _telegram_destination_info(ident)
            dupe_title, dupe_note = _telegram_name_note(dupe_info, ident)
            db.update_channel_details(
                duplicate["id"],
                identifier=ident,
                role=role,
                allowed_sources=allowed_src or duplicate.get("allowed_sources") or "",
                wa_session_key=wa_key or duplicate.get("wa_session_key") or "",
                title=dupe_title or None,
                **channel_settings,
            )
            label = dupe_title or ident
            return _channel_feedback(
                inf_id, "warning",
                f"{label} ({ident}) is already connected to {influencer['name']}. "
                "Its settings were updated instead of adding a duplicate." + dupe_note,
            )
        # Ask Telegram for the real name (and our rights there) — read-only.
        info = _telegram_destination_info(ident)
        title, rights_note = _telegram_name_note(info, ident)
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
            title=title,
            **channel_settings,
        )
        message = _channel_ready_message(
            inf_id, influencer, ident, role, override_tag, tag_note)
        if title:
            message = f"{message} Telegram reports this destination as “{title}”."
        if rights_note:
            message += rights_note
        return _channel_feedback(
            inf_id,
            "warning" if (tag_note or rights_note) else "success",
            message,
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
    """The starting point for any add, from the owner's presets (set once).

    ``/setup → 👥 New-creator defaults`` decides these; until they are set, the
    code defaults below are what this function has always returned.
    """
    values = presets.resolved()
    values.update({
        "name": "",
        "amazon_tag": config.AMAZON_ASSOCIATE_TAG,
        "approval": "",
        "main": "",
        "whatsapp": "",
        "hypd_store_id": (str(values.get("hypd_store_id") or "").strip()
                          or _effective_hypd_store_id()),
    })
    return values


def _easy_setup_form_values() -> dict:
    """Read the easy-setup form, defaulting every switch sensibly."""
    values = _easy_setup_defaults()
    values.update({
        "name": request.form.get("name", "").strip(),
        "amazon_tag": request.form.get("amazon_tag", "").strip() or config.AMAZON_ASSOCIATE_TAG,
        "approval": request.form.get("approval_channel", "").strip(),
        "main": request.form.get("main_channel", "").strip(),
        "whatsapp": request.form.get("whatsapp_channel", "").strip(),
        # Absent means "not managed by this form": inherit the recommended
        # default (all three on) instead of silently disabling every network.
        # The screen's switches send an explicit 0/1 pair, so an operator can
        # still switch any of them off.
        # A switch the screen actually sends (an explicit 0/1 pair) wins for this
        # import; a switch the screen does not carry inherits the owner's preset.
        "allow_amazon": bool(_form_flag("allow_amazon", default=bool(values["allow_amazon"]))),
        "allow_earnkaro": bool(_form_flag("allow_earnkaro",
                                          default=bool(values["allow_earnkaro"]))),
        "allow_hypd": bool(_form_flag("allow_hypd", default=bool(values["allow_hypd"]))),
        "only_amazon": bool(_form_flag("only_amazon", default=bool(values["only_amazon"]))),
        "mirror_whatsapp": bool(_form_flag("mirror_whatsapp",
                                           default=bool(values["mirror_whatsapp"]))),
        "hypd_store_id": (
            request.form.get("hypd_store_id", "").strip()
            or str(values.get("hypd_store_id") or "").strip()
            or _effective_hypd_store_id()
        ),
        # The owner's presets travel with the row, so a bulk import also sets the
        # categories / hours / price filter they chose once.
        "categories": str(values.get("categories") or "").strip(),
        "posting_schedule": str(values.get("posting_schedule") or "").strip(),
        "price_filter": str(values.get("price_filter") or "all").strip() or "all",
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


def _guided_owned(profile: dict | None) -> bool:
    """May this session work on that creator?

    The owner may work on anybody. For a team member the wizard *is* their add
    flow: they may finish setting up a creator they created in this very run
    (``guided_fresh``), and nothing else. Coming back later to change a creator
    that is already saved — even one they added — is an edit, and editing stays
    the owner's job (the team page says so, and that is also how
    :func:`team_add` treats a name that already exists).

    The check is server-side and every step goes through it, so a crafted
    ``?creator=`` link or a hand-built form post fails the same way a bookmarked
    admin URL does.
    """
    if not profile:
        return False
    if not _is_team():
        return True
    inf_id = int(profile.get("id") or 0)
    return (str(profile.get("added_by") or "").strip() == _session_actor()
            and int(session.get("guided_fresh") or 0) == inf_id)


def _guided_creator(values: dict) -> dict | None:
    """The creator the wizard is working on.

    Precedence: the creator the submitted form names, then ``?creator=``, then
    the one this session was last working on. A form that names a creator must
    win — with the session taking precedence, two open tabs could silently edit
    the wrong creator.
    """
    inf_id = 0
    if request.method == "POST":
        inf_id = request.form.get("creator", type=int) or 0
    if not inf_id:
        inf_id = request.args.get("creator", type=int) or session.get("guided_creator")
    try:
        inf_id = int(inf_id or 0)
    except (TypeError, ValueError):
        return None
    profile = db.get_influencer(inf_id) if inf_id else None
    if profile and not _guided_owned(profile):
        security.audit(_session_actor(), security.TEAM, "team_blocked_route",
                       target=f"/guided?creator={inf_id}",
                       detail="another member's creator", ip=_client_ip())
        flash("That creator was added by somebody else. Keep setting up your own — "
              "ask the owner to change one that is already saved.", "warning")
        return None
    return profile


def _guided_context(profile: dict | None) -> dict:
    """Everything the wizard page needs, without a second read per template."""
    channels = db.list_channels(int(profile["id"])) if profile else []
    sessions = db.list_wa_sessions(int(profile["id"])) if profile else []
    wa_status = str(sessions[0].get("status") or "not paired") if sessions else "not paired"
    checkpoint = onboarding.checkpoint(profile, channels, wa_status=wa_status) if profile else None
    if checkpoint:
        # "Amazon links will read …?tag=ravi-21" — the exact link the worker will
        # publish for this creator, shown in the review before Go live.
        own_tag = tag_proof.own_tag(profile)
        for line in checkpoint["review"]:
            if line.get("link"):
                line["value"] = (tag_proof.url_summary(tag_proof.tagged_link(profile))
                                 if own_tag else
                                 "their own Amazon tag is missing — Amazon deals stay held")
                line["ok"] = bool(own_tag)
    return {
        "guided_profile": profile,
        "guided_channels": channels,
        # How long this creator has been set up — the owner asked for this line
        # beside every creator, and the guided page is where a creator is read
        # back before it is made live.
        "guided_age": age.setup_label((profile or {}).get("created_at"),
                                      member=str((profile or {}).get("added_by") or "")),
        "guided_wa_key": WA_SESSION_KEY(int(profile["id"])) if profile else "",
        "guided_wa_status": wa_status,
        "guided_checkpoint": checkpoint,
        "guided_progress": onboarding.progress_text(checkpoint) if checkpoint else "",
        "guided_tag_link": tag_proof.url_summary(tag_proof.tagged_link(profile)) if profile else "",
        "guided_steps": onboarding.STEPS,
        "guided_categories": onboarding.CATEGORY_CHOICES,
        # Which deal sources this creator may post from: ticked boxes beside the
        # channels that were just saved, defaulting to LootZoneIndia11.
        "guided_sources": sources.choice_rows(),
        "guided_source_picked": set(
            token.lower() for token in
            sources.selection_tokens((profile or {}).get("allowed_sources") or "")
        ),
        "guided_source_default": sources.default_token(),
        "guided_source_line": sources.selection_line((profile or {}).get("allowed_sources") or ""),
        "guided_source_all": not sources.selection_tokens(
            (profile or {}).get("allowed_sources") or ""),
        "guided_picked": onboarding.selected_categories((profile or {}).get("categories")),
        "batch_defaults": presets.resolved(),
        "batch_defaults_line": presets.summary_line(),
    }


@app.route("/guided")
def guided_setup():
    """The guided setup screen: creator → channels → categories → go live.

    One page, four numbered steps, each one save — open the dashboard, add the
    links you were given, pick the categories, press Go live. Every step works
    without JavaScript (the page simply shows all four cards) and JavaScript only
    moves the highlight along as each step saves.
    """
    profile = _guided_creator({})
    step = (request.args.get("step") or "").strip()
    if profile and not step:
        step = (_guided_context(profile)["guided_checkpoint"] or {}).get("current", "channels")
    return render_template("guided.html", step=step, **_guided_context(profile),
                           **_setup_page_context())


@app.route("/guided/creator", methods=["POST"])
def guided_creator_step():
    """Step 1: the name and their own Amazon tag (or pick an existing creator)."""
    name = request.form.get("name", "").strip()
    tag = request.form.get("amazon_tag", "").strip()
    existing_id = request.form.get("creator", type=int) or 0
    if not name and not existing_id:
        flash("Enter the creator's name (or pick one from the list).", "warning")
        return redirect(url_for("guided_setup"))

    if not existing_id:
        # A team member only ever sees their own additions here: the wizard is
        # an add flow for them, not a way into the rest of the hub.
        known = db.list_influencers(added_by=_session_actor() if _is_team() else "")
        match = next((profile for profile in known
                      if str(profile.get("name") or "").strip().lower() == name.lower()), None)
        if match and _is_team() and not _guided_owned(match):
            flash(f"“{match['name']}” is already in the hub and already saved — ask the owner "
                  "to change it. Add a different creator, or paste everything in one go with "
                  "the one-line add above.", "warning")
            return redirect(url_for("team_home"))
        if match:
            existing_id = int(match["id"])
            flash(f"“{match['name']}” is already in the hub — continuing its setup below.",
                  "success")
    if existing_id:
        profile = db.get_influencer(existing_id)
        if not profile:
            flash("That creator no longer exists.", "warning")
            return redirect(url_for("guided_setup"))
        if not _guided_owned(profile):
            security.audit(_session_actor(), security.TEAM, "team_blocked_route",
                           target=f"/guided/creator#{existing_id}",
                           detail="another member's creator", ip=_client_ip())
            flash("That creator was added by somebody else — only the owner can edit it.",
                  "warning")
            return redirect(url_for("team_home") if _is_team() else url_for("guided_setup"))
        session["guided_creator"] = existing_id
        if tag:
            index = guard.GuardIndex.from_db()
            taken = index.preflight(name=profile.get("name") or name, tag=tag,
                                    ignore_id=existing_id)
            if taken["errors"]:
                flash("⛔ " + index.explain(taken), "error")
                return redirect(url_for("guided_setup", creator=existing_id))
            if taken["warnings"]:
                flash("⚠ " + taken["warnings"][0]["detail"], "warning")
            db.update_influencer(existing_id, amazon_tag=tag)
            security.audit(_session_actor(), _session_role(), "guided_tag_saved",
                           target=profile.get("name"), detail=f"tag {tag}", ip=_client_ip())
        return redirect(url_for("guided_setup", creator=existing_id, step="channels"))

    if not tag:
        flash("This creator's own Amazon Associate tag is required — their deals must earn "
              "under their own id.", "warning")
        return redirect(url_for("guided_setup"))
    index = guard.GuardIndex.from_db()
    taken = index.preflight(name=name, tag=tag)
    if taken["errors"]:
        flash("⛔ " + index.explain(taken), "error")
        return redirect(url_for("guided_setup"))
    inf_id = db.add_influencer(name, tag, added_by=_session_actor() if _is_team() else "",
                               allowed_sources=sources.default_token(),
                               **_add_defaults_kwargs())
    db.set_influencer_active(inf_id, True)
    # A creator's posts need a source the hub can really read: make sure the
    # default one (LootZoneIndia11) is in the pool as soon as it is needed.
    default_source = sources.ensure_default()
    session["guided_creator"] = inf_id
    # For a team member this marks the creator as belonging to this run: they may
    # finish setting it up now, and never edit it later.
    session["guided_fresh"] = inf_id
    security.audit(_session_actor(), _session_role(), "guided_creator_created", target=name,
                   detail=f"tag {tag} · source {sources.selection_line(sources.default_token())}"
                          + (" (default source added to the pool)"
                             if default_source.get("added") else ""),
                   ip=_client_ip())
    for warning in taken["warnings"]:
        flash("⚠ " + warning["detail"], "warning")
    flash(f"✅ {name} created — now add the channels you were given "
          f"(posts from {sources.selection_line(sources.default_token())}).", "success")
    return redirect(url_for("guided_setup", creator=inf_id, step="channels"))


@app.route("/guided/channels", methods=["POST"])
def guided_channels_step():
    """Step 2: the approval / main links and the WhatsApp destination."""
    profile = _guided_creator({})
    if not profile:
        flash("Pick a creator first.", "warning")
        return redirect(url_for("guided_setup"))
    inf_id = int(profile["id"])
    index = guard.GuardIndex.from_db()
    added: list[str] = []
    problems: list[str] = []

    for field, role, label in (("approval_channel", "approval", "Approval (preview) channel"),
                               ("main_channel", "broadcast", "Main channel")):
        raw = request.form.get(field, "").strip()
        if not raw:
            continue
        ident = clean_identifier(raw)
        ok, error = _valid_telegram_identifier(ident)
        if not ok:
            problems.append(f"{label}: {error}")
            continue
        taken = index.preflight(name=profile.get("name") or "", tag="",
                                ignore_id=inf_id, approval=ident if role == "approval" else "",
                                main=ident if role == "broadcast" else "")
        if taken["errors"]:
            problems.append(index.explain(taken))
            continue
        existing = db.get_channel(inf_id, role)
        mirror = "1" if (role == "broadcast" and request.form.get("mirror_whatsapp")) else (
            "0" if role == "broadcast" and request.form.get("mirror_whatsapp_off") else None)
        # Same smart handling as the creator page: Telegram tells us the real
        # name of what was pasted (read-only), and warns if we cannot post there.
        info = _telegram_destination_info(ident)
        title, rights_note = _telegram_name_note(info, ident)
        if existing:
            db.update_channel_details(int(existing["id"]), identifier=ident, status="ready",
                                      title=title or None)
            if mirror is not None:
                db.update_channel_details(int(existing["id"]), mirror_whatsapp=mirror)
        else:
            db.add_channel(inf_id, "telegram", ident, role=role, status="ready", title=title,
                           mirror_whatsapp=mirror if mirror is not None else
                           ("1" if role == "broadcast" and
                            presets.resolved().get("mirror_whatsapp", True) else ""))
        index.note(inf_id, "", name=profile.get("name") or "",
                   channels=[{"platform": "telegram", "identifier": ident, "role": role}])
        added.append(f"{label} {title} ({ident})" if title else f"{label} {ident}")
        if rights_note:
            problems.append(f"{label}:{rights_note}")

    whatsapp = request.form.get("whatsapp_channel", "").strip()
    if whatsapp:
        if _looks_like_whatsapp_jid(whatsapp) or whatsapp.lower().startswith(
                ("http://", "https://", "chat.whatsapp.com", "whatsapp.com")):
            destination, error = _resolve_whatsapp_destination(inf_id, whatsapp)
            if destination:
                taken = index.preflight(name=profile.get("name") or "", tag="",
                                        ignore_id=inf_id,
                                        whatsapp=str(destination.get("identifier") or ""))
                if taken["errors"]:
                    problems.append(index.explain(taken))
                else:
                    _save_whatsapp_destination(inf_id, destination, role="whatsapp",
                                               wa_session_key=WA_SESSION_KEY(inf_id))
                    added.append(f"WhatsApp {destination['identifier']} (pending a test)")
            elif _looks_like_whatsapp_jid(whatsapp):
                problems.append(error or "That WhatsApp destination could not be saved.")
            else:
                # An invite link needs the creator's paired phone to resolve: save
                # the line for the pairing step instead of losing it.
                session["guided_wa_link"] = whatsapp[:300]
                problems.append(
                    "WhatsApp invite link kept: pair the phone (QR or code) below and press "
                    "Save channels again to resolve it.")
        else:
            problems.append("WhatsApp channel link: paste a whatsapp.com/channel link, a "
                            "chat.whatsapp.com invite, or the …@newsletter id.")

    mirror_pick = request.form.get("mirror_whatsapp")
    if mirror_pick in {"0", "1"} and profile:
        main = db.get_channel(inf_id, "broadcast")
        if main:
            db.update_channel_details(int(main["id"]), mirror_whatsapp=mirror_pick)

    # Which deal sources this creator posts from. Ticked boxes win; a creator with
    # no selection falls back to the hub's default source (LootZoneIndia11) rather
    # than to "every source", which is what the owner asked for.
    source_tokens = [token for token in request.form.getlist("sources")
                     if str(token or "").strip()]
    source_specs = request.form.get("sources_text", "")
    if source_tokens or source_specs:
        selection = sources.normalize_selection(
            ", ".join(source_tokens + ([source_specs] if source_specs else []))
        )
    elif request.form.get("sources_all"):
        selection = ""
    else:
        selection = sources.default_token()
    if request.form.get("sources_all"):
        selection = ""
    if selection != (profile.get("allowed_sources") or ""):
        db.update_influencer(inf_id, allowed_sources=selection)
        added.append(f"Sources {sources.selection_line(selection)}")

    if added:
        security.audit(_session_actor(), _session_role(), "guided_channels_saved", target=profile.get("name"),
                       detail=" · ".join(added)[:200], ip=_client_ip())
        flash("✅ " + " · ".join(added), "success")
    for problem in problems:
        flash("⚠ " + problem, "warning")
    if not added and not problems:
        flash("Paste at least the main channel link (and the approval one if you have it).",
              "warning")
    return redirect(url_for("guided_setup", creator=inf_id, step="categories"))


@app.context_processor
def product_policy_context():
    from influencer_hub.category_preferences import view
    return {"category_preference_view": view, "product_category_choices": onboarding.CATEGORY_CHOICES,
            "product_category_picked": onboarding.selected_categories}


def _product_preferences_from_form(profile):
    from influencer_hub.category_preferences import parse
    current = parse()
    current.update(mode=request.form.get("category_mode", "strict"),
                   mixed=request.form.get("mixed_products", "block"),
                   excluded_categories=request.form.getlist("excluded_categories"),
                   required_keywords=request.form.get("required_keywords", ""),
                   blocked_keywords=request.form.get("blocked_keywords", ""))
    return json.dumps(parse(current), sort_keys=True)


@app.route("/influencer/<int:inf_id>/product-preferences", methods=["POST"])
def save_product_preferences(inf_id):
    from influencer_hub.category_preferences import validate_spec
    profile = db.get_influencer(inf_id)
    if not profile:
        abort(404)
    try:
        picked = request.form.getlist("categories")
        if len(picked) > 50:
            raise ValueError("Too many categories.")
        if not picked and request.form.get("category_mode", "strict") == "strict":
            raise ValueError("Select a category, or explicitly choose All categories.")
        spec = validate_spec(",".join(picked))
        preferences = _product_preferences_from_form(profile)
    except ValueError as exc:
        flash(str(exc) + " Nothing was saved.", "error")
        return redirect(url_for("influencer_detail", inf_id=inf_id) + "#product-preferences")
    db.update_influencer(inf_id, categories=spec, category_preferences=preferences)
    security.audit(_session_actor(), _session_role(), "product_preferences_saved", target=str(inf_id),
                   detail="categories=" + (spec or "all") + " preferences=" + preferences, ip=_client_ip())
    flash("Product preferences saved for this creator only. Channels can narrow strict rules, never widen them.", "success")
    return redirect(url_for("influencer_detail", inf_id=inf_id) + "#product-preferences")


@app.route("/guided/categories", methods=["POST"])
def guided_categories_step():
    """Step 3: which deals, which hours, which networks (pre-filled from presets)."""
    profile = _guided_creator({})
    if not profile:
        flash("Pick a creator first.", "warning")
        return redirect(url_for("guided_setup"))
    inf_id = int(profile["id"])

    picked = request.form.getlist("categories")
    free_text = request.form.get("categories_text", "").strip()
    categories = onboarding.category_text(picked) if picked else free_text
    preferences = None
    if "category_mode" in request.form:
        from influencer_hub.category_preferences import validate_spec
        try:
            if not picked and not free_text and request.form.get("category_mode") == "strict":
                raise ValueError("Select a category, or explicitly choose All categories.")
            # Validate original values before the picker can discard unknown IDs.
            validate_spec(",".join(picked) if picked else free_text)
            preferences = _product_preferences_from_form(profile)
        except ValueError as exc:
            flash(str(exc) + " Nothing was saved.", "error")
            return redirect(url_for("guided_setup", creator=inf_id, step="categories"))
    schedule = request.form.get("posting_schedule", "").strip()
    price_filter = request.form.get("price_filter", "").strip() or "all"
    flags = {}
    for key in ("allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon"):
        flags[key] = str(request.form.get(key, "")).strip().lower() in {"1", "on", "yes"}
    if not (flags["allow_amazon"] or flags["allow_earnkaro"] or flags["allow_hypd"]):
        flash("⚠ Switch on at least one network — otherwise nothing can be posted.",
              "warning")
        flags["allow_amazon"] = True
    db.update_influencer(inf_id, categories=categories, posting_schedule=schedule,
                         price_filter=price_filter, category_preferences=preferences, **flags)
    mirror = request.form.get("mirror_whatsapp")
    main = db.get_channel(inf_id, "broadcast")
    if main and mirror in {"0", "1"}:
        db.update_channel_details(int(main["id"]), mirror_whatsapp=mirror)
    security.audit(_session_actor(), _session_role(), "guided_deals_saved", target=profile.get("name"),
                   detail=f"categories {categories or 'all'} · hours {schedule or 'any'} · "
                          f"price {price_filter}", ip=_client_ip())
    flash("✅ Deal settings saved.", "success")
    return redirect(url_for("guided_setup", creator=inf_id, step="post"))


@app.route("/guided/finish", methods=["POST"])
def guided_finish():
    """Step 4: activate everything and point at the first post."""
    profile = _guided_creator({})
    if not profile:
        flash("Pick a creator first.", "warning")
        return redirect(url_for("guided_setup"))
    inf_id = int(profile["id"])
    db.set_influencer_active(inf_id, True)
    session.pop("guided_creator", None)
    session.pop("guided_fresh", None)
    session.pop("guided_wa_link", None)
    checkpoint = onboarding.checkpoint(db.get_influencer(inf_id), db.list_channels(inf_id))
    security.audit(_session_actor(), _session_role(), "guided_finished", target=profile.get("name"),
                   detail=onboarding.progress_text(checkpoint), ip=_client_ip())
    flash(f"🚀 {profile.get('name')} is live — {onboarding.progress_text(checkpoint)}. "
          "The worker posts to it as soon as a matching deal arrives; use "
          "“Send a test deal” below to see it happen now.", "success")
    return redirect(url_for("guided_setup", creator=inf_id, step="post", done=1))


def _add_defaults_kwargs() -> dict:
    """The owner's new-creator presets as ``db.add_influencer`` keyword arguments."""
    values = presets.resolved()
    return {
        "allow_amazon": bool(values.get("allow_amazon", True)),
        "allow_earnkaro": bool(values.get("allow_earnkaro", True)),
        "allow_hypd": bool(values.get("allow_hypd", True)),
        "only_amazon": bool(values.get("only_amazon", False)),
        "hypd_store_id": str(values.get("hypd_store_id") or ""),
        "categories": str(values.get("categories") or ""),
        "posting_schedule": str(values.get("posting_schedule") or ""),
        "price_filter": str(values.get("price_filter") or "all") or "all",
    }


def _setup_page_context() -> dict:
    """The few things ``guided.html`` shares with the setup page."""
    return {
        "default_amazon_tag": config.AMAZON_ASSOCIATE_TAG,
        "current_hypd_store": _effective_hypd_store_id(),
    }


@app.route("/easy-setup", methods=["GET", "POST"])
def easy_setup():
    """One screen: creator + approval channel + main channel + 3 switches."""
    sources = db.list_sources(active_only=False)
    active_sources = sum(1 for source in sources if source.get("active"))
    values = _easy_setup_defaults()
    result: dict | None = session.pop("_easy_setup_result", None)
    bulk_result: dict | None = None
    bulk_token = (request.args.get("bulk") or "").strip()
    if bulk_token:
        bulk_result = BULK_RESULTS.pop(bulk_token, None) or BULK_RESULTS.get(bulk_token)
    errors: list[str] = []

    if request.method == "POST":
        values = _easy_setup_form_values()
        tag_value, tag_note, tag_blocked = _normalize_amazon_tag(values["amazon_tag"])
        if tag_blocked:
            errors.append(tag_note)
        elif not tag_value:
            values["amazon_tag"] = config.AMAZON_ASSOCIATE_TAG
            tag_note = (
                f"No Amazon tag was given, so the configured fallback "
                f"{config.AMAZON_ASSOCIATE_TAG} will sign Amazon links. "
                "Put the creator's own tag here to earn from their id."
            )
        else:
            values["amazon_tag"] = tag_value
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
            from influencer_hub import commission_guard

            proof = commission_guard.amazon_tag_proof(
                values["amazon_tag"], compact=bool(main_ident)
            )
            flash(
                f"✅ {applied['name']} is set up and posting. "
                f"Amazon → {values['amazon_tag']}"
                + (", other merchants → EarnKaro" if values["allow_earnkaro"] else "")
                + (f", Meesho → HYPD store {values['hypd_store_id']}" if values["allow_hypd"] else ""),
                "success",
            )
            proof_message = (
                "Amazon deals will post as " + proof["posted_link"]
                + (" (verified ✅)" if proof["ok"] else " (⚠ not verified — check the tag)")
            )
            if proof.get("shortened"):
                proof_message += " → opens only " + proof["redirect_target"]
            elif proof.get("short_link_status", {}).get("active") and proof.get("short_link_error"):
                proof_message += " ⚠ " + proof["short_link_error"] + " Using canonical tagged URL instead."
            flash(proof_message, "success" if proof["ok"] else "warning")
            if applied["whatsapp"]:
                flash(
                    "💬 WhatsApp channel saved as Pending. Pair the creator's phone "
                    "(QR or the phone-linking code) and send one Test post from their "
                    "page — after that, main-channel deals are mirrored there "
                    "automatically.",
                    "success" if values.get("mirror_whatsapp", True) else "warning",
                )
            elif applied["whatsapp_note"]:
                flash("⚠ WhatsApp channel: " + applied["whatsapp_note"], "warning")
            elif values.get("mirror_whatsapp", True):
                flash(
                    "🔁 WhatsApp mirror is ON: as soon as this creator has a ready "
                    "WhatsApp channel, every post from their main channel is copied "
                    "there with the same deals.",
                    "warning",
                )
            if tag_note:
                flash("⚠ " + tag_note, "warning")
            return redirect(url_for("easy_setup", done=applied["inf_id"]))

    preview = _routing_preview(**_routing_arguments(values))

    # Which creator is this screen about? The just-saved one, or the one the
    # operator picked with "?creator=<id>" (so the same one screen edits an
    # existing creator and still shows the next step).
    focus_id = request.args.get("creator", type=int) or (result or {}).get("inf_id")
    if request.method == "GET" and focus_id and not request.args.get("blank"):
        profile = db.get_influencer(focus_id)
        if profile:
            values = _easy_setup_values_from_profile(profile)

    creators = db.list_influencers()
    plan = None
    creator_plan = None
    wa_pairing = None
    # Who was set up when, in one line: the owner opens Easy Setup and instantly
    # sees how long the oldest and the newest creator have been running.
    hub_age = age.summarise(creators)
    wa_key = ""
    example_link = ""
    # The creator picker is one page of pills, not the whole hub: with a
    # thousand profiles the strip stays a strip (search + paging above it).
    pill_q = (request.args.get("pill_q") or "").strip()
    pill_offset = max(0, request.args.get("pill_offset", 0, type=int) or 0)
    pill_size = max(6, min(120, request.args.get("pill_size", 40, type=int) or 40))
    if creators:
        try:
            # The focused creator always has to be in the payload, even when the
            # current search/page would filter it out.
            plan = readiness.overview(
                influencers=creators, query=pill_q, state="",
                limit=pill_size, offset=pill_offset,
            )
            creator_plan = next(
                (c for c in plan["creators"] if c["id"] == focus_id), None
            )
            if focus_id and creator_plan is None:
                focused = readiness.creator_overview(focus_id)
                creator_plan = (focused["creators"] or [None])[0]
        except Exception as exc:  # pragma: no cover - the form must still render
            app.logger.warning("easy-setup readiness failed: %s", exc)
    wa_channel_id = 0
    if focus_id:
        wa_key = WA_SESSION_KEY(focus_id)
        wa_channels = [c for c in db.list_channels(focus_id)
                       if str(c.get("platform") or "") in {"whatsapp_channel", "whatsapp_group"}]
        pending_first = next((c for c in wa_channels
                              if str(c.get("status") or "").lower() not in pipeline.ACTIVE_CHANNEL_STATUSES),
                             None)
        wa_channel_id = int((pending_first or (wa_channels[0] if wa_channels else {})).get("id") or 0)
        row = next(
            (r for r in db.list_wa_sessions(focus_id) if r.get("session_key") == wa_key),
            None,
        ) or {}
        wa_pairing = {
            "phone": str(row.get("pairing_phone") or ""),
            "code": str(row.get("pairing_code") or ""),
            "error": str(row.get("pairing_error") or ""),
            "status": str(row.get("status") or "offline"),
            "has_code": bool(row.get("pairing_code")),
        }
        profile = db.get_influencer(focus_id)
        if profile:
            example_link = readiness.example_tagged_link(profile)

    return render_template(
        "easy_setup.html",
        values=values,
        preview=preview,
        result=result,
        errors=errors,
        switches=ROUTING_SWITCHES,
        source_count=len(sources),
        active_source_count=active_sources,
        flow=_flow_snapshot(live_probe=_wants_live_probe()),
        plan=plan,
        creator_plan=creator_plan,
        creators=creators,
        hub_age=hub_age,
        creator_ages={int(inf["id"]): age.setup_label(
            inf.get("created_at"), member=str(inf.get("added_by") or ""))
            for inf in creators},
        bulk_result=bulk_result,
        bulk_limit=BULK_MAX_ROWS,
        pill_q=pill_q,
        pill_offset=pill_offset,
        pill_size=pill_size,
        pill_prev=max(0, pill_offset - pill_size),
        pill_next=pill_offset + pill_size,
        focus_id=focus_id,
        wa_key=wa_key,
        wa_pairing=wa_pairing,
        wa_channel_id=wa_channel_id,
        example_link=example_link,
    )


def _easy_setup_values_from_profile(profile: dict) -> dict:
    """Prefill the one-screen form from a saved creator (edit in place)."""
    inf_id = int(profile["id"])
    channels = db.list_channels(inf_id)

    def _identifier(role: str) -> str:
        match = next(
            (c for c in channels
             if str(c.get("role") or "broadcast") == role
             and str(c.get("platform") or "") == "telegram"),
            None,
        )
        return str(match.get("identifier") or "") if match else ""

    whatsapp = next(
        (c for c in channels
         if str(c.get("platform") or "") in {"whatsapp_channel", "whatsapp_group"}),
        None,
    )
    values = _easy_setup_defaults()
    values.update({
        "name": str(profile.get("name") or ""),
        "amazon_tag": str(profile.get("amazon_tag") or config.AMAZON_ASSOCIATE_TAG),
        "approval": _identifier("approval"),
        "main": _identifier("broadcast"),
        "whatsapp": str((whatsapp or {}).get("identifier") or ""),
        "allow_amazon": bool(profile.get("allow_amazon", True)),
        "allow_earnkaro": bool(profile.get("allow_earnkaro", True)),
        "allow_hypd": bool(profile.get("allow_hypd", True)),
        "only_amazon": bool(profile.get("only_amazon", False)),
        # Our store stays ours: the profile's stored store id only matters when
        # central accounts are switched off (legacy path).
        "hypd_store_id": _routing_hypd_store(profile.get("hypd_store_id")),
        "mirror_whatsapp": bool(_main_mirror_flag(channels)),
    })
    return values


def _main_mirror_flag(channels: list[dict]) -> bool:
    """The main channel's mirror switch: '' follows the global default (ON)."""
    main = next(
        (c for c in channels
         if str(c.get("platform") or "") == "telegram"
         and str(c.get("role") or "broadcast") == "broadcast"),
        None,
    )
    if not main:
        return pipeline.mirror_main_to_whatsapp_enabled()
    flag = str(main.get("mirror_whatsapp") or "").strip().lower()
    if flag in {"1", "true", "yes", "on", "enabled"}:
        return True
    if flag in {"0", "false", "no", "off", "disabled", "none"}:
        return False
    return pipeline.mirror_main_to_whatsapp_enabled()


def _apply_easy_setup(values: dict, approval_ident: str, main_ident: str) -> dict:
    """Create or update one creator with both channels and the chosen routes."""
    return _apply_creator(values, approval_ident, main_ident)


def _apply_creator(values: dict, approval_ident: str, main_ident: str,
                   known_profiles: dict | None = None) -> dict:
    """The single creator upsert used by Easy Setup (one row or a bulk paste).

    ``known_profiles`` is the ``{lowercased name: profile}`` map a bulk caller
    already read once, so importing a thousand creators does not re-read the
    whole table a thousand times.
    """
    name = values["name"]
    key = name.strip().lower()
    if known_profiles is not None:
        existing = known_profiles.get(key)
    else:
        existing = next(
            (profile for profile in db.list_influencers()
             if str(profile.get("name") or "").strip().lower() == key),
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
    # Optional profile details a row may carry (phone, categories, hours, price
    # filter, notes). None of them is written as an empty value on an update, so
    # a paste that only mentions channels never wipes a saved field.
    extras = {
        "phone_number": (str(values.get("phone_number") or "").strip() or None),
        "categories": (str(values.get("categories") or "").strip() or None),
        "posting_schedule": (str(values.get("posting_schedule") or "").strip() or None),
        "price_filter": (str(values.get("price_filter") or "").strip() or None),
        "notes": (str(values.get("notes") or "").strip() or None),
    }
    if existing:
        inf_id = int(existing["id"])
        db.update_influencer(inf_id, name=name, amazon_tag=values["amazon_tag"],
                             **settings, **extras)
        action = "updated"
    else:
        inf_id = db.add_influencer(
            name, values["amazon_tag"], allow_amazon=values["allow_amazon"],
            allow_earnkaro=values["allow_earnkaro"], allow_hypd=values["allow_hypd"],
            only_amazon=values["only_amazon"], hypd_store_id=values["hypd_store_id"],
            phone_number=str(values.get("phone_number") or "").strip(),
            categories=str(values.get("categories") or "").strip(),
            posting_schedule=str(values.get("posting_schedule") or "").strip(),
            price_filter=str(values.get("price_filter") or "").strip() or "all",
            notes=str(values.get("notes") or "").strip(),
        )
        action = "created"
    if known_profiles is not None:
        known_profiles[key] = db.get_influencer(inf_id) or {"id": inf_id, "name": name}
    db.set_influencer_active(inf_id, True)

    channels = db.list_channels(inf_id)
    created, updated = [], []

    def _upsert(identifier: str, role: str) -> None:
        if not identifier:
            return
        # Only a main (broadcast) Telegram channel mirrors into WhatsApp; the
        # creator can still switch it off per channel on their page.
        mirror_flag = (
            "1" if values.get("mirror_whatsapp", True) else "0"
        ) if role == "broadcast" else None
        # Match against the list read once above, so creating both channels for
        # one creator (or a thousand creators) does not re-read the table.
        match = next(
            (channel for channel in channels
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
                mirror_whatsapp=mirror_flag,
            )
            updated.append(identifier)
        else:
            db.add_channel(
                inf_id, "telegram", identifier, status="ready", role=role,
                allow_amazon=values["allow_amazon"],
                allow_earnkaro=values["allow_earnkaro"],
                allow_hypd=values["allow_hypd"],
                hypd_store_id=values["hypd_store_id"],
                mirror_whatsapp=mirror_flag or "",
            )
            channels.append({
                "id": 0, "influencer_id": inf_id, "platform": "telegram",
                "identifier": identifier, "role": role, "status": "ready",
            })
            created.append(identifier)

    _upsert(approval_ident, "approval")
    _upsert(main_ident, "broadcast")

    # WhatsApp channel (optional): saved as Pending on purpose — it starts
    # receiving posts (including mirrored main-channel posts) only after the
    # phone is paired and one real test post succeeds.
    whatsapp_added: list[str] = []
    whatsapp_note = ""
    if values.get("whatsapp"):
        destination, wa_error = _resolve_whatsapp_destination(inf_id, values["whatsapp"])
        if destination:
            _save_whatsapp_destination(
                inf_id, destination, role="whatsapp",
                wa_session_key=WA_SESSION_KEY(inf_id),
            )
            whatsapp_added.append(destination["identifier"])
        else:
            whatsapp_note = wa_error or "The WhatsApp channel link could not be resolved."

    sources = db.list_sources(active_only=False)
    return {
        "action": action,
        "inf_id": inf_id,
        "name": name,
        "created": created,
        "updated": updated,
        "whatsapp": whatsapp_added,
        "whatsapp_note": whatsapp_note,
        "existing_channels": len(channels),
        "active_sources": sum(1 for source in sources if source.get("active")),
    }


BULK_MAX_ROWS = 2000
# The last few bulk imports, keyed by a short token and shown once via
# ?bulk=<token>. A thousand lines must never be serialised into the session
# cookie (browsers silently drop anything over ~4 KB).
BULK_RESULT_ROWS = 200
BULK_RESULTS: "OrderedDict[str, dict]" = OrderedDict()
BULK_RESULTS_KEPT = 5


def _remember_bulk_result(summary: dict) -> str:
    token = secrets.token_urlsafe(8)
    BULK_RESULTS[token] = summary
    while len(BULK_RESULTS) > BULK_RESULTS_KEPT:
        BULK_RESULTS.popitem(last=False)
    return token
# One line per creator. Positional order is the same order the single-screen
# form asks for; a header row (or a CSV file with these column names) overrides
# the order so a spreadsheet export works unchanged.
# The column names a paste or a CSV may use. The real map lives in
# influencer_hub.creators (one place, used by the CLI too); this alias stays for
# callers that imported it.
BULK_COLUMNS = dict(creators.ALIASES)
BULK_FIELDS = ("name", "tag", "main", "approval", "whatsapp", "phone")


def parse_bulk_creators(text: str) -> list[dict]:
    """Parse pasted lines or CSV into ``{line, name, tag, main, approval, whatsapp, …}``.

    Handles comma/tab/pipe/semicolon rows (documented column order, or a header
    row) **and** plain white-space lines like

    ``Ravi Loots ravi-21 @ravi_loots @ravi_amazon_deals 1203630…@newsletter +91 98765 43210``

    where every token is recognised for what it is. ``creators.parse`` owns the
    logic, so the dashboard, the team page and the CLI can never disagree.
    """
    return creators.parse(text)


def _new_creator_defaults() -> dict:
    """What every new creator starts with: the owner's presets, then the code.

    Read once per request and handed to ``_bulk_row_values``, so the team page,
    Easy Setup, a bulk paste and the inventory import all create creators the
    same way (see ``/setup → 👥 New-creator defaults``).
    """
    values = presets.resolved()
    values["mirror_whatsapp"] = bool(values.get("mirror_whatsapp", True))
    return values


def _bulk_row_values(row: dict, defaults: dict) -> dict:
    """Turn one parsed row into the values dict the creator upsert expects.

    Precedence, most specific first: the pasted row → the form on screen → the
    owner's new-creator presets → the code defaults.
    """
    values = {**_new_creator_defaults(), **dict(defaults or {})}
    values.update({
        "name": str(row.get("name") or "").strip(),
        "amazon_tag": (str(row.get("tag") or "").strip()
                       or str(values.get("amazon_tag") or "").strip()
                       or config.AMAZON_ASSOCIATE_TAG),
        "approval": str(row.get("approval") or "").strip(),
        "main": str(row.get("main") or "").strip(),
        "whatsapp": str(row.get("whatsapp") or "").strip(),
        "phone_number": str(row.get("phone") or "").strip(),
        "categories": (str(row.get("categories") or "").strip()
                       or str(values.get("categories") or "").strip()),
        "posting_schedule": (str(row.get("schedule") or "").strip()
                             or str(values.get("posting_schedule") or "").strip()),
        "price_filter": (str(row.get("price_filter") or "").strip()
                         or str(values.get("price_filter") or "").strip() or "all"),
        "notes": str(row.get("notes") or "").strip(),
        "hypd_store_id": (str(row.get("hypd_store_id") or "").strip()
                          or str(values.get("hypd_store_id") or "").strip()),
    })
    return values


def _bulk_prepare_row(row: dict, defaults: dict) -> tuple[dict | None, str, str, str]:
    """Validate one row. Returns (values, approval_ident, main_ident, error)."""
    values = _bulk_row_values(row, defaults)
    if not values["name"]:
        return None, "", "", "name missing"
    tag_value, tag_note, tag_blocked = _normalize_amazon_tag(values["amazon_tag"])
    if tag_blocked:
        return None, "", "", tag_note
    values["amazon_tag"] = tag_value or config.AMAZON_ASSOCIATE_TAG
    approval_ident = clean_identifier(values["approval"]) if values["approval"] else ""
    main_ident = clean_identifier(values["main"]) if values["main"] else ""
    if approval_ident:
        ok, error = _valid_telegram_identifier(approval_ident)
        if not ok:
            return None, "", "", f"approval channel: {error}"
    if main_ident:
        ok, error = _valid_telegram_identifier(main_ident)
        if not ok:
            return None, "", "", f"main channel: {error}"
    if not approval_ident and not main_ident:
        return None, "", "", "no main or approval channel"
    if approval_ident and main_ident and approval_ident == main_ident:
        return None, "", "", "approval and main channels are the same"
    return values, approval_ident, main_ident, ""


def _looks_like_whatsapp_jid(value: str) -> bool:
    return str(value or "").strip().lower().endswith(
        ("@newsletter", "@g.us", "@s.whatsapp.net")
    )


def _bulk_whatsapp(values: dict, inf_id: int) -> tuple[list[str], str]:
    """Save a WhatsApp destination when the row already has its real JID.

    A pasted invite link needs the creator's paired phone to resolve, so a bulk
    import of a thousand rows must not try a thousand hub calls: those rows are
    reported as "paste the link after pairing" instead of being guessed at.
    """
    raw = str(values.get("whatsapp") or "").strip()
    if not raw:
        return [], ""
    if not _looks_like_whatsapp_jid(raw):
        return [], (
            "WhatsApp link saved later: it needs the creator's paired phone to "
            "resolve. Pair the phone, then paste it on the creator page."
        )
    platform = "whatsapp_group" if raw.lower().endswith("@g.us") else "whatsapp_channel"
    destination = {"platform": platform, "identifier": raw, "invite_link": ""}
    try:
        _save_whatsapp_destination(
            inf_id, destination, role="whatsapp", wa_session_key=WA_SESSION_KEY(inf_id),
        )
    except Exception as exc:  # pragma: no cover - one bad row must not stop the batch
        return [], f"WhatsApp destination could not be saved: {exc}"
    return [raw], ""


@app.route("/easy-setup/bulk", methods=["POST"])
def easy_setup_bulk():
    """Create or update many creators from one paste (or a CSV upload).

    Re-running the same paste updates the same profiles instead of duplicating
    them, so a corrected spreadsheet can simply be pasted again.
    """
    raw_text = request.form.get("bulk_lines", "")
    upload = request.files.get("bulk_file")
    if upload and upload.filename:
        try:
            raw_text = upload.stream.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # pragma: no cover - unreadable upload
            raw_text = ""
            flash(f"⚠ Could not read the uploaded file: {exc}", "error")

    defaults = _easy_setup_form_values()
    defaults["whatsapp"] = ""   # per row
    rows = parse_bulk_creators(raw_text)
    results: list[dict] = []
    if not rows:
        flash(
            "⚠ Nothing to import: paste one creator per line, for example "
            "`Ravi Loots, ravi-21, @ravi_loots, @ravi_amazon_deals, 1203630…@newsletter`.",
            "warning",
        )
        return redirect(url_for("easy_setup"))

    # One read of the current profiles for the whole paste (a thousand-row
    # import must not become a thousand full-table scans).
    known_profiles = {
        str(profile.get("name") or "").strip().lower(): profile
        for profile in db.list_influencers()
    }
    # The assignment guard: an update of the same creator is fine (its own tag and
    # channels are ignored by id); giving somebody else's tag or channel to a new
    # creator is refused with a name and a reason.
    index = guard.GuardIndex.from_db()
    for row in rows[:BULK_MAX_ROWS]:
        values, approval_ident, main_ident, error = _bulk_prepare_row(row, defaults)
        if error:
            results.append({"line": row.get("line"), "name": str(row.get("name") or "?"),
                            "status": "failed", "detail": error})
            continue
        existing = known_profiles.get(str(values["name"]).strip().lower()) or {}
        taken = index.preflight(name=values["name"], tag=values["amazon_tag"],
                               ignore_id=int(existing.get("id") or 0),
                               approval=approval_ident, main=main_ident,
                               whatsapp=values.get("whatsapp") or "")
        if taken["errors"]:
            results.append({"line": row.get("line"), "name": values["name"],
                            "status": "failed", "detail": "⛔ " + index.explain(taken)})
            continue
        try:
            applied = _apply_creator(values, approval_ident, main_ident,
                                     known_profiles=known_profiles)
        except Exception as exc:  # pragma: no cover - keep the batch going
            results.append({"line": row.get("line"), "name": values["name"],
                            "status": "failed", "detail": str(exc)[:200]})
            continue
        index.note(applied["inf_id"], values["amazon_tag"], name=applied["name"],
                   channels=[{"platform": "telegram", "identifier": identifier, "role": role}
                             for role, identifier in (("approval", approval_ident),
                                                      ("broadcast", main_ident)) if identifier]
                   + ([{"platform": "whatsapp", "identifier": values["whatsapp"], "role": "whatsapp"}]
                      if values.get("whatsapp") else []))
        whatsapp_saved, whatsapp_note = _bulk_whatsapp(
            values, int(applied["inf_id"]))
        channels = [name for name in (approval_ident, main_ident) if name]
        detail = ", ".join(channels) or "no channels"
        if whatsapp_saved:
            detail += f" · WhatsApp {whatsapp_saved[0]} (Pending)"
        if whatsapp_note:
            detail += f" · ⚠ {whatsapp_note}"
        results.append({"line": row.get("line"), "name": applied["name"],
                        "status": applied["action"], "detail": detail})

    summary = {
        "token": "",
        "total": len(results),
        "created": sum(1 for r in results if r["status"] == "created"),
        "updated": sum(1 for r in results if r["status"] == "updated"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": max(0, len(rows) - len(results)),
        "rows": results[:BULK_RESULT_ROWS],
        "rows_total": len(results),
        "limit": BULK_MAX_ROWS,
    }
    token = _remember_bulk_result(summary)
    flash(
        f"📦 Bulk setup: {summary['created']} created, {summary['updated']} updated, "
        f"{summary['failed']} failed"
        + (f", {summary['skipped']} skipped over the {BULK_MAX_ROWS}-row limit"
           if summary["skipped"] else "")
        + ". Open the dashboard command center to see each creator's next step.",
        "success" if not summary["failed"] else "warning",
    )
    return redirect(url_for("easy_setup", bulk=token))


@app.route("/api/flow")
def api_flow():
    """Read-only deal-flow status: worker, sources, channels (no network calls)."""
    try:
        stall_seconds = max(600, int(request.args.get("stall_seconds", 3 * 3600)))
    except (TypeError, ValueError):
        stall_seconds = 3 * 3600
    try:
        window_hours = max(1, min(168, int(request.args.get("window_hours", 24))))
    except (TypeError, ValueError):
        window_hours = 24
    return jsonify(_flow_snapshot(
        stall_seconds=stall_seconds, window_hours=window_hours, live_probe=_wants_live_probe(),
    ))


@app.route("/api/category-preview", methods=["POST"])
def category_preview_api():
    """Local-only product evidence; never sends, fetches URLs or changes settings."""
    from influencer_hub.categories import preview
    values = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    if not isinstance(values, dict):
        return jsonify(ok=False, error="invalid_input"), 400
    text = values.get("text", "")
    selected = values.get("categories", "")
    if not isinstance(text, str) or not isinstance(selected, str):
        return jsonify(ok=False, error="invalid_input"), 400
    if not text.strip():
        return jsonify(ok=False, error="Paste a product or deal first."), 400
    if len(text) > 20000 or len(selected) > 1000:
        return jsonify(ok=False, error="Preview input is too long."), 400
    if "preferences" in values or "creator_id" in values:
        from influencer_hub.category_preferences import evaluate, parse, validate_spec
        def preview_id(value):
            text_id = str(value)
            if (isinstance(value, bool) or len(text_id) > 19 or not text_id.isascii()
                    or not text_id.isdecimal()):
                raise ValueError("Invalid creator/channel id")
            number = int(text_id)
            if not 0 < number <= 9223372036854775807:
                raise ValueError("Invalid creator/channel id")
            return number

        try:
            channel = None
            if "preferences" in values:
                if not isinstance(values["preferences"], dict):
                    raise ValueError("preferences must be an object")
                profile = {"categories": validate_spec(selected),
                           "category_preferences": json.dumps(parse(values["preferences"]))}
            else:
                creator_id = preview_id(values["creator_id"])
                profile = db.get_influencer(creator_id)
                if not profile:
                    return jsonify(ok=False, error="Creator not found"), 404
                if "channel_id" in values:
                    channel_id = preview_id(values["channel_id"])
                    channel = next((ch for ch in db.list_channels(profile["id"])
                                    if ch["id"] == int(channel_id)), None)
                    if not channel:
                        return jsonify(ok=False, error="Channel not found for this creator"), 404
            decision = evaluate(text, profile, channel)
            return jsonify(ok=True, preview=decision)
        except ValueError as exc:
            return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, preview=preview(text, selected))


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
        custom_button2_text=snapshot.get("custom_button2_text") or "",
        custom_button2_url=snapshot.get("custom_button2_url") or "",
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

    # Check live WhatsApp session status & fetch groups/channels. `admin_chats`
    # is the operator's own ask — the channels this paired number is an admin of,
    # by name, so a destination can be picked from a dropdown instead of pasted.
    wa_status_info = {
        "connected": False, "phone": "", "chats": [], "admin_chats": [], "admin_error": "",
    }
    try:
        status_res = _run(whatsapp_client.session_status(wa_key))
        if status_res.get("status") == "connected":
            wa_status_info["connected"] = True
            wa_status_info["phone"] = status_res.get("phone", "")
            chats = _run(whatsapp_client.list_chats(wa_key))
            wa_status_info["chats"] = [c for c in chats if c.get("isGroup") or c.get("isChannel")]
            wa_status_info["admin_chats"] = _run(whatsapp_client.list_admin_chats(wa_key))
    except Exception as exc:
        wa_status_info["admin_error"] = str(exc)[:160]

    # Phone-link (code) pairing: the live hub is the source of truth, the DB
    # copy keeps the code visible across a page reload.
    wa_pairing = {"phone": "", "code": "", "error": "", "has_code": False}
    stored_session = next(
        (row for row in wa_sessions if row.get("session_key") == wa_key), None
    )
    if stored_session:
        wa_pairing.update({
            "phone": str(stored_session.get("pairing_phone") or ""),
            "code": str(stored_session.get("pairing_code") or ""),
            "error": str(stored_session.get("pairing_error") or ""),
            "has_code": bool(stored_session.get("pairing_code")),
        })
    pairing_attempted = bool(
        stored_session
        and (
            stored_session.get("pairing_phone")
            or stored_session.get("pairing_code")
            or str(stored_session.get("status") or "") == "pairing"
        )
    )
    if pairing_attempted:
        # Only ask the hub when a pairing attempt is actually in flight: an
        # idle creator page must not pay for a round trip it cannot use.
        try:
            live_pairing = _run(whatsapp_client.pairing_state(wa_key))
            if live_pairing and live_pairing.get("ok"):
                wa_pairing.update({
                    "phone": str(live_pairing.get("phone") or "") or wa_pairing["phone"],
                    "code": str(live_pairing.get("code") or ""),
                    "error": str(live_pairing.get("error") or wa_pairing["error"] or ""),
                    "has_code": bool(live_pairing.get("hasCode")),
                })
        except Exception:
            pass

    # What the main-channel -> WhatsApp mirror will do for this creator, as a
    # fact instead of a toggle: sources, destinations and mirrored posts.
    mirror_info = pipeline.mirror_plan(inf)
    
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
    source_rows = _source_rows()
    # A Telegram test alert contains no Amazon URL and bypasses the commission
    # guard, so it cannot tell an operator whether real Amazon deals will post.
    # Show the exact per-channel signing source on the creator page instead.
    tag_diag = []
    try:
        for ch in channels:
            if str(ch.get("platform") or "").lower() != "telegram":
                continue
            signing = tag_proof.signing(inf, ch)
            tag_diag.append({
                "channel": str(ch.get("identifier") or ""),
                "role": str(ch.get("role") or "broadcast"),
                "status": str(ch.get("status") or ""),
                "tag": signing.get("tag") or "",
                "source": signing.get("source") or "fallback",
                "own": bool(signing.get("own")),
                "usable": bool(signing.get("usable")),
            })
    except Exception:
        tag_diag = []

    active_channels = [
        ch for ch in channels
        if str(ch.get("status") or "").strip().lower() in {"ready", "active"}
    ]
    pending_channels = [
        ch for ch in channels
        if str(ch.get("status") or "").strip().lower() == "pending"
    ]
    paused_channels = [
        ch for ch in channels
        if str(ch.get("status") or "").strip().lower() == "paused"
    ]
    active_tag_holds = [
        item for item in tag_diag
        if item.get("status") in {"ready", "active"}
        and (not item.get("own") or not item.get("usable"))
    ]
    delivery_readiness = {
        "profile_active": bool(inf.get("active", True)),
        "active_destinations": len(active_channels),
        "pending_destinations": len(pending_channels),
        "paused_destinations": len(paused_channels),
        "amazon_tag_holds": len(active_tag_holds),
        "pending_whatsapp": sum(
            str(ch.get("platform") or "").lower() in {"whatsapp", "whatsapp_group", "whatsapp_channel"}
            for ch in pending_channels
        ),
        "active_names": [
            f"{ch.get('platform', 'channel')} · {ch.get('title') or ch.get('identifier') or 'unnamed'}"
            for ch in active_channels
        ],
    }
    return render_template(
        "influencer.html", inf=inf, channels=channels,
        # Let the source fields suggest what exists: a creator's allowed_sources
        # is a comma list of these tokens (any shape is normalized on save).
        source_tokens=[row["token"] for row in source_rows],
        source_rows=source_rows,
        source_picked=set(sources.selection_tokens(inf.get("allowed_sources") or "")),
        source_is_every=sources.asks_for_everything(inf.get("allowed_sources") or ""),
        source_default=sources.default_token(),
        source_line=sources.selection_line(inf.get("allowed_sources") or ""),
        wa_sessions=wa_sessions, stats=stats, wa_key=wa_key,
        navigation_preview=channel_navigation.navigation_matrix(inf_id, channels),
        wa_hub_url=config.WA_HUB_URL, wa_status_info=wa_status_info,
        wa_pairing=wa_pairing, mirror=mirror_info,
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
        tag_diag=tag_diag,
        delivery_readiness=delivery_readiness,
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
        res = _run(whatsapp_client.create_session(inf_id, "wa")) or {}
        if not res.get("ok"):
            raise RuntimeError(str(res.get("error") or "WhatsApp hub did not start the session"))
        db.upsert_wa_session(inf_id, key, phone="")
        db.set_wa_session_status(key, "connected" if res.get("status") == "connected" else "qr")
        if res.get("status") == "connected":
            flash("WhatsApp account is already connected. QR is not needed.", "success")
        else:
            flash("WhatsApp session started. Keep this page open while the live QR loads.", "success")
    except Exception as e:  # pragma: no cover
        flash(f"Could not start WhatsApp QR session: {str(e)[:240]}", "error")
    return _redirect_after_action(url_for("influencer_detail", inf_id=inf_id))


@app.route("/influencer/<int:inf_id>/pair-wa-phone", methods=["POST"])
def pair_wa_phone(inf_id):
    """Link the influencer's phone with a code instead of a QR scan.

    The operator types the number; WhatsApp shows an 8-character code on that
    phone (Linked devices -> Link with phone number) and the same registered
    device results, so every posting path below stays unchanged. The code is a
    one-time credential: it is stored for the operator's own page and cleared
    the moment the number is linked.
    """
    key = WA_SESSION_KEY(inf_id)
    raw_phone = request.form.get("pairing_phone", "").strip()
    # The dashboard is commonly used for Indian numbers. Accept a local 10-digit
    # mobile and normalize it to E.164 digits before asking Baileys for a code.
    # If +91 was supplied, stripping punctuation already leaves the full number.
    phone = re.sub(r"[^0-9]", "", raw_phone)
    if len(phone) == 10:
        phone = "91" + phone
    if not raw_phone:
        return _redirect_after_action(
            url_for("influencer_detail", inf_id=inf_id),
            wa_pair_status="missing_phone",
        )
    if not (10 <= len(phone) <= 15):
        return _redirect_after_action(
            url_for("influencer_detail", inf_id=inf_id),
            wa_pair_status="bad_phone", phone=phone,
        )

    db.upsert_wa_session(inf_id, key, phone="")
    try:
        res = _run(whatsapp_client.request_pairing_code(key, phone))
    except Exception as exc:
        db.set_wa_session_pairing(key, phone=phone, code="", error=str(exc))
        db.set_wa_session_status(key, "offline")
        return _redirect_after_action(
            url_for("influencer_detail", inf_id=inf_id),
            wa_pair_status="failed",
        )

    code = str((res or {}).get("code") or "").strip()
    if (res or {}).get("ok") and code:
        db.set_wa_session_pairing(key, phone=phone, code=code)
        return _redirect_after_action(
            url_for("influencer_detail", inf_id=inf_id),
            wa_pair_status="code",
        )

    db.set_wa_session_pairing(
        key, phone=phone, code="",
        error=str((res or {}).get("error") or "WhatsApp did not return a pairing code"),
    )
    return _redirect_after_action(
        url_for("influencer_detail", inf_id=inf_id),
        wa_pair_status="failed",
    )


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
    """Resolve a WhatsApp group/channel link or save a detected JID as pending.

    The operator picks the channel from the dropdown of chats where their paired
    number is admin ("vallu admin ga vunna channel ni isthanu"), or pastes an
    invite link. Either way the channel's **name** is saved with the JID, and the
    main→WhatsApp mirror can be switched on in the same step, so "whatever comes
    into their Telegram channel also goes to this WhatsApp channel".
    """
    # These are separate selects; older pages sent the same `chat_jid` name
    # twice, where browsers/Flask could read the empty first select and ignore
    # the actual choice in the second dropdown.
    chat_jid = (
        request.form.get("admin_chat_jid", "").strip()
        or request.form.get("any_chat_jid", "").strip()
        or request.form.get("chat_jid", "").strip()  # backward compatibility
    )
    invite_link = request.form.get("invite_link", "").strip()
    # The dropdown submits "jid|name"; the name is what the operator recognises.
    # Split first: the classifier must see a bare JID, never "…|Pick Loot Deals".
    title = ""
    if chat_jid and "|" in chat_jid:
        chat_jid, _, title = chat_jid.partition("|")
        chat_jid, title = chat_jid.strip(), title.strip()
    raw_destination = invite_link or chat_jid
    picked_src = _source_selection_from_form()
    allowed_src = (request.form.get("allowed_sources", "").strip()
                   if picked_src is None else picked_src)
    mirror = request.form.get("mirror_whatsapp")

    destination, error = _resolve_whatsapp_destination(inf_id, raw_destination)
    if not destination:
        return redirect(url_for(
            "influencer_detail", inf_id=inf_id,
            wa_link_status="failed", wa_link_error=error,
        ))
    if not title:
        # A pasted link resolved to a real destination: use the name the phone
        # knows it by, when there is one.
        title = _whatsapp_chat_title(inf_id, destination.get("identifier") or "")

    channel_settings = {}
    if mirror:
        channel_settings["mirror_whatsapp"] = db._clean_mirror_flag(mirror)

    _save_whatsapp_destination(
        inf_id,
        destination,
        role="whatsapp",
        allowed_sources=allowed_src,
        wa_session_key=WA_SESSION_KEY(inf_id),
        title=title,
        channel_settings=channel_settings or None,
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

    from influencer_hub import advanced_shortener, earnkaro, link_router

    requested_amz_tag = amz_tag or config.AMAZON_ASSOCIATE_TAG
    effective_amz_tag = link_router.effective_amazon_tag(requested_amz_tag)

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

    # Same first step as the pipeline: promote scheme-less links and unwrap
    # shorteners so the preview shows the links that will really be posted.
    sample_text = link_router.promote_scheme_less_links(sample_text)
    unresolved_wrappers: list[str] = []
    if link_router.has_opaque_link(sample_text):
        try:
            sample_text, unresolved_wrappers = _run(asyncio.wait_for(
                link_router.resolve_opaque_links_in_text_async(sample_text, effective_amz_tag), timeout=12
            ))
        except Exception:
            unresolved_wrappers = link_router.unresolved_opaque_links(sample_text)
            warnings_pre: list[str] = [
                "Wrapper links could not be resolved in the preview; they stay visible and the "
                "worker retries such a deal instead of posting it for free."
            ]
        else:
            warnings_pre = []
    else:
        warnings_pre = []

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
        amazon_tag=effective_amz_tag,
        earnkaro_links=earnkaro_links,
        role=role,
        hypd_store_id=hypd_store,
    )
    # The preview must use the same final compacting pass as the worker. This
    # allocates only a local stored redirect record (no deal is dispatched), so
    # the returned /a/<code> link is genuinely clickable evidence.
    if role != "approval":
        try:
            rendered = _run(advanced_shortener.shorten_our_links_advanced(
                rendered, effective_amz_tag, hypd_store
            ))
            if config.LEHLAH_SHORTLINKS_ENABLED:
                rendered = lehlah_shortlinks.shorten_lehlah_links(rendered)
            rendered = link_router.deduplicate_urls_in_text(rendered)
        except Exception as exc:
            warnings.append(
                f"Branded shortening preview could not complete ({type(exc).__name__}); "
                "the canonical attributed links are shown instead."
            )

    from influencer_hub import commission_guard

    expected_pubid = (
        db.get_global_setting("earnkaro_publisher_id") or config.EARNKARO_PUBLISHER_ID or ""
    ).strip()
    our_links = commission_guard.our_affiliate_urls(
        rendered, effective_amz_tag, hypd_store, expected_pubid,
        source_text=sample_text,
    )
    if our_links:
        verdict = "our_link_present"
    elif unresolved_wrappers:
        verdict = "held_for_retry"
    else:
        verdict = "no_our_link"
    preview_short_link_status = amazon_shortlinks.short_link_status()
    return jsonify({
        "ok": True,
        "input": sample_text,
        "rendered": rendered,
        "detected_links": detected_links,
        "our_links": our_links,
        "unresolved_wrappers": unresolved_wrappers,
        "verdict": verdict,
        "warnings": warnings_pre + warnings,
        "network_settings": {
            "amazon": bool(allow_amazon),
            "earnkaro": bool(allow_earnkaro),
            "hypd": bool(allow_hypd),
            "only_amazon": bool(only_amazon),
        },
        "amazon_tag": effective_amz_tag,
        "short_links": {
            **preview_short_link_status,
            "active_for_this_preview": bool(
                role != "approval" and allow_amazon and preview_short_link_status["active"]
            ),
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

    return _redirect_after_action(
        url_for(
            "influencer_detail", inf_id=inf_id, test_status=status,
            test_err=err_msg, ch_name=ch["identifier"] if ch else "",
            ch_platform=ch.get("platform", "") if ch else "",
            ch_status=ch.get("status", "") if ch else "",
        ),
        test_status=status, test_err=err_msg, ch_name=ch["identifier"] if ch else "",
        ch_platform=ch.get("platform", "") if ch else "",
        ch_status=ch.get("status", "") if ch else "",
    )


@app.route("/influencer/<int:inf_id>/create-newsletter", methods=["POST"])
def create_newsletter(inf_id):
    key = WA_SESSION_KEY(inf_id)
    profile = db.get_influencer(inf_id) or {}
    name = request.form.get("name") or f"{profile.get('name') or 'Creator'} Channel"
    desc = request.form.get("description", "")
    status, error = "pending", ""
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
        else:
            status = "failed"
            error = str(res.get("error") or "WhatsApp did not return the new channel id")
    except Exception as e:  # pragma: no cover
        status, error = "failed", str(e)
    return _redirect_after_action(
        url_for("influencer_detail", inf_id=inf_id),
        wa_link_status=status, wa_link_error=error,
    )


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


@app.route("/api/wa/<key>/pairing")
def wa_pairing_status(key):
    """Read-only live view of a phone-link pairing code (never starts a session).

    The payload contains a one-time login code, so it stays behind the
    dashboard's own authentication — exactly like the QR frame beside it.
    """
    try:
        return jsonify(_wa_hub_get(f"/sessions/{key}/pairing"))
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "code": "", "hasCode": False})


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
