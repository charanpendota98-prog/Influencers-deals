"""Deal puller that reads only dialogs already present in the Telegram account.

No invite is resolved or joined here: in particular this module never sends a
CheckChatInviteRequest. Public source selectors are matched to the account's
joined dialogs; private invite links are treated as hints and resolved by title
when a source name is stored, otherwise the joined-group dialog allowlist is
used. Configured output channels are always excluded from ingestion.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from . import config, db, telegram_ops

logger = logging.getLogger(__name__)

# Top Tier Priority Channels (preferred source processing order).
PRIORITY_SOURCE_SPECS = [
    "https://t.me/+O3j4ghbtJzhjZjJl",
    "https://t.me/+8KzU3P58MJ9jN2M1",
    "https://t.me/+6LA1ljXGlbNmMjA1",
]


async def _ensure_connected(client) -> None:
    if not client.is_connected():
        await client.connect()


def _source_entries(use_dummy: bool = False) -> list[dict[str, str]]:
    """Build source hints while keeping dummy sources opt-in only."""
    entries: list[dict[str, str]] = []
    entries.extend({"name": "", "spec": spec.strip(), "kind": "production"}
                   for spec in config.SHARED_SOURCES if spec.strip())
    if use_dummy:
        entries.extend({"name": "", "spec": spec.strip(), "kind": "dummy"}
                       for spec in config.DUMMY_SOURCES if spec.strip())

    try:
        entries.extend(db.list_sources(kind="production", active_only=True))
        if use_dummy:
            entries.extend(db.list_sources(kind="dummy", active_only=True))
    except Exception as exc:
        logger.warning("Could not load source hints from SQLite: %s", exc)

    unique: dict[str, dict[str, str]] = {}
    for item in entries:
        spec = str(item.get("spec") or "").strip()
        if not spec:
            continue
        key = spec.casefold()
        if key not in unique:
            unique[key] = {
                "name": str(item.get("name") or "").strip(),
                "spec": spec,
                "kind": str(item.get("kind") or "production").strip(),
            }
        elif not unique[key]["name"] and item.get("name"):
            # Preserve a human-readable dialog title when the same URL was
            # also supplied through SHARED_SOURCES without a display name.
            unique[key]["name"] = str(item["name"]).strip()

    priority = {spec.casefold(): position for position, spec in enumerate(PRIORITY_SOURCE_SPECS)}
    return sorted(
        unique.values(),
        key=lambda item: (priority.get(item["spec"].casefold(), len(priority)), item["spec"].casefold()),
    )


def _source_list(use_dummy: bool) -> list[str]:
    """Backward-compatible list of source specs for CLI/dashboard callers."""
    return [item["spec"] for item in _source_entries(use_dummy)]


def _normal_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _source_parts(spec: str) -> tuple[str | None, str | None, bool]:
    """Return (public username, numeric id, is_unresolvable_private_invite)."""
    raw = (spec or "").strip()
    if not raw:
        return None, None, False
    if raw.startswith("@"):
        return raw.lstrip("@").casefold(), None, False
    if raw.lstrip("-").isdigit():
        return None, raw, False

    candidate = raw if "://" in raw else "https://" + raw if raw.casefold().startswith("t.me/") else ""
    if candidate:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").casefold()
        if host in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
            segments = [segment for segment in parsed.path.split("/") if segment]
            if not segments:
                return None, None, False
            first = segments[0]
            if first.startswith("+") or first.casefold() == "joinchat":
                return None, None, True
            if first.lstrip("-").isdigit():
                return None, first, False
            return first.lstrip("@").casefold(), None, False
    if not candidate:
        # SHARED_SOURCES commonly contains a username without @ or URL.
        return raw.lstrip("@").casefold(), None, False
    return None, None, False


def _dialog_id(dialog) -> str:
    """Return Telethon's marked peer ID, stable across channel/group dialogs."""
    entity = getattr(dialog, "entity", None)
    ident = getattr(dialog, "id", None) or getattr(entity, "id", None)
    return str(ident) if ident is not None else ""


