"""SQLite persistence for the influencer hub.

Schema is intentionally small and explicit (no ORM) so it can be inspected and
backed up trivially on the VM. All write helpers are idempotent where it makes
sense (e.g. upsert by natural key).
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, parse_qsl, urlparse

from . import config

Schema = """
CREATE TABLE IF NOT EXISTS influencers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    handle            TEXT NOT NULL DEFAULT '',
    amazon_tag        TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 1,
    use_dummy_sources INTEGER NOT NULL DEFAULT 0,
    telegram_enabled  INTEGER NOT NULL DEFAULT 1,
    whatsapp_enabled  INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    platform      TEXT NOT NULL,   -- 'telegram' | 'whatsapp_group' | 'whatsapp_channel'
    identifier    TEXT NOT NULL,   -- tg @username / wa group jid / newsletter jid
    role          TEXT NOT NULL DEFAULT 'broadcast',  -- 'approval' | 'broadcast' | 'whatsapp'
    invite_link   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|ready|error
    wa_session_key TEXT NOT NULL DEFAULT '',        -- custom WA session for multi-account isolation
    bitly_api_key  TEXT NOT NULL DEFAULT '',        -- per-channel custom Bitly token
    categories    TEXT NOT NULL DEFAULT '',        -- e.g. 'clothing,electronics,home' (empty = all)
    posting_schedule TEXT NOT NULL DEFAULT '',     -- e.g. '06:00-09:00,18:00-23:00' (empty = 24/7)
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wa_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    phone         TEXT NOT NULL DEFAULT '',
    session_key   TEXT NOT NULL,   -- key used by wa_hub
    status        TEXT NOT NULL DEFAULT 'offline', -- offline|qr|connected|error
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sources (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'production', -- 'production' | 'dummy'
    spec    TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    influencer_id INTEGER NOT NULL REFERENCES influencers(id) ON DELETE CASCADE,
    channel_id    INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    deal_sig      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued', -- queued|posted|failed|skipped
    error         TEXT NOT NULL DEFAULT '',
    posted_at     TEXT
);

