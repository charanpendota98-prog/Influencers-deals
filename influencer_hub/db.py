"""SQLite persistence for the influencer hub.

Schema is intentionally small and explicit (no ORM) so it can be inspected and
backed up trivially on the VM. All write helpers are idempotent where it makes
sense (e.g. upsert by natural key).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

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

CREATE INDEX IF NOT EXISTS idx_channels_influencer ON channels(influencer_id);
CREATE INDEX IF NOT EXISTS idx_posts_sig ON posts(deal_sig, influencer_id);
CREATE INDEX IF NOT EXISTS idx_wa_influencer ON wa_sessions(influencer_id);
"""


def _connect() -> sqlite3.Connection:
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(config.DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init() -> None:
    """Create the schema (idempotent). Call once at startup."""
    con = _connect()
    try:
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
        ):
            if col not in inf_cols:
                con.execute(f"ALTER TABLE influencers ADD COLUMN {col} {ddl}")

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
        ):
            if col not in ch_cols:
                con.execute(f"ALTER TABLE channels ADD COLUMN {col} {ddl}")

        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----------------------------- influencers -----------------------------

def add_influencer(name: str, amazon_tag: str, handle: str = "", notes: str = "",
                   use_dummy_sources: bool = False,
                   telegram_enabled: bool = True, whatsapp_enabled: bool = True,
                   insta_id: str = "", phone_number: str = "",
                   price_filter: str = "all", allowed_sources: str = "",
                   bitly_api_key: str = "", categories: str = "",
                   posting_schedule: str = "") -> int:
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO influencers "
            "(name, handle, amazon_tag, notes, use_dummy_sources, telegram_enabled, whatsapp_enabled, insta_id, phone_number, price_filter, allowed_sources, bitly_api_key, categories, posting_schedule) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name.strip(), handle.strip(), amazon_tag.strip(), notes.strip(), 1 if use_dummy_sources else 0,
             1 if telegram_enabled else 0, 1 if whatsapp_enabled else 0, insta_id.strip(),
             phone_number.strip(), price_filter.strip() or "all", allowed_sources.strip(), bitly_api_key.strip(),
             categories.strip(), posting_schedule.strip()),
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
                      posting_schedule: str | None = None,
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
            params.append(amazon_tag.strip())
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
        if notes is not None:
            updates.append("notes=?")
            params.append(notes.strip())
        if active is not None:
            updates.append("active=?")
            params.append(1 if active else 0)
        if updates:
            params.append(influencer_id)
            con.execute(f"UPDATE influencers SET {', '.join(updates)} WHERE id=?", params)
            con.commit()
    finally:
        con.close()


def set_channel_flags(influencer_id: int, telegram_enabled: bool | None = None,
                      whatsapp_enabled: bool | None = None) -> None:
    con = _connect()
    try:
        if telegram_enabled is not None:
            con.execute("UPDATE influencers SET telegram_enabled=? WHERE id=?",
                        (1 if telegram_enabled else 0, influencer_id))
        if whatsapp_enabled is not None:
            con.execute("UPDATE influencers SET whatsapp_enabled=? WHERE id=?",
                        (1 if whatsapp_enabled else 0, influencer_id))
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
                    (1 if active else 0, influencer_id))
        con.commit()
    finally:
        con.close()


def set_use_dummy_sources(influencer_id: int, use_dummy: bool) -> None:
    con = _connect()
    try:
        con.execute("UPDATE influencers SET use_dummy_sources=? WHERE id=?",
                    (1 if use_dummy else 0, influencer_id))
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
                posting_schedule: str = "") -> int:
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO channels (influencer_id, platform, identifier, invite_link, status, role, amazon_override_tag, strip_amazon, price_filter, allowed_sources, wa_session_key, bitly_api_key, categories, posting_schedule) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (influencer_id, platform, identifier.strip(), invite_link.strip(), status, role,
             amazon_override_tag.strip(), 1 if strip_amazon else 0, price_filter.strip(), allowed_sources.strip(),
             wa_session_key.strip(), bitly_api_key.strip(), categories.strip(), posting_schedule.strip()),
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
                           posting_schedule: str | None = None) -> None:
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
            params.append(1 if strip_amazon else 0)
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