def _dialog_identifiers(dialog) -> set[str]:
    """Return both Telethon's marked peer ID and the raw entity ID.

    Telethon dialogs expose ``-100...`` marked channel IDs while channel
    creation and older DB rows commonly store the raw positive ``entity.id``.
    Comparing both prevents a configured output channel from being re-ingested.
    """
    entity = getattr(dialog, "entity", None)
    return {
        str(value)
        for value in (getattr(dialog, "id", None), getattr(entity, "id", None))
        if value is not None
    }


def _dialog_username(dialog) -> str:
    entity = getattr(dialog, "entity", None)
    username = getattr(dialog, "username", None) or getattr(entity, "username", None)
    return str(username or "").lstrip("@").casefold()


def _dialog_name(dialog) -> str:
    entity = getattr(dialog, "entity", None)
    return str(getattr(dialog, "name", None) or getattr(entity, "title", None) or "").strip()


def _is_group_or_channel(dialog) -> bool:
    """Exclude user DMs/saved messages; accept channels, supergroups and groups."""
    if bool(getattr(dialog, "is_user", False)) or bool(getattr(dialog, "is_bot", False)):
        return False
    if bool(getattr(dialog, "is_group", False)) or bool(getattr(dialog, "is_channel", False)):
        return True
    entity = getattr(dialog, "entity", None)
    if entity is None:
        return False
    if bool(getattr(entity, "megagroup", False)) or bool(getattr(entity, "broadcast", False)):
        return True
    # Tolerate light-weight dialog stubs while still requiring a group/channel
    # shape (user entities do not have a title).
    return bool(getattr(entity, "title", None))


def _dialog_matches_source(dialog, entries: list[dict[str, str]]) -> bool:
    username = _dialog_username(dialog)
    title = _normal_name(_dialog_name(dialog))
    for entry in entries:
        source_name = entry.get("name", "")
        source_spec = entry.get("spec", "")
        spec_username, spec_id, _private = _source_parts(source_spec)
        if spec_username and username and spec_username == username:
            return True
        if spec_id and spec_id in _dialog_identifiers(dialog):
            return True
        if source_name:
            normalized = _normal_name(source_name)
            if normalized and title and (
                normalized == title
                or (min(len(normalized), len(title)) >= 5 and (normalized in title or title in normalized))
            ):
                return True
        # A plain source selector can be a username or a visible dialog title.
        if not source_spec.startswith(("http://", "https://", "t.me/", "telegram.me/", "@")):
            normalized = _normal_name(source_spec)
            if normalized and title and normalized == title:
                return True
    return False


def _excluded_output_keys() -> tuple[set[str], set[str]]:
    usernames: set[str] = set()
    identifiers: set[str] = set()
    selectors = list(config.TELEGRAM_OUTPUT_CHANNELS)
    if config.OPS_TELEGRAM_CHANNEL:
        selectors.append(config.OPS_TELEGRAM_CHANNEL)
    try:
        selectors.extend(
            channel.get("identifier", "")
            for channel in db.list_channels()
            if str(channel.get("platform", "")).casefold() == "telegram"
        )
    except Exception:
        pass

    for selector in selectors:
        value = str(selector or "").strip()
        if not value:
            continue
        username, numeric_id, _private = _source_parts(value)
        if username:
            usernames.add(username)
        if numeric_id:
            identifiers.add(numeric_id)
        # Direct identifiers may also arrive as -100... channel IDs.
        if value.lstrip("-").isdigit():
            identifiers.add(value)
    return usernames, identifiers