CREATE TABLE IF NOT EXISTS vm_stats (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL DEFAULT (datetime('now')),
    cpu_pct   REAL NOT NULL DEFAULT 0,
    mem_pct   REAL NOT NULL DEFAULT 0,
    disk_pct  REAL NOT NULL DEFAULT 0,
    bot_running INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS global_settings (
    key   TEXT PRIMARY KEY,
    val   TEXT NOT NULL DEFAULT ''
);

-- Durable, cross-process WhatsApp send reservations. The worker and dashboard
-- share one SQLite row per WhatsApp session so restarts and parallel send paths
-- cannot independently reset the conservative pacing schedule.
CREATE TABLE IF NOT EXISTS wa_pacing_state (
    session_key       TEXT PRIMARY KEY,
    window_started_at REAL NOT NULL,
    next_allowed_at   REAL NOT NULL,
    posts_in_window   INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A small heartbeat lets the dashboard distinguish a configured worker from
-- a live worker. The singleton row is refreshed by the worker's own event loop.
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    singleton        INTEGER PRIMARY KEY CHECK (singleton = 1),
    pid              INTEGER NOT NULL,
    state            TEXT NOT NULL,
    started_at       REAL NOT NULL,
    heartbeat_at     REAL NOT NULL,
    last_poll_at     REAL NOT NULL DEFAULT 0,
    last_error_code  TEXT NOT NULL DEFAULT ''
);

-- Durable per-dialog cursor for the continuous worker. A cursor advances only
-- after a message has been successfully handled (or intentionally filtered).
CREATE TABLE IF NOT EXISTS worker_offsets (
    source_key       TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- First-party, auditable Amazon redirects. The short public URL still carries
-- the creator's Associates tag; targets are restricted in the redirect route.
CREATE TABLE IF NOT EXISTS amazon_short_links (
    code          TEXT PRIMARY KEY,
    target_url    TEXT NOT NULL UNIQUE,
    associate_tag TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- First-party vanity redirects for valid HYPD affiliate URLs. The stored
-- destination is restricted to hypd.store and revalidated before redirect.
CREATE TABLE IF NOT EXISTS hypd_short_links (
    code          TEXT PRIMARY KEY,
    target_url    TEXT NOT NULL UNIQUE,
    store_id      TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Preserve existing LehLah/AppsFlyer attribution on Meesho product URLs.
CREATE TABLE IF NOT EXISTS lehlah_short_links (
    code          TEXT PRIMARY KEY,
    target_url    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Engagement polls are manually created and globally de-duplicated by a
-- normalized question. The creator id intentionally has no FK so deleting an
-- influencer cannot erase question history and permit a repeated poll later.
CREATE TABLE IF NOT EXISTS polls (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_by_influencer_id   INTEGER NOT NULL,
    question                   TEXT NOT NULL,
    normalized_question        TEXT NOT NULL UNIQUE,
    options_json               TEXT NOT NULL,
    allow_multiple             INTEGER NOT NULL DEFAULT 0,
    created_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One durable queued/send record per poll/destination. Once claimed, a delivery
-- is never retried automatically: a timeout can happen after the platform
-- accepted a poll, so a retry could create a visible duplicate.
CREATE TABLE IF NOT EXISTS poll_deliveries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id              INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    channel_id           INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    status               TEXT NOT NULL DEFAULT 'queued', -- queued|sending|posted|failed|skipped
    error                TEXT NOT NULL DEFAULT '',
    platform_message_id  TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(poll_id, channel_id)
);

CREATE INDEX IF NOT EXISTS idx_channels_influencer ON channels(influencer_id);
CREATE INDEX IF NOT EXISTS idx_posts_sig ON posts(deal_sig, influencer_id);
CREATE INDEX IF NOT EXISTS idx_wa_influencer ON wa_sessions(influencer_id);
CREATE INDEX IF NOT EXISTS idx_polls_creator_created ON polls(created_by_influencer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_poll_deliveries_poll ON poll_deliveries(poll_id);
"""


def _as_bool(value: Any, default: bool = False, unrestricted: bool = False) -> bool:
    """Normalize form/env/database boolean-like values without truthy-string bugs."""
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "default"}:
            return default
        if normalized in {"all", "any", "unrestricted", "*", "no_filter"}:
            return unrestricted
        if normalized in {"1", "true", "yes", "on", "enabled", "active"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled", "none", "null"}:
            return False
    return bool(value)


def _connect() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    con.row_factory = sqlite3.Row
    # busy_timeout and synchronous are connection-local; WAL mode is enabled
    # once by init(), avoiding a journal-mode lock on every dashboard request.
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def init() -> None:
    """Create the schema (idempotent). Call once at startup."""
    con = _connect()
    try:
        # WAL permits readers in the Flask UI while the worker records posts.
        # Apply it during initialization only, not on every short-lived connection.
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(Schema)
        con.commit()
    finally:
        con.close()
    migrate()


def migrate() -> None:
    """Add columns introduced after the first deploy (safe to re-run)."""
    con = _connect()
    try:
        inf_cols = {r["name"] for r in con.execute("PRAGMA table_info(influencers)")}
        for col, ddl in (
            ("telegram_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("whatsapp_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("insta_id", "TEXT NOT NULL DEFAULT ''"),
            ("phone_number", "TEXT NOT NULL DEFAULT ''"),
            ("price_filter", "TEXT NOT NULL DEFAULT 'all'"),
            ("allowed_sources", "TEXT NOT NULL DEFAULT ''"),
            ("bitly_api_key", "TEXT NOT NULL DEFAULT ''"),
            ("categories", "TEXT NOT NULL DEFAULT ''"),
            ("posting_schedule", "TEXT NOT NULL DEFAULT ''"),
            ("only_amazon", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_amazon", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_earnkaro", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_hypd", "INTEGER NOT NULL DEFAULT 1"),
            ("hypd_store_id", "TEXT NOT NULL DEFAULT '93944'"),
            ("custom_button_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("custom_button_text", "TEXT NOT NULL DEFAULT ''"),
            ("custom_button_url", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in inf_cols:
                try:
                    con.execute(f"ALTER TABLE influencers ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as exc:
                    # Two app processes may initialize the same DB at once.
                    # Treat only the race where another process added this exact
                    # column as success; surface every other migration failure.
                    if "duplicate column name" not in str(exc).lower():
                        raise

        ch_cols = {r["name"] for r in con.execute("PRAGMA table_info(channels)")}
        for col, ddl in (
            ("role", "TEXT NOT NULL DEFAULT 'broadcast'"),
            ("amazon_override_tag", "TEXT NOT NULL DEFAULT ''"),
            ("strip_amazon", "INTEGER NOT NULL DEFAULT 0"),
            ("price_filter", "TEXT NOT NULL DEFAULT ''"),
            ("allowed_sources", "TEXT NOT NULL DEFAULT ''"),
            ("wa_session_key", "TEXT NOT NULL DEFAULT ''"),
            ("bitly_api_key", "TEXT NOT NULL DEFAULT ''"),
            ("categories", "TEXT NOT NULL DEFAULT ''"),
            ("posting_schedule", "TEXT NOT NULL DEFAULT ''"),
            ("only_amazon", "INTEGER NOT NULL DEFAULT 0"),
            ("allow_amazon", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_earnkaro", "INTEGER NOT NULL DEFAULT 1"),
            ("allow_hypd", "INTEGER NOT NULL DEFAULT 1"),
            ("hypd_store_id", "TEXT NOT NULL DEFAULT '93944'"),
            ("custom_button_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("custom_button_text", "TEXT NOT NULL DEFAULT ''"),
            ("custom_button_url", "TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in ch_cols:
                try:
                    con.execute(f"ALTER TABLE channels ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise

        posts_cols = {r["name"] for r in con.execute("PRAGMA table_info(posts)")}
        if "deal_text" not in posts_cols:
            try:
                con.execute("ALTER TABLE posts ADD COLUMN deal_text TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise

        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------- influencers -----------------------------

def add_influencer(name: str, amazon_tag: str = config.AMAZON_ASSOCIATE_TAG,
                   handle: str = "", notes: str = "",
                   use_dummy_sources: bool = False,
                   telegram_enabled: bool = True, whatsapp_enabled: bool = True,
                   insta_id: str = "", phone_number: str = "",
                   price_filter: str = "all", allowed_sources: str = "",
                   bitly_api_key: str = "", categories: str = "",
                   posting_schedule: str = "", only_amazon: bool = False,
                   allow_amazon: bool = True, allow_earnkaro: bool = True,
                   allow_hypd: bool = True, hypd_store_id: str | None = None) -> int:
    # Ensure schema exists for fresh DBs (pytest tmp_path without explicit init)
    try:
        init()
    except Exception:
        pass
    con = _connect()
    try:
        try:
            global_hypd = con.execute(
                "SELECT val FROM global_settings WHERE key='hypd_store_id'"
            ).fetchone()
        except Exception:
            global_hypd = None
        effective_hypd_store_id = (
            str(hypd_store_id or "").strip()
            or (str(global_hypd["val"] or "").strip() if global_hypd else "")
            or config.HYPD_STORE_ID
        )
        cur = con.execute(
            "INSERT INTO influencers "
            "(name, handle, amazon_tag, notes, use_dummy_sources, telegram_enabled, whatsapp_enabled, insta_id, phone_number, price_filter, allowed_sources, bitly_api_key, categories, posting_schedule, only_amazon, allow_amazon, allow_earnkaro, allow_hypd, hypd_store_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), handle.strip(), (amazon_tag or "").strip() or config.AMAZON_ASSOCIATE_TAG,
             notes.strip(), 1 if _as_bool(use_dummy_sources) else 0,
             1 if _as_bool(telegram_enabled, default=True) else 0,
             1 if _as_bool(whatsapp_enabled, default=True) else 0, insta_id.strip(),
             phone_number.strip(), price_filter.strip() or "all", allowed_sources.strip(), bitly_api_key.strip(),
             categories.strip(), posting_schedule.strip(), 1 if _as_bool(only_amazon) else 0,
             1 if _as_bool(allow_amazon, default=True, unrestricted=True) else 0,
             1 if _as_bool(allow_earnkaro, default=True, unrestricted=True) else 0,
             1 if _as_bool(allow_hypd, default=True, unrestricted=True) else 0,
             effective_hypd_store_id),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def update_influencer(influencer_id: int, name: str | None = None,
                      amazon_tag: str | None = None, handle: str | None = None,
                      insta_id: str | None = None, phone_number: str | None = None,
                      price_filter: str | None = None, allowed_sources: str | None = None,
                      bitly_api_key: str | None = None, categories: str | None = None,
                      posting_schedule: str | None = None, only_amazon: bool | None = None,
                      allow_amazon: bool | None = None, allow_earnkaro: bool | None = None,
                      allow_hypd: bool | None = None, hypd_store_id: str | None = None,
                      custom_button_enabled: bool | None = None,
                      custom_button_text: str | None = None,
                      custom_button_url: str | None = None,
                      notes: str | None = None, active: bool | None = None) -> None:
    con = _connect()
    try:
        updates = []
        params = []
        if name is not None:
            updates.append("name=?")
            params.append(name.strip())
        if amazon_tag is not None:
            updates.append("amazon_tag=?")
            params.append(amazon_tag.strip() or config.AMAZON_ASSOCIATE_TAG)
        if handle is not None:
            updates.append("handle=?")
            params.append(handle.strip())
        if insta_id is not None:
            updates.append("insta_id=?")
            params.append(insta_id.strip())
        if phone_number is not None:
            updates.append("phone_number=?")
            params.append(phone_number.strip())
        if price_filter is not None:
            updates.append("price_filter=?")
            params.append(price_filter.strip())
        if allowed_sources is not None:
            updates.append("allowed_sources=?")
            params.append(allowed_sources.strip())
        if bitly_api_key is not None:
            updates.append("bitly_api_key=?")
            params.append(bitly_api_key.strip())
        if categories is not None:
            updates.append("categories=?")
            params.append(categories.strip())
        if posting_schedule is not None:
            updates.append("posting_schedule=?")
            params.append(posting_schedule.strip())
        if only_amazon is not None:
            updates.append("only_amazon=?")
            params.append(1 if _as_bool(only_amazon) else 0)
        if allow_amazon is not None:
            updates.append("allow_amazon=?")
            params.append(1 if _as_bool(allow_amazon, default=True, unrestricted=True) else 0)
        if allow_earnkaro is not None:
            updates.append("allow_earnkaro=?")
            params.append(1 if _as_bool(allow_earnkaro, default=True, unrestricted=True) else 0)
        if allow_hypd is not None:
            updates.append("allow_hypd=?")
            params.append(1 if _as_bool(allow_hypd, default=True, unrestricted=True) else 0)
        if hypd_store_id is not None:
            updates.append("hypd_store_id=?")
            params.append(hypd_store_id.strip())
        if custom_button_enabled is not None:
            updates.append("custom_button_enabled=?")
            params.append(1 if _as_bool(custom_button_enabled) else 0)
        if custom_button_text is not None:
            updates.append("custom_button_text=?")
            params.append(custom_button_text.strip())
        if custom_button_url is not None:
            updates.append("custom_button_url=?")
            params.append(custom_button_url.strip())
        if notes is not None:
            updates.append("notes=?")
            params.append(notes.strip())
        if active is not None:
            updates.append("active=?")
            params.append(1 if _as_bool(active) else 0)
        if updates:
            params.append(influencer_id)
            con.execute(f"UPDATE influencers SET {', '.join(updates)} WHERE id=?", params)
            con.commit()
    finally:
        con.close()


def update_inherited_channel_hypd_store_ids(
    influencer_id: int, previous_store_id: str, new_store_id: str
) -> None:
    """Propagate a profile Store ID change to channels using the old profile value.

    Distinct channel-specific Store IDs are left intact. A channel whose ID
    matched the previous profile default is treated as inheriting that value.
    """
    previous = (previous_store_id or "").strip()
    new = (new_store_id or "").strip()
    if previous == new:
        return
    con = _connect()
    try:
        con.execute(
            "UPDATE channels SET hypd_store_id=? WHERE influencer_id=? "
            "AND (TRIM(COALESCE(hypd_store_id, ''))='' OR TRIM(hypd_store_id)=?)",
            (new, influencer_id, previous),
        )
        con.commit()
    finally:
        con.close()


def set_channel_flags(influencer_id: int, telegram_enabled: bool | None = None,
                      whatsapp_enabled: bool | None = None) -> None:
    con = _connect()
    try:
        if telegram_enabled is not None:
            con.execute("UPDATE influencers SET telegram_enabled=? WHERE id=?",
                        (1 if _as_bool(telegram_enabled, default=True) else 0, influencer_id))
        if whatsapp_enabled is not None:
            con.execute("UPDATE influencers SET whatsapp_enabled=? WHERE id=?",
                        (1 if _as_bool(whatsapp_enabled, default=True) else 0, influencer_id))
        con.commit()
    finally:
        con.close()


def get_influencer(influencer_id: int) -> Optional[dict]:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM influencers WHERE id=?", (influencer_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_influencers(active_only: bool = False) -> list[dict]:
    con = _connect()
    try:
        sql = "SELECT * FROM influencers"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        return [dict(r) for r in con.execute(sql).fetchall()]
    finally:
        con.close()


def set_influencer_active(influencer_id: int, active: bool) -> None:
    con = _connect()
    try:
        con.execute("UPDATE influencers SET active=? WHERE id=?",
                    (1 if _as_bool(active) else 0, influencer_id))
        con.commit()
    finally:
        con.close()


def set_use_dummy_sources(influencer_id: int, use_dummy: bool) -> None:
    con = _connect()
    try:
        con.execute("UPDATE influencers SET use_dummy_sources=? WHERE id=?",
                    (1 if _as_bool(use_dummy) else 0, influencer_id))
        con.commit()
    finally:
        con.close()


# ------------------------------ channels -------------------------------

def add_channel(influencer_id: int, platform: str, identifier: str,
                invite_link: str = "", status: str = "pending",
                role: str = "broadcast",
                amazon_override_tag: str = "",
                strip_amazon: bool = False,
                price_filter: str = "",
                allowed_sources: str = "",
                wa_session_key: str = "",
                bitly_api_key: str = "",
                categories: str = "",
                posting_schedule: str = "",
                only_amazon: bool = False,
                allow_amazon: bool = True,
                allow_earnkaro: bool = True,
                allow_hypd: bool = True,
                hypd_store_id: str | None = None,
                custom_button_enabled: bool = False,
                custom_button_text: str = "",
                custom_button_url: str = "") -> int:
    con = _connect()
    try:
        profile = con.execute(
            "SELECT hypd_store_id FROM influencers WHERE id=?", (influencer_id,)
        ).fetchone()
        profile_store_id = (
            str(profile["hypd_store_id"] or "").strip() if profile else ""
        )
        effective_hypd_store_id = (
            str(hypd_store_id or "").strip()
            or profile_store_id
            or config.HYPD_STORE_ID
        )
        cur = con.execute(
            "INSERT INTO channels (influencer_id, platform, identifier, invite_link, status, role, amazon_override_tag, strip_amazon, price_filter, allowed_sources, wa_session_key, bitly_api_key, categories, posting_schedule, only_amazon, allow_amazon, allow_earnkaro, allow_hypd, hypd_store_id, custom_button_enabled, custom_button_text, custom_button_url) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (influencer_id, platform, identifier.strip(), invite_link.strip(), status, role,
             amazon_override_tag.strip(), 1 if _as_bool(strip_amazon, unrestricted=False) else 0, price_filter.strip(), allowed_sources.strip(),
             wa_session_key.strip(), bitly_api_key.strip(), categories.strip(), posting_schedule.strip(),
             1 if _as_bool(only_amazon) else 0,
             1 if _as_bool(allow_amazon, default=True, unrestricted=True) else 0,
             1 if _as_bool(allow_earnkaro, default=True, unrestricted=True) else 0,
             1 if _as_bool(allow_hypd, default=True, unrestricted=True) else 0,
             effective_hypd_store_id,
             1 if _as_bool(custom_button_enabled) else 0, custom_button_text.strip(), custom_button_url.strip()),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def update_channel_details(channel_id: int, identifier: str | None = None,
                           role: str | None = None, invite_link: str | None = None,
                           status: str | None = None,
                           amazon_override_tag: str | None = None,
                           strip_amazon: bool | None = None,
                           price_filter: str | None = None,
                           allowed_sources: str | None = None,
                           wa_session_key: str | None = None,
                           bitly_api_key: str | None = None,
                           categories: str | None = None,
                           posting_schedule: str | None = None,
                           only_amazon: bool | None = None,
                           allow_amazon: bool | None = None,
                           allow_earnkaro: bool | None = None,
                           allow_hypd: bool | None = None,
                           hypd_store_id: str | None = None,
                           custom_button_enabled: bool | None = None,
                           custom_button_text: str | None = None,
                           custom_button_url: str | None = None) -> None:
    con = _connect()
    try:
        updates = []
        params = []
        if identifier is not None:
            updates.append("identifier=?")
            params.append(identifier.strip())
        if role is not None:
            updates.append("role=?")
            params.append(role.strip())
        if invite_link is not None:
            updates.append("invite_link=?")
            params.append(invite_link.strip())
        if status is not None:
            updates.append("status=?")
            params.append(status.strip())
        if amazon_override_tag is not None:
            updates.append("amazon_override_tag=?")
            params.append(amazon_override_tag.strip())
        if strip_amazon is not None:
            updates.append("strip_amazon=?")
            params.append(1 if _as_bool(strip_amazon, unrestricted=False) else 0)
        if price_filter is not None:
            updates.append("price_filter=?")
            params.append(price_filter.strip())
        if allowed_sources is not None:
            updates.append("allowed_sources=?")
            params.append(allowed_sources.strip())
        if wa_session_key is not None:
            updates.append("wa_session_key=?")
            params.append(wa_session_key.strip())
        if bitly_api_key is not None:
            updates.append("bitly_api_key=?")
            params.append(bitly_api_key.strip())
        if categories is not None:
            updates.append("categories=?")
            params.append(categories.strip())
        if posting_schedule is not None:
            updates.append("posting_schedule=?")
            params.append(posting_schedule.strip())
        if only_amazon is not None:
            updates.append("only_amazon=?")
            params.append(1 if _as_bool(only_amazon) else 0)
        if allow_amazon is not None:
            updates.append("allow_amazon=?")
            params.append(1 if _as_bool(allow_amazon, default=True, unrestricted=True) else 0)
        if allow_earnkaro is not None:
            updates.append("allow_earnkaro=?")
            params.append(1 if _as_bool(allow_earnkaro, default=True, unrestricted=True) else 0)
        if allow_hypd is not None:
            updates.append("allow_hypd=?")
            params.append(1 if _as_bool(allow_hypd, default=True, unrestricted=True) else 0)
        if hypd_store_id is not None:
            updates.append("hypd_store_id=?")
            params.append(hypd_store_id.strip())
        if custom_button_enabled is not None:
            updates.append("custom_button_enabled=?")
            params.append(1 if _as_bool(custom_button_enabled) else 0)
        if custom_button_text is not None:
            updates.append("custom_button_text=?")
            params.append(custom_button_text.strip())
        if custom_button_url is not None:
            updates.append("custom_button_url=?")
            params.append(custom_button_url.strip())
        if updates:
            params.append(channel_id)
            con.execute(f"UPDATE channels SET {', '.join(updates)} WHERE id=?", params)
            con.commit()
    finally:
        con.close()


def search_influencers(query: str = "") -> list[dict]:
    """Search influencers by ID, Phone Number, Name, Instagram ID, Amazon Tag, or Handle."""
    q = query.strip()
    if not q:
        return list_influencers()
    con = _connect()
    try:
        sql = """
        SELECT * FROM influencers
        WHERE id = ?
           OR phone_number LIKE ?
           OR name LIKE ?
           OR insta_id LIKE ?
           OR amazon_tag LIKE ?
           OR handle LIKE ?
        ORDER BY id
        """
        id_val = int(q) if q.isdigit() else -1
        like_val = f"%{q}%"
        rows = con.execute(sql, (id_val, like_val, like_val, like_val, like_val, like_val)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_channel(influencer_id: int, role: str, platform: str | None = None) -> dict | None:
    con = _connect()
    try:
        if platform:
            row = con.execute(
                "SELECT * FROM channels WHERE influencer_id=? AND role=? AND platform=? "
                "ORDER BY id DESC LIMIT 1",
                (influencer_id, role, platform)).fetchone()
        else:
            row = con.execute(
                "SELECT * FROM channels WHERE influencer_id=? AND role=? "
                "ORDER BY id DESC LIMIT 1",
                (influencer_id, role)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_channels(influencer_id: Optional[int] = None) -> list[dict]:
    con = _connect()
    try:
        if influencer_id is None:
            return [dict(r) for r in con.execute("SELECT * FROM channels ORDER BY id").fetchall()]
        return [dict(r) for r in con.execute(
            "SELECT * FROM channels WHERE influencer_id=? ORDER BY id", (influencer_id,)).fetchall()]
    finally:
        con.close()


def delete_channel(channel_id: int) -> None:
    con = _connect()
    try:
        con.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        con.commit()
    finally:
        con.close()


def delete_influencer(influencer_id: int) -> None:
    con = _connect()
    try:
        con.execute("DELETE FROM influencers WHERE id=?", (influencer_id,))
        con.commit()
    finally:
        con.close()


def _poll_questions_near_duplicate(existing: str, candidate: str) -> bool:
    """Catch high-confidence rewordings without blocking short/common prompts."""
    if not existing or not candidate:
        return False
    if existing == candidate:
        return True
    if min(len(existing), len(candidate)) < 28:
        return False
    existing_words = set(existing.split())
    candidate_words = set(candidate.split())
    if min(len(existing_words), len(candidate_words)) < 4:
        return False
    word_overlap = len(existing_words & candidate_words) / max(
        len(existing_words), len(candidate_words)
    )
    char_similarity = difflib.SequenceMatcher(
        None, existing, candidate, autojunk=False
    ).ratio()
    return word_overlap >= 0.78 and char_similarity >= 0.86


def create_poll_question(
    influencer_id: int,
    question: str,
    normalized_question: str,
    options: list[str],
    allow_multiple: bool = False,
    channel_ids: Iterable[int] = (),
) -> dict:
    """Atomically save a globally unique poll question.

    The normalized string is unique across all profiles and destinations. A
    conservative fuzzy check also blocks near-identical long prompts, while
    intentionally leaving short/common prompts alone to avoid false positives.
    """
    question = str(question or "").strip()
    normalized = str(normalized_question or "").strip()
    if not question or not normalized:
        raise ValueError("A poll question is required")
    if len(options) < 2:
        raise ValueError("A poll needs at least two answer options")

    options_json = json.dumps([str(option) for option in options], ensure_ascii=False)
    destination_ids = list(dict.fromkeys(int(channel_id) for channel_id in channel_ids))
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        for channel_id in destination_ids:
            owner = con.execute(
                "SELECT influencer_id FROM channels WHERE id=?", (channel_id,)
            ).fetchone()
            if not owner or int(owner["influencer_id"]) != int(influencer_id):
                raise ValueError("Poll destinations must belong to the selected influencer")

        rows = con.execute(
            "SELECT id, question, normalized_question FROM polls ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            prior_normalized = str(row["normalized_question"] or "")
            exact = prior_normalized == normalized
            near = not exact and _poll_questions_near_duplicate(prior_normalized, normalized)
            if exact or near:
                con.rollback()
                return {
                    "created": False,
                    "poll_id": int(row["id"]),
                    "existing_question": str(row["question"]),
                    "duplicate_kind": "exact" if exact else "similar",
                }

        created_at = _now()
        cur = con.execute(
            "INSERT INTO polls "
            "(created_by_influencer_id, question, normalized_question, options_json, allow_multiple, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                int(influencer_id), question, normalized, options_json,
                1 if _as_bool(allow_multiple, default=False) else 0, created_at,
            ),
        )
        poll_id = int(cur.lastrowid)
        con.executemany(
            "INSERT INTO poll_deliveries "
            "(poll_id, channel_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            [(poll_id, channel_id, created_at, created_at) for channel_id in destination_ids],
        )
        con.commit()
        return {
            "created": True,
            "poll_id": poll_id,
            "destination_count": len(destination_ids),
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def claim_next_poll_delivery() -> dict | None:
    """Atomically move the oldest queued poll destination to ``sending``."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT d.id AS delivery_id, d.poll_id, d.channel_id, "
            "p.question, p.options_json, p.allow_multiple, "
            "c.influencer_id AS channel_influencer_id, c.platform AS channel_platform, "
            "c.identifier AS channel_identifier, c.role AS channel_role, "
            "c.status AS channel_status, c.wa_session_key, "
            "c.posting_schedule AS channel_posting_schedule, "
            "i.active AS influencer_active, i.telegram_enabled AS influencer_telegram_enabled, "
            "i.whatsapp_enabled AS influencer_whatsapp_enabled, "
            "i.posting_schedule AS influencer_posting_schedule "
            "FROM poll_deliveries d "
            "JOIN polls p ON p.id=d.poll_id "
            "JOIN channels c ON c.id=d.channel_id "
            "JOIN influencers i ON i.id=c.influencer_id "
            "WHERE d.status='queued' ORDER BY d.id LIMIT 1"
        ).fetchone()
        if not row:
            con.commit()
            return None

        cur = con.execute(
            "UPDATE poll_deliveries SET status='sending', updated_at=? "
            "WHERE id=? AND status='queued'",
            (_now(), int(row["delivery_id"])),
        )
        if cur.rowcount != 1:
            con.rollback()
            return None
        con.commit()
        result = dict(row)
        try:
            result["options"] = json.loads(result.pop("options_json"))
        except (TypeError, json.JSONDecodeError):
            result["options"] = []
            result.pop("options_json", None)
        return result
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def finish_poll_delivery(
    poll_id: int,
    channel_id: int,
    status: str,
    *,
    error: str = "",
    platform_message_id: str = "",
) -> None:
    """Persist a final delivery result; failures are not blindly retried."""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"posted", "failed", "skipped"}:
        raise ValueError("Poll delivery status must be posted, failed, or skipped")
    con = _connect()
    try:
        con.execute(
            "UPDATE poll_deliveries SET status=?, error=?, platform_message_id=?, updated_at=? "
            "WHERE poll_id=? AND channel_id=?",
            (
                normalized_status, str(error or "")[:500],
                str(platform_message_id or "")[:200], _now(),
                int(poll_id), int(channel_id),
            ),
        )
        con.commit()
    finally:
        con.close()


def list_recent_polls(influencer_id: int, limit: int = 8) -> list[dict]:
    """Return an influencer's newest poll records with aggregate send outcomes."""
    safe_limit = max(1, min(50, int(limit)))
    con = _connect()
    try:
        rows = con.execute(
            "SELECT p.id, p.question, p.options_json, p.allow_multiple, p.created_at, "
            "COUNT(d.id) AS destination_count, "
            "COALESCE(SUM(CASE WHEN d.status='posted' THEN 1 ELSE 0 END), 0) AS posted_count, "
            "COALESCE(SUM(CASE WHEN d.status='failed' THEN 1 ELSE 0 END), 0) AS failed_count, "
            "COALESCE(SUM(CASE WHEN d.status IN ('queued', 'sending') THEN 1 ELSE 0 END), 0) AS pending_count, "
            "COALESCE(SUM(CASE WHEN d.status='skipped' THEN 1 ELSE 0 END), 0) AS skipped_count, "
            "GROUP_CONCAT(CASE WHEN d.id IS NOT NULL THEN "
            "COALESCE(c.identifier, 'destination') || ': ' || d.status || "
            "CASE WHEN d.error!='' THEN ' (' || SUBSTR(d.error,1,160) || ')' ELSE '' END "
            "END, ' · ') AS destination_details "
            "FROM polls p LEFT JOIN poll_deliveries d ON d.poll_id=p.id "
            "LEFT JOIN channels c ON c.id=d.channel_id "
            "WHERE p.created_by_influencer_id=? "
            "GROUP BY p.id ORDER BY p.id DESC LIMIT ?",
            (int(influencer_id), safe_limit),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["options"] = json.loads(item.pop("options_json"))
            except (TypeError, json.JSONDecodeError):
                item["options"] = []
                item.pop("options_json", None)
            result.append(item)
        return result
    finally:
        con.close()


def update_channel(influencer_id: int, platform: str, identifier: str,
                   invite_link: str = "", status: str = "ready") -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE channels SET invite_link=?, status=? "
            "WHERE influencer_id=? AND platform=? AND identifier=?",
            (invite_link, status, influencer_id, platform, identifier),
        )
        con.commit()
    finally:
        con.close()


# ----------------------------- wa_sessions -----------------------------

def upsert_wa_session(influencer_id: int, session_key: str, phone: str = "") -> int:
    con = _connect()
    try:
        row = con.execute(
            "SELECT id FROM wa_sessions WHERE influencer_id=? AND session_key=?",
            (influencer_id, session_key)).fetchone()
        if row:
            con.execute("UPDATE wa_sessions SET phone=? WHERE id=?", (phone, row["id"]))
            con.commit()
            return int(row["id"])
        cur = con.execute(
            "INSERT INTO wa_sessions (influencer_id, session_key, phone) VALUES (?,?,?)",
            (influencer_id, session_key, phone))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def set_wa_session_status(session_key: str, status: str) -> None:
    con = _connect()
    try:
        con.execute("UPDATE wa_sessions SET status=? WHERE session_key=?", (status, session_key))
        con.commit()
    finally:
        con.close()


def list_wa_sessions(influencer_id: Optional[int] = None) -> list[dict]:
    con = _connect()
    try:
        if influencer_id is None:
            return [dict(r) for r in con.execute("SELECT * FROM wa_sessions ORDER BY id").fetchall()]
        return [dict(r) for r in con.execute(
            "SELECT * FROM wa_sessions WHERE influencer_id=? ORDER BY id",
            (influencer_id,)).fetchall()]
    finally:
        con.close()


# ------------------------------- sources -------------------------------

def add_bulk_influencers(records: list[dict]) -> int:
    """Batch insert profiles and any supplied channels in one transaction.

    Optional affiliate columns are ``allow_amazon``, ``allow_earnkaro``,
    ``allow_hypd``, ``only_amazon``, and ``hypd_store_id``. The three network
    toggles are independent; ``only_amazon`` is the explicit exclusive-mode
    override. An omitted Store ID inherits the current global default.
    """
    # Ensure schema exists for fresh DBs
    try:
        init()
    except Exception:
        pass
    con = _connect()
    count = 0
    try:
        try:
            global_hypd = con.execute(
                "SELECT val FROM global_settings WHERE key='hypd_store_id'"
            ).fetchone()
        except Exception:
            global_hypd = None
        default_hypd_store = (
            str(global_hypd["val"] or "").strip() if global_hypd else ""
        ) or config.HYPD_STORE_ID

        for r in records:
            name = str(r.get("name") or "").strip()
            if not name:
                continue
            tag = str(
                r.get("amazon_tag") or r.get("tag") or config.AMAZON_ASSOCIATE_TAG
            ).strip() or config.AMAZON_ASSOCIATE_TAG
            phone = str(r.get("phone_number") or r.get("phone") or "").strip()
            insta = str(r.get("insta_id") or r.get("insta") or "").strip()
            handle = str(r.get("handle") or "").strip()
            price_filt = str(r.get("price_filter") or "all").strip()
            sources = str(r.get("allowed_sources") or "").strip()
            strip_amz = _as_bool(r.get("strip_amazon"), default=False, unrestricted=False)
            only_amz = _as_bool(r.get("only_amazon"), default=False, unrestricted=False)
            allow_amz = _as_bool(
                r.get("allow_amazon"), default=not strip_amz, unrestricted=True
            )
            allow_ek = _as_bool(r.get("allow_earnkaro"), default=True, unrestricted=True)
            allow_hypd = _as_bool(r.get("allow_hypd"), default=True, unrestricted=True)
            hypd_store = (
                str(r.get("hypd_store_id") or "").strip() or default_hypd_store
            )
            bitly_key = str(r.get("bitly_api_key") or "").strip()
            categories = str(r.get("categories") or "").strip()
            schedule = str(r.get("posting_schedule") or "").strip()

            cur = con.execute(
                "INSERT INTO influencers "
                "(name, handle, amazon_tag, use_dummy_sources, telegram_enabled, whatsapp_enabled, "
                "insta_id, phone_number, price_filter, allowed_sources, bitly_api_key, categories, "
                "posting_schedule, only_amazon, allow_amazon, allow_earnkaro, allow_hypd, hypd_store_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, handle, tag, 0, 1, 1, insta, phone, price_filt, sources, bitly_key,
                 categories, schedule, int(only_amz), int(allow_amz), int(allow_ek),
                 int(allow_hypd), hypd_store),
            )
            iid = int(cur.lastrowid)

            # Approval channels remain explicitly Amazon-only.
            if r.get("approval_tg"):
                ident = str(r["approval_tg"]).strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, "
                    "allowed_sources, categories, posting_schedule, only_amazon, allow_amazon, "
                    "allow_earnkaro, allow_hypd, hypd_store_id) "
                    "VALUES (?,?,?,?,?,?,?,?,1,1,0,0,?)",
                    (iid, "telegram", ident, "approval", "ready", sources, categories,
                     schedule, hypd_store),
                )
            if r.get("broadcast_tg"):
                ident = str(r["broadcast_tg"]).strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, "
                    "strip_amazon, price_filter, allowed_sources, bitly_api_key, categories, "
                    "posting_schedule, only_amazon, allow_amazon, allow_earnkaro, allow_hypd, "
                    "hypd_store_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, "telegram", ident, "broadcast", "ready", int(strip_amz),
                     price_filt if price_filt != "all" else "", sources, bitly_key,
                     categories, schedule, int(only_amz), int(allow_amz), int(allow_ek),
                     int(allow_hypd), hypd_store),
                )
            if r.get("whatsapp_id"):
                ident = str(r["whatsapp_id"]).strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, "
                    "strip_amazon, price_filter, allowed_sources, wa_session_key, bitly_api_key, "
                    "categories, posting_schedule, only_amazon, allow_amazon, allow_earnkaro, "
                    "allow_hypd, hypd_store_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, "whatsapp_group", ident, "whatsapp", "pending", int(strip_amz),
                     price_filt if price_filt != "all" else "", sources,
                     str(r.get("wa_session_key") or "").strip(), bitly_key, categories,
                     schedule, int(only_amz), int(allow_amz), int(allow_ek),
                     int(allow_hypd), hypd_store),
                )
            count += 1
        con.commit()
        return count
    finally:
        con.close()


