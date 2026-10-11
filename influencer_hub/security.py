"""Access control for the dashboard: two roles, real limits, and an audit trail.

The hub has exactly two kinds of people:

* the **admin** (the owner) — signs in with the admin password and, when
  ``ADMIN_TOTP_SECRET`` is set, a six-digit code from an authenticator app. Only
  the admin may change anything: edit a creator, delete a channel, touch the
  vault.
* the **team** — signs in on their own page with their own password (never the
  admin one) and may only *add* creators and look at what they added. Editing is
  not hidden from them, it is refused: every route outside the team page is
  rejected before a view runs, so a bookmarked admin URL, a crafted form post or
  an API call all end the same way.

Everything here is plain standard library — :func:`totp_code` is RFC 6238 built
on :mod:`hmac`, so no new dependency enters a verified lock file.

Failed sign-ins are counted in the database (not in memory), so a restart does
not hand an attacker a fresh budget, and every meaningful action is written to
the audit log with the actor's name, their role and the client IP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import datetime, timezone

from . import config, db

ADMIN = "admin"
TEAM = "team"

# How long a burst of failures blocks one IP and how many are allowed.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 5
TEAM_ADDS_PER_HOUR = 120

# Endpoints the team role may reach. Everything else needs the admin role, so a
# new route is private by default — the safe direction for a mistake.
TEAM_ALLOWED_ENDPOINTS = {
    "static", "healthz", "logout",
    "team_login", "team_home", "team_add", "team_creators",
    "amazon_short_link", "meesho_hypd_short_link", "lehlah_meesho_short_link",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------
# Passwords (constant-time, salted digest so a length difference is invisible)
# --------------------------------------------------------------------------
def password_digest(password: str, *, salt: str = "dashboard") -> str:
    """A stable, salted SHA-256 digest used only for constant-time comparison."""
    return hashlib.sha256(f"{salt}\x00{password}".encode("utf-8")).hexdigest()


def password_matches(supplied: str, expected: str, *, salt: str = "dashboard") -> bool:
    expected = str(expected or "")
    if not expected:
        return False
    return hmac.compare_digest(
        password_digest(str(supplied or ""), salt=salt),
        password_digest(expected, salt=salt),
    )


def admin_password_matches(supplied: str) -> bool:
    return password_matches(supplied, config.DASHBOARD_ADMIN_PASSWORD, salt="admin")


def team_access_configured() -> bool:
    return bool(str(config.DASHBOARD_TEAM_PASSWORD or "").strip())


def team_password_matches(supplied: str) -> bool:
    return password_matches(supplied, config.DASHBOARD_TEAM_PASSWORD, salt="team")


def strong_password(value: str, *, minimum: int = 12) -> bool:
    """A quick strength floor for the credentials an operator sets themselves."""
    text = str(value or "")
    return len(text) >= minimum and len(set(text)) >= 6


# --------------------------------------------------------------------------
# Two-factor: RFC 6238 TOTP over the standard library
# --------------------------------------------------------------------------
def new_totp_secret(length: int = 20) -> str:
    """A fresh base32 secret (160 bits by default) for an authenticator app."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def normalize_totp_secret(secret: str) -> bytes:
    text = str(secret or "").strip().replace(" ", "").upper()
    padding = "=" * (-len(text) % 8)
    return base64.b32decode(text + padding, casefold=True)


def totp_code(secret: str, *, at: float | None = None, step: int = 30,
              digits: int = 6) -> str:
    """The six-digit code for this moment (RFC 6238, HMAC-SHA1)."""
    counter = int((time.time() if at is None else at) // step)
    key = normalize_totp_secret(secret)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def totp_enabled() -> bool:
    return bool(str(config.ADMIN_TOTP_SECRET or "").strip())


def verify_totp(code: str, *, secret: str | None = None, at: float | None = None,
                window: int = 1) -> bool:
    """Check a code, allowing one step of clock drift either way."""
    secret = secret if secret is not None else config.ADMIN_TOTP_SECRET
    if not str(secret or "").strip():
        return False
    supplied = str(code or "").strip().replace(" ", "")
    if not supplied.isdigit():
        return False
    moment = time.time() if at is None else at
    try:
        return any(
            hmac.compare_digest(supplied, totp_code(secret, at=moment + drift * 30))
            for drift in range(-window, window + 1)
        )
    except (ValueError, TypeError):
        return False


def totp_uri(secret: str, *, account: str = "admin", issuer: str = "Influencer Hub") -> str:
    """The ``otpauth://`` URL an authenticator app scans/pastes."""
    from urllib.parse import quote

    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}"
            f"?secret={str(secret or '').strip().upper()}&issuer={quote(issuer)}&digits=6&period=30")


# --------------------------------------------------------------------------
# Throttling, kept in the database so a restart is not a fresh budget
# --------------------------------------------------------------------------
def block_state(scope: str, ip: str, *, window_seconds: int = LOGIN_WINDOW_SECONDS,
                max_failures: int = LOGIN_MAX_FAILURES) -> dict:
    """How many recent failures this IP has, and whether it must wait."""
    failures = db.count_login_failures(scope, ip, window_seconds=window_seconds)
    blocked = failures >= max_failures
    return {
        "failures": failures,
        "blocked": blocked,
        "max_failures": max_failures,
        "retry_after_minutes": (window_seconds // 60) if blocked else 0,
    }


def note_login(scope: str, ip: str, ok: bool) -> None:
    """Record one attempt; a success clears the IP's failure budget."""
    try:
        if ok:
            db.clear_login_failures(scope, ip)
        else:
            db.note_login_attempt(scope, ip, ok=False)
    except Exception:  # pragma: no cover - a broken counter must not lock anyone out
        pass


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def audit(actor: str, role: str, action: str, *, target: str = "", detail: str = "",
          ip: str = "") -> None:
    """Write one line of the who-did-what log (never raises)."""
    try:
        db.record_audit(actor=str(actor or role or "unknown")[:60], role=role,
                        action=action, target=str(target or "")[:120],
                        detail=str(detail or "")[:400], ip=str(ip or "")[:60])
    except Exception:  # pragma: no cover - auditing must never break a request
        pass


def team_adds_last_hour(actor: str) -> int:
    try:
        return db.count_audit_since("creator_added", actor=actor, hours=1)
    except Exception:  # pragma: no cover - defensive
        return 0


def team_quota(actor: str, limit: int = TEAM_ADDS_PER_HOUR) -> dict:
    used = team_adds_last_hour(actor)
    return {"limit": limit, "used": used, "remaining": max(0, limit - used),
            "allowed": used < limit}