def _joined_source_dialogs(dialogs: list, entries: list[dict[str, str]]) -> list:
    username_excludes, id_excludes = _excluded_output_keys()
    candidates = []
    for dialog in dialogs:
        if not _is_group_or_channel(dialog):
            continue
        if (
            _dialog_username(dialog) in username_excludes
            or _dialog_identifiers(dialog).intersection(id_excludes)
        ):
            continue
        entity = getattr(dialog, "entity", None)
        # Do not feed the account's own output channels back into itself unless
        # the user has explicitly configured that dialog as a source.
        if bool(getattr(entity, "creator", False)) and not _dialog_matches_source(dialog, entries):
            continue
        candidates.append(dialog)

    if not entries:
        return candidates

    matched = [dialog for dialog in candidates if _dialog_matches_source(dialog, entries)]
    unresolved_private_invites = any(
        _source_parts(entry.get("spec", ""))[2] and not entry.get("name")
        for entry in entries
    )
    if unresolved_private_invites:
        # Invite hashes cannot be mapped to an entity without checking the
        # invite. Read already-joined groups/channels instead, avoiding that RPC.
        return candidates
    return matched


async def _read_dialog_list(client) -> list:
    dialogs = []
    async for dialog in client.iter_dialogs():
        dialogs.append(dialog)
    # Output-only/private-owned channels are filtered later; this no-source
    # read still deliberately uses iter_dialogs and never resolves invites.
    return dialogs


async def _iter_source_dialogs(client, use_dummy: bool = False) -> list[tuple[object, str, str]]:
    await _ensure_connected(client)
    dialogs = await _read_dialog_list(client)
    entries = _source_entries(use_dummy)
    selected = _joined_source_dialogs(dialogs, entries)
    # Preserve the Telegram dialog order (usually most recently active first).
    return [(dialog, _dialog_name(dialog) or _dialog_username(dialog) or _dialog_id(dialog), _dialog_id(dialog))
            for dialog in selected]


async def inspect_source_selection() -> dict:
    """Safely verify Telegram auth and source matching without touching invites.

    This only enumerates dialogs already joined to the account and applies the
    exact selector/output-exclusion rules used by the deal worker. It never
    checks an invite, joins a dialog, reads message history, or sends a message.
    """
    client = telegram_ops._client()
    was_connected = client.is_connected()
    try:
        await _ensure_connected(client)
        authorized = await client.is_user_authorized()
        if not authorized:
            return {"ok": False, "authorized": False, "reason": "telegram_session_not_authorized"}

        dialogs = await _read_dialog_list(client)
        entries = _source_entries(use_dummy=False)
        selected = _joined_source_dialogs(dialogs, entries)
        unresolved_private = sum(
            1 for entry in entries
            if _source_parts(entry.get("spec", ""))[2] and not entry.get("name")
        )
        return {
            "ok": bool(entries) and bool(selected),
            "authorized": True,
            "configured_sources": len(entries),
            "joined_group_channels": sum(1 for dialog in dialogs if _is_group_or_channel(dialog)),
            "selected_sources": len(selected),
            "unresolved_private_invites": unresolved_private,
            "selection_mode": "joined_dialog_fallback" if unresolved_private else "configured_selectors",
        }
    finally:
        if not was_connected and client.is_connected():
            await client.disconnect()


async def _collect_messages(client, entity, limit: int | None, after_id: int = 0) -> list:
    """Collect messages with bounded flood-wait recovery and no invite lookups."""
    kwargs = {}
    if limit is not None:
        kwargs["limit"] = max(1, int(limit))
    if after_id > 0:
        # reverse=True yields the earliest unprocessed messages first; min_id is
        # exclusive, so a restarted worker cannot replay the saved cursor.
        kwargs.update({"min_id": int(after_id), "reverse": True})

    for attempt in range(2):
        try:
            result = []
            async for message in client.iter_messages(entity, **kwargs):
                result.append(message)
            return result
        except TypeError:
            # Test doubles and older clients may not accept min_id/reverse; filter
            # by ID locally. The production Telethon call uses the full options.
            if after_id <= 0:
                raise
            try:
                result = []
                async for message in client.iter_messages(entity, limit=kwargs.get("limit")):
                    if int(getattr(message, "id", 0) or 0) > after_id:
                        result.append(message)
                result.sort(key=lambda message: int(getattr(message, "id", 0) or 0))
                return result
            except Exception as exc:
                logger.warning("Unable to read joined dialog messages: %s", exc)
                return []
        except Exception as exc:
            seconds = getattr(exc, "seconds", None)
            if seconds is None:
                seconds = getattr(exc, "value", None)
            is_flood_wait = "floodwait" in type(exc).__name__.casefold()
            if is_flood_wait and attempt == 0 and seconds is not None and int(seconds) <= 60:
                wait_seconds = max(1, int(seconds))
                logger.warning("Telegram history read asked for a %ss cooldown; retrying once", wait_seconds)
                await asyncio.sleep(wait_seconds)
                continue
            logger.warning("Joined dialog history read failed: %s", exc)
            return []
    return []