def add_source(name: str, spec: str, kind: str = "production", active: bool = True) -> int:
    """Add a deal source channel. Skips duplicate specs to avoid double polling."""
    con = _connect()
    try:
        clean_spec = spec.strip()
        existing = con.execute("SELECT id FROM sources WHERE spec=?", (clean_spec,)).fetchone()
        if existing:
            return int(existing["id"])
        cur = con.execute(
            "INSERT INTO sources (name, spec, kind, active) VALUES (?,?,?,?)",
            (name.strip(), clean_spec, kind.strip(), 1 if _as_bool(active) else 0))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def delete_source(source_id: int) -> None:
    con = _connect()
    try:
        con.execute("DELETE FROM sources WHERE id=?", (source_id,))
        con.commit()
    finally:
        con.close()


def toggle_source(source_id: int) -> None:
    con = _connect()
    try:
        con.execute("UPDATE sources SET active = CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (source_id,))
        con.commit()
    finally:
        con.close()


def list_sources(kind: Optional[str] = None, active_only: bool = True) -> list[dict]:
    con = _connect()
    try:
        sql = "SELECT * FROM sources"
        clauses: list[str] = []
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if active_only:
            clauses.append("active=1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def record_worker_heartbeat(
    state: str,
    *,
    poll_completed: bool = False,
    error_code: str = "",
) -> None:
    """Record worker lifecycle/health without persisting sensitive exception text."""
    normalized = str(state or "").strip().lower()
    if normalized not in {"starting", "running", "degraded", "stopping", "stopped"}:
        raise ValueError("Unsupported worker heartbeat state")
    pid = os.getpid()
    now = time.time()
    safe_error = str(error_code or "")[:80] if normalized == "degraded" else ""

    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT pid, started_at, last_poll_at FROM worker_heartbeat WHERE singleton=1"
        ).fetchone()
        new_instance = row is None or int(row["pid"]) != pid or normalized == "starting"
        started_at = now if new_instance else float(row["started_at"])
        last_poll_at = now if poll_completed else (float(row["last_poll_at"]) if row else 0.0)
        con.execute(
            "INSERT INTO worker_heartbeat "
            "(singleton, pid, state, started_at, heartbeat_at, last_poll_at, last_error_code) "
            "VALUES (1,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
            "pid=excluded.pid, state=excluded.state, started_at=excluded.started_at, "
            "heartbeat_at=excluded.heartbeat_at, last_poll_at=excluded.last_poll_at, "
            "last_error_code=excluded.last_error_code",
            (pid, normalized, started_at, now, last_poll_at, safe_error),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def touch_worker_heartbeat() -> bool:
    """Refresh liveness from the recorded worker process, preserving its state."""
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE worker_heartbeat SET heartbeat_at=? WHERE singleton=1 AND pid=?",
            (time.time(), os.getpid()),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def get_worker_heartbeat() -> Optional[dict]:
    """Return the worker's most recent heartbeat, if it has ever started."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT pid, state, started_at, heartbeat_at, last_poll_at, last_error_code "
            "FROM worker_heartbeat WHERE singleton=1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def get_worker_offset(source_key: str) -> int:
    """Return the last fully handled Telegram message id for a joined dialog."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT last_message_id FROM worker_offsets WHERE source_key=?",
            (str(source_key),),
        ).fetchone()
        return int(row["last_message_id"]) if row else 0
    finally:
        con.close()


