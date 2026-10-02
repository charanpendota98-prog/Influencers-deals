"""Telegram channel operations backed by process-isolated Telethon sessions.

All Telethon operations use the same authorized Telegram account. Each Python
process/event loop gets its own SQLite session copy seeded with the configured
base session, so the Flask dashboard, continuous worker, and CLI never open the
same ``.session`` database. On Linux a bootstrap file lock plus SQLite's online
backup API makes those copies safe even if the base session is active.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config, db

logger = logging.getLogger(__name__)

# Kept as a compatibility reference for older callers/tests. Runtime selection
# is per-event-loop; Telethon clients must never be moved between loops.
_TG_CLIENT = None
_TG_CLIENTS: dict[asyncio.AbstractEventLoop, object] = {}
_CLIENTS_LOCK = threading.RLock()
_LOOP_SLOTS: dict[asyncio.AbstractEventLoop, str] = {}
_SLOT_COUNTER = 0

try:  # pragma: no cover - target is Ubuntu; retain portability for unit tests
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def _session_base_path() -> Path:
    configured = Path(config.TELEGRAM_SESSION).expanduser()
    if not configured.is_absolute():
        configured = config.BASE_DIR / "influencer_hub" / configured
    if configured.suffix != ".session":
        configured = Path(str(configured) + ".session")
    return configured


def _isolated_slot(loop: asyncio.AbstractEventLoop) -> str:
    global _SLOT_COUNTER
    with _CLIENTS_LOCK:
        slot = _LOOP_SLOTS.get(loop)
        if slot:
            return slot
        _SLOT_COUNTER += 1
        configured = config.TELEGRAM_SESSION_SLOT or f"process-{os.getpid()}"
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", configured).strip("._-") or "process"
        slot = f"{safe}-{_SLOT_COUNTER}"
        _LOOP_SLOTS[loop] = slot
        return slot


@contextmanager
def _bootstrap_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _seed_isolated_session(source: Path, target: Path) -> None:
    """Online-backup the authorized Telethon SQLite session to a private copy."""
    if not config.TELEGRAM_SESSION_ISOLATION or source.resolve() == target.resolve():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".bootstrap.lock")
    with _bootstrap_lock(lock_path):
        if target.exists():
            try:
                target.chmod(0o600)
            except OSError:
                pass
            return
        if not source.exists():
            # A new, isolated Telethon session can still be created; it won't
            # authenticate until a provisioned session is present, but it won't
            # collide with another process's SQLite file.
            return
        src = dest = None
        try:
            src_uri = source.resolve().as_uri() + "?mode=ro"
            src = sqlite3.connect(src_uri, uri=True, timeout=30.0)
            dest = sqlite3.connect(str(target), timeout=30.0)
            src.backup(dest, pages=256, sleep=0.05)
            dest.commit()
        except sqlite3.Error as exc:
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"Could not create isolated Telethon session copy from {source.name}: {exc}"
            ) from exc
        finally:
            if src is not None:
                src.close()
            if dest is not None:
                dest.close()
        try:
            target.chmod(0o600)
        except OSError:
            pass


def _session_path_for_loop(loop: asyncio.AbstractEventLoop) -> Path:
    base = _session_base_path()
    if not config.TELEGRAM_SESSION_ISOLATION:
        return base
    slot = _isolated_slot(loop)
    target = base.with_name(f"{base.stem}.{slot}.session")
    _seed_isolated_session(base, target)
    return target


def _client():
    """Return a Telethon client bound to the current asyncio loop and session copy."""
    global _TG_CLIENT
    loop = asyncio.get_running_loop()
    with _CLIENTS_LOCK:
        existing = _TG_CLIENTS.get(loop)
        if existing is not None:
            return existing
        if not (config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH):
            raise RuntimeError("TELEGRAM_API_ID / TELEGRAM_API_HASH not set")

        from telethon import TelegramClient

        session_path = _session_path_for_loop(loop)
        client = TelegramClient(
            str(session_path),
            int(config.TELEGRAM_API_ID),
            config.TELEGRAM_API_HASH,
            connection_retries=5,
            request_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=60,
        )
        _TG_CLIENTS[loop] = client
        _TG_CLIENT = client
        return client


async def create_channel_for_influencer(influencer_id: int, title: str,
                                        about: str = "", role: str = "broadcast") -> dict:
    """Create a channel and persist it as ready for direct Telethon posting.

    Adding the optional posting bot as admin is best-effort. The authorized
    Telethon account itself posts to the channel, so a failed bot-admin grant
    must not leave an otherwise usable channel stuck in ``pending``.
    """
    from telethon.tl.functions.channels import (
        CreateChannelRequest,
        EditAdminRequest,
        ExportInviteLinkRequest,
    )
    from telethon.tl.types import ChatAdminRights

    client = _client()
    if not client.is_connected():
        await client.connect()

    result = await client(CreateChannelRequest(title=title, about=about))
    channel = result.chats[0]

    admin_granted = False
    if config.BOT_USERNAME:
        try:
            bot = await client.get_input_entity(config.BOT_USERNAME)
            await client(EditAdminRequest(
                channel=channel,
                user_id=bot,
                admin_rights=ChatAdminRights(
                    post_messages=True, edit_messages=True, delete_messages=True,
                    invite_users=True, manage_call=False, other=True,
                ),
                rank="Poster",
            ))
            admin_granted = True
        except Exception as exc:  # pragma: no cover - depends on live API
            logger.warning("Optional Telegram posting-bot admin grant failed: %s", exc)

    invite = ""
    try:
        invite = str(await client(ExportInviteLinkRequest(channel=channel)))
    except Exception as exc:  # pragma: no cover - depends on live API
        logger.warning("Could not export Telegram channel invite: %s", exc)

    from telethon.utils import get_peer_id

    ident = getattr(channel, "username", None) or str(get_peer_id(channel))
    db.add_channel(influencer_id, "telegram", ident,
                   invite_link=invite, status="ready", role=role)
    return {"identifier": ident, "invite": invite, "admin_granted": admin_granted, "role": role}


async def post_to_channel(identifier: str, text: str, media_path: str | None = None,
                          button_text: str | None = None, button_url: str | None = None) -> None:
    client = _client()
    if not client.is_connected():
        await client.connect()
    entity_selector = identifier
    if isinstance(identifier, str) and identifier.lstrip("-").isdigit():
        entity_selector = int(identifier)
    entity = await client.get_input_entity(entity_selector)

    buttons = None
    if button_text and button_url:
        from telethon import Button
        buttons = [Button.url(button_text.strip(), button_url.strip())]

    if media_path:
        await client.send_file(entity, media_path, caption=text, buttons=buttons)
    else:
        await client.send_message(entity, text, buttons=buttons)


async def disconnect() -> None:
    """Disconnect the Telethon client owned by the current event loop."""
    global _TG_CLIENT
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _CLIENTS_LOCK:
        client = _TG_CLIENTS.pop(loop, None)
        _LOOP_SLOTS.pop(loop, None)
        if client is _TG_CLIENT:
            _TG_CLIENT = next(iter(_TG_CLIENTS.values()), None)
    if client is not None and client.is_connected():
        await client.disconnect()