def _message_id(message) -> int:
    try:
        return int(getattr(message, "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _message_text(message) -> str:
    text = getattr(message, "message", None)
    if text is None:
        text = getattr(message, "text", "")
    return str(text or "").strip()


async def pull_recent_deals(
    limit: int = 10,
    use_dummy: bool = False,
    include_source: bool = False,
) -> list[str] | list[dict[str, str]]:
    """Return recent messages from joined source dialogs (never checks invites).

    The default remains ``list[str]`` for older CLI callers. Worker callers can
    request ``include_source=True`` to retain the source-dialog name for
    influencer-level source filters.
    """
    client = telegram_ops._client()
    out: list = []
    dialog_count = 0
    for dialog, source, _source_id in await _iter_source_dialogs(client, use_dummy):
        if dialog_count and config.TELEGRAM_SOURCE_REQUEST_SPACING:
            await asyncio.sleep(config.TELEGRAM_SOURCE_REQUEST_SPACING)
        dialog_count += 1
        entity = getattr(dialog, "entity", None) or dialog
        messages = await _collect_messages(client, entity, limit=max(1, int(limit)))
        # Telegram returns newest-first by default. Process chronologically.
        messages.sort(key=_message_id)
        for message in messages:
            text = _message_text(message)
            if not text:
                continue
            out.append({"text": text, "source": source} if include_source else text)
    return out


async def pull_new_deals(limit: int | None = None, use_dummy: bool = False) -> list[dict]:
    """Return the next bounded batch from every joined dialog after its durable cursor.

    On the first worker run, the latest ``limit`` messages are loaded as a small
    bootstrap window. Afterwards each dialog is read oldest-first after the
    saved message ID, allowing backlog to drain without skipping message IDs.
    ``text`` may be empty for media-only messages; the worker checkpoints those
    messages without attempting to dispatch empty content.
    """
    batch_size = max(1, int(limit or config.DEAL_WORKER_BATCH_SIZE))
    client = telegram_ops._client()
    records: list[dict] = []
    dialog_count = 0
    for dialog, source, source_id in await _iter_source_dialogs(client, use_dummy):
        key = source_id or _dialog_username(dialog) or _normal_name(source)
        if not key:
            continue
        cursor = db.get_worker_offset(key)
        latest_id = _message_id(getattr(dialog, "message", None))
        if cursor and latest_id and latest_id <= cursor:
            continue
        if dialog_count and config.TELEGRAM_SOURCE_REQUEST_SPACING:
            await asyncio.sleep(config.TELEGRAM_SOURCE_REQUEST_SPACING)
        dialog_count += 1
        entity = getattr(dialog, "entity", None) or dialog
        fetch_limit = batch_size if cursor else min(
            batch_size, max(1, config.DEAL_WORKER_INITIAL_BATCH_SIZE)
        )
        messages = await _collect_messages(client, entity, fetch_limit, after_id=cursor)
        # First boot reads newest N; later reads are already oldest-first but
        # sort defensively to maintain ordered, exactly-once cursor updates.
        messages.sort(key=_message_id)
        for message in messages:
            message_id = _message_id(message)
            if message_id <= cursor:
                continue
            records.append({
                "text": _message_text(message),
                "source": source,
                "source_id": key,
                "message_id": message_id,
            })
    return records