def set_worker_offset(source_key: str, last_message_id: int) -> None:
    """Advance a worker cursor monotonically after successful/intentional handling."""
    key = str(source_key).strip()
    message_id = int(last_message_id)
    if not key or message_id <= 0:
        return
    con = _connect()
    try:
        con.execute(
            "INSERT INTO worker_offsets (source_key, last_message_id, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(source_key) DO UPDATE SET "
            "last_message_id=MAX(worker_offsets.last_message_id, excluded.last_message_id), "
            "updated_at=excluded.updated_at",
            (key, message_id, _now()),
        )
        con.commit()
    finally:
        con.close()


def reserve_whatsapp_send(
    session_key: str,
    gap_seconds: float,
    rest_seconds: float,
    window_seconds: float = 3600.0,
    now: float | None = None,
) -> float:
    """Atomically reserve a send slot and return seconds to wait before it.

    ``BEGIN IMMEDIATE`` serializes reservations from independent processes using
    the same SQLite database. Reservations are persisted before network I/O, so
    a crash/restart or a failed send consumes its slot conservatively rather
    than allowing another process to send immediately.
    """
    key = str(session_key or "").strip()
    if not key:
        raise ValueError("A WhatsApp session key is required for pacing")

    reserved_at = float(now) if now is not None else datetime.now(timezone.utc).timestamp()
    gap = max(0.0, float(gap_seconds))
    rest = max(0.0, float(rest_seconds))
    window = max(1.0, float(window_seconds))
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT window_started_at, next_allowed_at, posts_in_window "
            "FROM wa_pacing_state WHERE session_key=?",
            (key,),
        ).fetchone()

        if row is None:
            scheduled_at = reserved_at
            window_started_at = scheduled_at
            posts_in_window = 1
            con.execute(
                "INSERT INTO wa_pacing_state "
                "(session_key, window_started_at, next_allowed_at, posts_in_window, updated_at) "
                "VALUES (?,?,?,?,?)",
                (key, window_started_at, scheduled_at + gap, posts_in_window, _now()),
            )
        else:
            previous_window_start = float(row["window_started_at"])
            previous_next_allowed = float(row["next_allowed_at"])
            scheduled_at = max(reserved_at, previous_next_allowed)
            if scheduled_at >= previous_window_start + window:
                # Check the reserved send time, not just the current clock: a
                # busy queue must not schedule posts past the hourly boundary
                # without first reserving the break. Starting the new window
                # here prevents concurrent callers from taking the same break.
                scheduled_at += rest
                window_started_at = scheduled_at
                posts_in_window = 1
            else:
                window_started_at = previous_window_start
                posts_in_window = int(row["posts_in_window"]) + 1
            con.execute(
                "UPDATE wa_pacing_state SET window_started_at=?, next_allowed_at=?, "
                "posts_in_window=?, updated_at=? WHERE session_key=?",
                (window_started_at, scheduled_at + gap, posts_in_window, _now(), key),
            )

        con.commit()
        return max(0.0, scheduled_at - reserved_at)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _amazon_short_code(target_url: str, salt: int) -> str:
    raw = hashlib.blake2s(f"{target_url}\0{salt}".encode("utf-8"), digest_size=6).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_or_create_amazon_short_link(target_url: str, associate_tag: str) -> str:
    """Persist a stable first-party code for a tagged Amazon.in product URL.

    The public redirect endpoint separately validates the stored destination
    and requires this same tag as a query parameter, avoiding an open redirect
    and keeping the Associate ID visible on the short URL.
    """
    tag = str(associate_tag or "").strip()
    parsed = urlparse(str(target_url or ""))
    query_tags = parse_qs(parsed.query).get("tag", [])
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"amazon.in", "www.amazon.in"}
        or not parsed.path.startswith("/dp/")
        or not tag
        or query_tags != [tag]
    ):
        raise ValueError("Only canonical, tagged Amazon.in product URLs can be shortened")

    con = _connect()
    try:
        existing = con.execute(
            "SELECT code FROM amazon_short_links WHERE target_url=?", (target_url,)
        ).fetchone()
        if existing:
            return str(existing["code"])

        for salt in range(32):
            code = _amazon_short_code(target_url, salt)
            con.execute(
                "INSERT OR IGNORE INTO amazon_short_links (code, target_url, associate_tag) "
                "VALUES (?,?,?)",
                (code, target_url, tag),
            )
            existing = con.execute(
                "SELECT code FROM amazon_short_links WHERE target_url=?", (target_url,)
            ).fetchone()
            if existing:
                con.commit()
                return str(existing["code"])
            # A code collision with another target is extraordinarily unlikely;
            # try a different salted digest rather than returning a bad mapping.
        raise RuntimeError("Could not allocate a unique Amazon short-link code")
    finally:
        con.close()