def update_influencer(influencer_id: int, name: str | None = None,
                      amazon_tag: str | None = None, handle: str | None = None,
                      insta_id: str | None = None, notes: str | None = None,
                      active: bool | None = None) -> None:
    con = _connect()
    try:
        updates = []
        params = []
        if name is not None:
            updates.append("name=?")
            params.append(name.strip())
        if amazon_tag is not None:
            updates.append("amazon_tag=?")
            params.append(amazon_tag.strip())
        if handle is not None:
            updates.append("handle=?")
            params.append(handle.strip())
        if insta_id is not None:
            updates.append("insta_id=?")
            params.append(insta_id.strip())
        if notes is not None:
            updates.append("notes=?")
            params.append(notes.strip())
        if active is not None:
            updates.append("active=?")
            params.append(1 if active else 0)
        if updates:
            params.append(influencer_id)
            con.execute(f"UPDATE influencers SET {', '.join(updates)} WHERE id=?", params)
            con.commit()
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
    """Batch insert influencers + their initial 3 channels.
    Designed for scaling to 1000+ influencers in a single SQLite transaction!
    Each record can have:
      name, amazon_tag, phone_number, insta_id, handle, price_filter, allowed_sources,
      approval_tg, broadcast_tg, whatsapp_id, strip_amazon
    """
    con = _connect()
    count = 0
    try:
        for r in records:
            name = (r.get("name") or "").strip()
            tag = (r.get("amazon_tag") or r.get("tag") or "").strip()
            if not name or not tag:
                continue

            phone = (r.get("phone_number") or r.get("phone") or "").strip()
            insta = (r.get("insta_id") or r.get("insta") or "").strip()
            handle = (r.get("handle") or "").strip()
            price_filt = (r.get("price_filter") or "all").strip()
            sources = (r.get("allowed_sources") or "").strip()
            strip_amz = bool(r.get("strip_amazon", False))

            cur = con.execute(
                "INSERT INTO influencers "
                "(name, handle, amazon_tag, use_dummy_sources, telegram_enabled, whatsapp_enabled, insta_id, phone_number, price_filter, allowed_sources, bitly_api_key, categories, posting_schedule) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, handle, tag, 0, 1, 1, insta, phone, price_filt, sources, (r.get("bitly_api_key") or "").strip(),
                 (r.get("categories") or "").strip(), (r.get("posting_schedule") or "").strip()),
            )
            iid = int(cur.lastrowid)

            # Auto-link channels if provided in the batch
            if r.get("approval_tg"):
                ident = r["approval_tg"].strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, allowed_sources, categories, posting_schedule) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (iid, "telegram", ident, "approval", "ready", sources,
                     (r.get("categories") or "").strip(), (r.get("posting_schedule") or "").strip()),
                )
            if r.get("broadcast_tg"):
                ident = r["broadcast_tg"].strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, strip_amazon, price_filter, allowed_sources, bitly_api_key, categories, posting_schedule) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, "telegram", ident, "broadcast", "ready", 1 if strip_amz else 0,
                     price_filt if price_filt != "all" else "", sources, (r.get("bitly_api_key") or "").strip(),
                     (r.get("categories") or "").strip(), (r.get("posting_schedule") or "").strip()),
                )
            if r.get("whatsapp_id"):
                ident = r["whatsapp_id"].strip()
                con.execute(
                    "INSERT INTO channels (influencer_id, platform, identifier, role, status, strip_amazon, price_filter, allowed_sources, wa_session_key, bitly_api_key, categories, posting_schedule) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (iid, "whatsapp_group", ident, "whatsapp", "ready", 1 if strip_amz else 0,
                     price_filt if price_filt != "all" else "", sources,
                     (r.get("wa_session_key") or "").strip(), (r.get("bitly_api_key") or "").strip(),
                     (r.get("categories") or "").strip(), (r.get("posting_schedule") or "").strip()),
                )
            count += 1
        con.commit()
        return count
    finally:
        con.close()


def add_source(name: str, spec: str, kind: str = "production", active: bool = True) -> int:
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO sources (name, spec, kind, active) VALUES (?,?,?,?)",
            (name, spec, kind, 1 if active else 0))
        con.commit()
        return int(cur.lastrowid)
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


# -------------------------------- posts --------------------------------

def record_post(influencer_id: int, channel_id: int, deal_sig: str,
                status: str = "queued", error: str = "") -> int:
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO posts (influencer_id, channel_id, deal_sig, status, error, posted_at) "
            "VALUES (?,?,?,?,?,?)",
            (influencer_id, channel_id, deal_sig, status, error,
             _now() if status == "posted" else None))
        con.commit()
        return int(cur.lastrowid)
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
            (cpu_pct, mem_pct, disk_pct, 1 if bot_running else 0))
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


def close() -> None:  # pragma: no cover - placeholder for future pooling
    pass