def get_amazon_short_link(code: str) -> dict | None:
    """Return a stored Amazon short-link target for the public redirect route."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT target_url, associate_tag FROM amazon_short_links WHERE code=?",
            (str(code or ""),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _hypd_short_code(target_url: str, salt: int) -> str:
    raw = hashlib.blake2s(
        f"hypd\0{target_url}\0{salt}".encode("utf-8"), digest_size=6
    ).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_or_create_hypd_short_link(target_url: str) -> str:
    """Return a stable code for a valid HYPD store/afflink URL.

    Only HTTPS URLs of the form ``hypd.store/<store-id>/afflink/<token>`` are
    accepted. This cannot mint a HYPD affiliate token from a raw Meesho URL; it
    only wraps an affiliate URL already generated by HYPD.
    """
    target = str(target_url or "").strip()
    parsed = urlparse(target)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    match = re.fullmatch(r"/(\d+)/afflink/([A-Za-z0-9_-]+)", parsed.path)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in {"hypd.store", "www.hypd.store"}
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
        or not match
    ):
        raise ValueError("Only clean HTTPS HYPD store afflink URLs can be shortened")
    store_id = match.group(1)

    con = _connect()
    try:
        existing = con.execute(
            "SELECT code FROM hypd_short_links WHERE target_url=?", (target,)
        ).fetchone()
        if existing:
            return str(existing["code"])

        for salt in range(32):
            code = _hypd_short_code(target, salt)
            con.execute(
                "INSERT OR IGNORE INTO hypd_short_links (code, target_url, store_id) "
                "VALUES (?,?,?)",
                (code, target, store_id),
            )
            existing = con.execute(
                "SELECT code FROM hypd_short_links WHERE target_url=?", (target,)
            ).fetchone()
            if existing:
                con.commit()
                return str(existing["code"])
        raise RuntimeError("Could not allocate a unique HYPD short-link code")
    finally:
        con.close()


def get_hypd_short_link(code: str) -> dict | None:
    """Return a stored HYPD affiliate target for the public redirect route."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT target_url, store_id FROM hypd_short_links WHERE code=?",
            (str(code or ""),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _lehlah_affiliate_meesho_url_is_valid(target_url: str) -> bool:
    """Validate a Meesho product link with explicit LehLah/AppsFlyer markers."""
    try:
        parsed = urlparse(str(target_url or ""))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or host not in {"meesho.com", "www.meesho.com"}
        or parsed.username
        or parsed.password
        or port is not None
        or not re.fullmatch(r"/s/p/[A-Za-z0-9_-]+", parsed.path)
    ):
        return False
    params = {
        key.lower(): value
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
    }
    return (
        params.get("af_siteid", "").lower() == "lehlah"
        or params.get("mcn", "").lower() == "lehlah"
        or "lehlah" in params.get("pid", "").lower()
    )


def _lehlah_short_code(target_url: str, salt: int) -> str:
    raw = hashlib.blake2s(
        f"lehlah\0{target_url}\0{salt}".encode("utf-8"), digest_size=6
    ).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def get_or_create_lehlah_short_link(target_url: str) -> str:
    """Persist a stable first-party code for a LehLah-attributed Meesho URL.

    The original destination, including every AppsFlyer attribution parameter,
    is stored and redirected to verbatim; this never rewrites the publisher ID.
    """
    target = str(target_url or "").strip()
    if not _lehlah_affiliate_meesho_url_is_valid(target):
        raise ValueError("Only LehLah-attributed HTTPS Meesho product URLs can be shortened")

    con = _connect()
    try:
        existing = con.execute(
            "SELECT code FROM lehlah_short_links WHERE target_url=?", (target,)
        ).fetchone()
        if existing:
            return str(existing["code"])

        for salt in range(32):
            code = _lehlah_short_code(target, salt)
            con.execute(
                "INSERT OR IGNORE INTO lehlah_short_links (code, target_url) VALUES (?,?)",
                (code, target),
            )
            existing = con.execute(
                "SELECT code FROM lehlah_short_links WHERE target_url=?", (target,)
            ).fetchone()
            if existing:
                con.commit()
                return str(existing["code"])
        raise RuntimeError("Could not allocate a unique LehLah short-link code")
    finally:
        con.close()


def get_lehlah_short_link(code: str) -> dict | None:
    """Return a stored LehLah/Meesho destination for the redirect route."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT target_url FROM lehlah_short_links WHERE code=?",
            (str(code or ""),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


# -------------------------------- posts --------------------------------

def record_post(influencer_id: int, channel_id: int, deal_sig: str,
                status: str = "queued", error: str = "", deal_text: str = "") -> int:
    con = _connect()
    try:
        # Check if deal_text column exists
        cur = con.execute(
            "INSERT INTO posts (influencer_id, channel_id, deal_sig, status, error, posted_at, deal_text) "
            "VALUES (?,?,?,?,?,?,?)",
            (influencer_id, channel_id, deal_sig, status, error,
             _now() if status == "posted" else None, deal_text))
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def get_recent_posted_deals(channel_id: int, hours: int = 1) -> list[dict]:
    """Retrieve all deals posted to channel_id within the last N hours."""
    con = _connect()
    try:
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = con.execute(
            "SELECT * FROM posts WHERE channel_id=? AND status='posted' AND posted_at >= ? "
            "ORDER BY id DESC",
            (channel_id, cutoff)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def already_posted(influencer_id: int, channel_id: int, deal_sig: str) -> bool:
    con = _connect()
    try:
        row = con.execute(
            "SELECT 1 FROM posts WHERE influencer_id=? AND channel_id=? AND deal_sig=? "
            "AND status='posted'",
            (influencer_id, channel_id, deal_sig)).fetchone()
        return row is not None
    finally:
        con.close()


def already_posted_to_identifier(platform: str, identifier: str, deal_sig: str) -> bool:
    """Global dedup: has this deal_sig already been posted to same physical channel (platform+identifier) by ANY influencer?
    Prevents duplicates when multiple influencers are configured to post to same Telegram channel (e.g. @loots_channel).
    Screenshot showed same NIRLON / Levis product posted twice at 11:27 to same loots channel = this case.
    """
    plat = str(platform or "").strip().lower()
    ident = str(identifier or "").strip().lower()
    if not ident or not deal_sig:
        return False
    con = _connect()
    try:
        row = con.execute(
            "SELECT 1 FROM posts p JOIN channels c ON p.channel_id=c.id " "WHERE lower(trim(c.platform))= ? AND lower(trim(c.identifier))= ? AND p.deal_sig=? AND p.status='posted' LIMIT 1",

            (plat, ident, deal_sig)).fetchone()
        return row is not None
    finally:
        con.close()


def already_posted_content_hash(platform: str, identifier: str, content_hash: str, hours: int = 24) -> bool:
    """Check if same rendered content hash was posted to same physical channel recently (24h).
    Catches duplicates where sig differs slightly but final rendered product title+price is identical.
    """
    plat = str(platform or "").strip().lower()
    ident = str(identifier or "").strip().lower()
    if not ident or not content_hash:
        return False
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    con = _connect()
    try:
        # Use deal_text hash stored implicitly via deal_sig? We store deal_text, so check posts with same hash of deal_text title+price
        # For efficiency, check posts where deal_text contains same hash substring? Instead check posts with same content_hash stored as deal_sig prefix?
        # We use a LIKE on deal_sig for content hash? Better: compute hash from posts.deal_text on fly for recent posts only (24h = few rows)
        rows = con.execute(
            "SELECT p.deal_text FROM posts p JOIN channels c ON p.channel_id=c.id " "WHERE lower(trim(c.platform))=? AND lower(trim(c.identifier))=? AND p.status='posted' AND p.posted_at >= ?",

            (plat, ident, cutoff)).fetchall()
        for r in rows:
            txt = str(r["deal_text"] or "")
            if not txt:
                continue
            # Compute content hash same way as pipeline does for current rendered text
            import hashlib, re as _re
            # Normalize: clean, lower, remove urls, keep title+price
            try:
                from .link_router import clean_source_post as _clean
                cleaned = _clean(txt)
            except Exception:
                cleaned = txt
            cleaned_norm = _re.sub(r"https?://\S+", "", cleaned).lower()
            cleaned_norm = _re.sub(r"\s+", " ", cleaned_norm).strip()
            # Take title + price for hash
            h = hashlib.sha1(cleaned_norm.encode("utf-8")).hexdigest()[:16]
            if h == content_hash:
                return True
        return False
    finally:
        con.close()


def post_stats(influencer_id: Optional[int] = None) -> dict:
    con = _connect()
    try:
        base = "SELECT status, COUNT(*) c FROM posts"
        params: list[Any] = []
        if influencer_id is not None:
            base += " WHERE influencer_id=?"
            params.append(influencer_id)
        base += " GROUP BY status"
        rows = con.execute(base, params).fetchall()
        stats = {"queued": 0, "posted": 0, "failed": 0, "skipped": 0}
        for r in rows:
            stats[r["status"]] = r["c"]
        return stats
    finally:
        con.close()


# ------------------------------- vm_stats ------------------------------

def record_vm(cpu_pct: float, mem_pct: float, disk_pct: float, bot_running: bool) -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT INTO vm_stats (cpu_pct, mem_pct, disk_pct, bot_running) VALUES (?,?,?,?)",
            (cpu_pct, mem_pct, disk_pct, 1 if _as_bool(bot_running) else 0))
        con.commit()
    finally:
        con.close()


def latest_vm() -> Optional[dict]:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM vm_stats ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
    finally:
        con.close()


# --------------------------- global settings ---------------------------

def get_global_setting(key: str, default: str = "") -> str:
    con = _connect()
    try:
        row = con.execute("SELECT val FROM global_settings WHERE key=?", (key,)).fetchone()
        return row["val"] if row else default
    finally:
        con.close()


def set_global_setting(key: str, val: str) -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT INTO global_settings (key, val) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (key, val),
        )
        con.commit()
    finally:
        con.close()


def get_all_global_settings() -> dict[str, str]:
    con = _connect()
    try:
        rows = con.execute("SELECT key, val FROM global_settings").fetchall()
        return {r["key"]: r["val"] for r in rows}
    finally:
        con.close()


def close() -> None:  # pragma: no cover - placeholder for future pooling
    pass


def purge_old_posts_and_stats(days_to_keep: int = 14) -> int:
    """Prune posted records and vm_stats older than N days to keep SQLite light, fast, and resilient."""
    from datetime import datetime, timezone, timedelta
    con = _connect()
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days_to_keep)).isoformat(timespec="seconds")
        # Keep distinct signatures safe in dedup while trimming bulky raw text/errors
        cur = con.execute("DELETE FROM posts WHERE posted_at < ? AND status IN ('posted', 'failed', 'skipped')", (cutoff,))
        deleted = cur.rowcount
        con.execute("DELETE FROM vm_stats WHERE ts < ?", (cutoff,))
        con.commit()
        return deleted
    finally:
        con.close()


def get_live_deal_insights() -> dict:
    """Return 24h operational performance metrics for mobile & desktop analytics."""
    from datetime import datetime, timezone, timedelta
    con = _connect()
    try:
        cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        total_24h = con.execute("SELECT COUNT(*) FROM posts WHERE posted_at >= ?", (cutoff_24h,)).fetchone()[0]
        posted_24h = con.execute("SELECT COUNT(*) FROM posts WHERE posted_at >= ? AND status='posted'", (cutoff_24h,)).fetchone()[0]
        failed_24h = con.execute("SELECT COUNT(*) FROM posts WHERE posted_at >= ? AND status='failed'", (cutoff_24h,)).fetchone()[0]
        active_influencers = con.execute("SELECT COUNT(*) FROM influencers WHERE active=1").fetchone()[0]
        active_channels = con.execute(
            "SELECT COUNT(*) FROM channels WHERE lower(trim(status)) IN ('ready', 'active')"
        ).fetchone()[0]
        return {
            "total_24h": total_24h,
            "posted_24h": posted_24h,
            "failed_24h": failed_24h,
            "active_influencers": active_influencers,
            "active_channels": active_channels,
            "delivery_rate_pct": round((posted_24h / total_24h * 100), 1) if total_24h > 0 else None
        }
    finally:
        con.close()
