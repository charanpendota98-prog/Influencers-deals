"""The creator inventory: everything about every creator, export and import.

Two jobs, both aimed at a thousand creators and a spreadsheet:

* :func:`rows` / :func:`csv_text` — one row per creator with **everything**:
  when they were added and by whom, their Amazon tag and where it came from,
  the approval / main / WhatsApp destinations with their real ids and status,
  the phone, the money switches, the filters, the schedule, and how they are
  doing (ready, next step, posts in the last 24 h, last post).
* :func:`plan_import` / :func:`apply_import` — the same file edited in Excel and
  pasted back. The plan is pure: it says exactly what would change, row by row,
  and nothing is written until the admin applies it. Empty cells mean "leave
  this alone"; a single ``-`` means "clear this" (an emptied channel, switched
  off network).

The export is the import's own format, so the loop is safe by construction: what
you downloaded is what you can paste back, and a round trip with no edits reports
"nothing to change".
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from . import age, config, db, pipeline, readiness

# Export order. Everything an operator needs to plan offline, in one row.
COLUMNS = [
    "creator_id", "name", "added_by", "created_date", "created_at",
    "setup_age", "setup_age_days",
    "active", "ready", "amazon_tag", "tag_source",
    "approval_channel", "main_channel", "whatsapp_channel", "wa_status",
    "phone_number", "mirror_whatsapp",
    "allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon",
    "hypd_store_id", "categories", "posting_schedule", "price_filter",
    "notes", "next_step", "next_action", "posts_24h", "mirrored_24h", "last_post_at",
]

# The subset an import may change. Everything else in the file is context.
EDITABLE = {
    "name", "amazon_tag", "active",
    "approval_channel", "main_channel", "whatsapp_channel",
    "allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon",
    "categories", "posting_schedule", "price_filter", "notes", "mirror_whatsapp",
}

# A single dash is "clear this" — an empty cell is "leave it alone". Without
# that distinction a spreadsheet could never switch a network off.
CLEAR = "-"

_TRUE = {"1", "true", "yes", "y", "on", "active", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "inactive", "disabled"}


def _text(value) -> str:
    """A human-readable cell value: booleans become yes/no, nothing becomes "-"."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value or "").strip()
    return text or CLEAR


def _bool(value: str) -> bool | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def _flag(value: str) -> str:
    if str(value or "").strip() == "":
        return ""
    if str(value).strip() == CLEAR:
        return CLEAR
    parsed = _bool(value)
    return "" if parsed is None else ("yes" if parsed else "no")


def rows(*, member: str = "", query: str = "", limit: int = 0,
         since: str = "", until: str = "", sort: str = "") -> list[dict]:
    """Every creator as one flat dict (read-only, batched queries).

    ``member`` narrows the list to the creators one team member added; ``query``
    is a free-text search over name, tag, channel ids and phone; ``since`` /
    ``until`` bound the setup date (``YYYY-MM-DD``, inclusive); ``sort`` is
    ``name``, ``oldest``, ``newest`` or ``ready`` (default: oldest first, the
    order creators were onboarded).
    """
    influencers = db.list_influencers(added_by=member)
    needle = str(query or "").strip().lower()
    if since:
        influencers = [inf for inf in influencers
                       if str(inf.get("created_at") or "")[:10] >= since]
    if until:
        influencers = [inf for inf in influencers
                       if str(inf.get("created_at") or "")[:10] <= until]
    if limit and not needle:
        influencers = influencers[: max(1, int(limit))]

    channels_by_creator: dict[int, list[dict]] = {}
    for channel in db.list_channels():
        channels_by_creator.setdefault(int(channel.get("influencer_id") or 0), []).append(channel)
    sessions_by_creator: dict[int, list[dict]] = {}
    for session in db.list_wa_sessions():
        sessions_by_creator.setdefault(int(session.get("influencer_id") or 0), []).append(session)
    try:
        mirrored = db.mirrored_post_counts(hours=24)
    except Exception:  # pragma: no cover - display only
        mirrored = {}
    try:
        # One query for the whole hub, never one per creator: the export has to
        # stay a single click on a 1000-creator hub.
        activity_by_creator: dict[int, list[dict]] = {}
        for row in db.channel_post_activity(hours=24):
            activity_by_creator.setdefault(int(row.get("influencer_id") or 0), []).append(row)
    except Exception:  # pragma: no cover - display only
        activity_by_creator = {}

    try:
        # Same idiom as the readiness dashboard: read the hub-wide mirror
        # default once, so an "inherit" channel never costs one more query.
        mirror_global_default = pipeline.mirror_main_to_whatsapp_enabled()
    except Exception:  # pragma: no cover - display only
        mirror_global_default = bool(config.MIRROR_MAIN_TO_WHATSAPP)

    out: list[dict] = []
    for inf in influencers:
        inf_id = int(inf.get("id") or 0)
        channels = channels_by_creator.get(inf_id, [])
        by_role: dict[str, list[dict]] = {"approval": [], "broadcast": [], "whatsapp": []}
        for channel in channels:
            by_role.setdefault(str(channel.get("role") or "broadcast"), []).append(channel)
        wa_channels = [
            channel for channel in channels
            if str(channel.get("platform") or "").startswith("whatsapp")
        ]
        sessions = sessions_by_creator.get(inf_id, [])
        created = str(inf.get("created_at") or "")
        posted_24h = 0
        mirrored_24h = 0
        last_post_at = ""
        mine = activity_by_creator.get(inf_id, [])
        posted_24h = sum(int(row.get("posted_24h") or 0) for row in mine)
        last_post_at = max((str(row.get("last_post_at") or "") for row in mine), default="")
        for channel in wa_channels:
            mirrored_24h += int(mirrored.get(int(channel.get("id") or 0), 0))

        waiter = readiness.creator_readiness(inf, channels=channels, wa_sessions=sessions,
                                            mirrored_counts=mirrored,
                                            mirror_global_default=mirror_global_default)
        out.append({
            "creator_id": inf_id,
            "name": str(inf.get("name") or ""),
            "added_by": str(inf.get("added_by") or ""),
            "created_date": created[:10],
            "created_at": created,
            "active": "yes" if inf.get("active") else "no",
            "ready": "yes" if waiter.get("ready") else "no",
            "amazon_tag": str(inf.get("amazon_tag") or ""),
            "tag_source": str(waiter.get("tag_source") or ""),
            "approval_channel": " ".join(
                str(channel.get("identifier") or "") for channel in by_role.get("approval", [])),
            "main_channel": " ".join(
                str(channel.get("identifier") or "") for channel in by_role.get("broadcast", [])),
            "whatsapp_channel": " ".join(
                str(channel.get("identifier") or "") for channel in wa_channels),
            "wa_status": str((sessions[0] if sessions else {}).get("status") or "not_paired"),
            "phone_number": str(inf.get("phone_number") or (sessions[0] if sessions else {})
                                .get("phone") or ""),
            "mirror_whatsapp": _mirror_flag(inf, by_role.get("broadcast", [])),
            "allow_amazon": _yes_no(inf.get("allow_amazon"), default=True),
            "allow_earnkaro": _yes_no(inf.get("allow_earnkaro"), default=True),
            "allow_hypd": _yes_no(inf.get("allow_hypd"), default=True),
            "only_amazon": _yes_no(inf.get("only_amazon"), default=False),
            "hypd_store_id": str(inf.get("hypd_store_id") or ""),
            "categories": str(inf.get("categories") or ""),
            "posting_schedule": str(inf.get("posting_schedule") or ""),
            "price_filter": str(inf.get("price_filter") or "all"),
            "notes": str(inf.get("notes") or ""),
            "next_step": str((waiter.get("next_step") or {}).get("id") or ""),
            "next_action": str((waiter.get("next_step") or {}).get("action")
                               or (waiter.get("next_step") or {}).get("detail") or ""),
            "posts_24h": posted_24h,
            "mirrored_24h": mirrored_24h,
            "last_post_at": last_post_at,
            "setup_age": age.human_age(created),
            "setup_age_days": age.days_since(created),
        })
    if needle:
        out = [row for row in out
               if needle in " ".join(str(row.get(field) or "") for field in (
                   "name", "amazon_tag", "approval_channel", "main_channel",
                   "whatsapp_channel", "phone_number", "categories", "notes",
                   "added_by")).lower()]
        if limit:
            out = out[: max(1, int(limit))]

    sort_key = str(sort or "").strip().lower()
    if sort_key in {"name", "name_desc"}:
        out.sort(key=lambda row: str(row.get("name") or "").lower(),
                 reverse=sort_key.endswith("_desc"))
    elif sort_key == "newest":
        out.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    elif sort_key == "ready":
        out.sort(key=lambda row: (row.get("ready") != "yes",
                                  str(row.get("created_at") or "")), reverse=False)
    elif sort_key == "oldest":
        out.sort(key=lambda row: str(row.get("created_at") or ""))
    return out


def _yes_no(value, *, default: bool) -> str:
    if value is None or value == "":
        return "yes" if default else "no"
    return "yes" if int(value or 0) else "no"


def _mirror_flag(inf: dict, main_channels: list[dict]) -> str:
    """``'yes'``/``'no'`` when any main channel has an explicit setting, else ``'inherit'``."""
    for channel in main_channels:
        flag = str(channel.get("mirror_whatsapp") or "").strip()
        if flag == "1":
            return "yes"
        if flag == "0":
            return "no"
    return "inherit"


def csv_text(data: list[dict] | None = None, *, member: str = "") -> str:
    """The whole inventory as CSV (Excel-ready, one row per creator)."""
    records = rows(member=member) if data is None else list(data)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for record in records:
        writer.writerow([record.get(column, "") for column in COLUMNS])
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Import: plan first, apply only when the admin says so
# --------------------------------------------------------------------------
def stash_dir() -> "Path":
    """Private directory for the pasted file between preview and apply.

    The raw spreadsheet is deliberately *not* kept in the browser session: a
    1000-creator file is ~250 KB and a cookie that big is dropped by every
    browser, which would log the owner out and lose the preview. Only a random
    token travels in the cookie.
    """
    import os
    import tempfile
    from pathlib import Path

    base = Path(os.getenv("HUB_INVENTORY_STASH_DIR")
                or Path(tempfile.gettempdir()) / "influencer-hub-inventory")
    try:
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:  # pragma: no cover - read-only tmp
        pass
    return base


def stash_write(text: str, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Store one pasted file privately and return its token ("" when too big)."""
    import os
    import secrets

    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        return ""
    stash_prune()
    token = secrets.token_urlsafe(18)
    path = stash_dir() / f"{token}.csv"
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:  # pragma: no cover - unreadable tmp
        return ""
    return token


def stash_read(token: str) -> str:
    """Read a stashed file back ("" when the token is unknown or expired)."""
    import re

    if not re.fullmatch(r"[A-Za-z0-9_\-]{10,64}", str(token or "")):
        return ""
    path = stash_dir() / f"{token}.csv"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def stash_prune(*, max_age_seconds: int = 3 * 60 * 60) -> int:
    """Drop stashes nobody applied; returns how many files were removed."""
    import time

    removed = 0
    now = time.time()
    try:
        files = list(stash_dir().glob("*.csv"))
    except OSError:  # pragma: no cover - display only
        return 0
    for path in files:
        try:
            if now - path.stat().st_mtime > max_age_seconds:
                path.unlink()
                removed += 1
        except OSError:  # pragma: no cover - racing cleanup
            continue
    return removed


def parse_csv(text: str) -> list[dict]:
    """Read a pasted/uploaded inventory file, tolerant of separators.

    The header decides the mapping (so the export's own column order is kept,
    but a column can also be moved or dropped). Rows without a recognisable
    creator are reported, never guessed at.
    """
    raw = str(text or "").strip()
    if not raw:
        return []
    lines = raw.splitlines()
    header_line = lines[0]
    delimiter = max((",", "\t", "|", ";"), key=header_line.count)
    reader = csv.reader(lines, delimiter=delimiter)
    header = [cell.strip().lower().replace(" ", "_") for cell in next(reader)]
    known = {column: index for index, column in enumerate(header) if column in COLUMNS}
    # Accept the short names the team form uses as well.
    aliases = {"creator": "name", "tag": "amazon_tag", "main": "main_channel",
               "approval": "approval_channel", "whatsapp": "whatsapp_channel",
               "id": "creator_id"}
    for alias, column in aliases.items():
        if alias in header and column not in known:
            known[column] = header.index(alias)
    if "name" not in known and "creator_id" not in known:
        raise ValueError("the file needs a 'name' (or 'creator_id') column")
    records: list[dict] = []
    for number, cells in enumerate(reader, start=2):
        row = {column: (cells[index].strip() if index < len(cells) else "")
               for column, index in known.items()}
        if not any(row.values()):
            continue
        row["_line"] = number
        records.append(row)
    return records


def plan_import(text: str, *, create: bool = False, known: dict[int, dict] | None = None) -> dict:
    """What an import file would change. Pure: writes nothing.

    ``operations`` is a list of ``{creator_id, name, changes{...}, channels{...}}``
    for review or dry-run display, and ``issues`` names the rows that cannot be
    applied with the reason.
    """
    records = parse_csv(text)
    if known is None:
        known = {int(inf["id"]): inf for inf in db.list_influencers()}
    by_name = {str(inf.get("name") or "").strip().lower(): int(inf["id"])
               for inf in known.values()}
    # Channels in one query: a 1000-row hub is reviewed in the browser, so the
    # plan may not cost two queries per row.
    channels_by_creator: dict[int, dict[str, dict]] = {}
    try:
        for channel in db.list_channels():
            inf_id = int(channel.get("influencer_id") or 0)
            role = str(channel.get("role") or "broadcast")
            channels_by_creator.setdefault(inf_id, {}).setdefault(role, channel)
    except Exception:  # pragma: no cover - display only
        channels_by_creator = {}
    try:
        from . import guard

        # The money map, read once: the plan warns when the edited spreadsheet
        # would leave two creators on one tag or one channel on two creators.
        assignment_index = guard.GuardIndex.from_db()
    except Exception:  # pragma: no cover - display only
        assignment_index = None
    from . import link_router

    operations: list[dict] = []
    issues: list[dict] = []
    created = 0
    for row in records:
        line = row.get("_line")
        name = str(row.get("name") or "").strip()
        inf_id = 0
        if str(row.get("creator_id") or "").strip().isdigit():
            candidate = int(str(row["creator_id"]).strip())
            if candidate in known:
                inf_id = candidate
            elif not name:
                issues.append({"line": line, "name": row.get("creator_id"),
                               "detail": f"creator_id {candidate} does not exist"})
                continue
        if not inf_id and name:
            inf_id = by_name.get(name.lower(), 0)
        if not inf_id:
            if not name:
                issues.append({"line": line, "name": "?", "detail": "no name"})
                continue
            if not create:
                issues.append({"line": line, "name": name,
                               "detail": "not in the hub (tick “add missing rows” to create it)"})
                continue
            tag = str(row.get("amazon_tag") or "").strip()
            if tag and not link_router.amazon_tag_is_usable(tag):
                issues.append({"line": line, "name": name,
                               "detail": f"'{tag}' is not a usable Amazon tag"})
                continue
            created += 1
            flags = {}
            for column in ("allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon"):
                parsed = _bool(row.get(column))
                if parsed is not None:
                    flags[column] = parsed
            operations.append({"line": line, "creator_id": 0, "name": name,
                               "create": True, "tag": tag,
                               "changes": flags, "channels": {
                                   "approval": str(row.get("approval_channel") or "").strip(),
                                   "broadcast": str(row.get("main_channel") or "").strip(),
                                   "whatsapp": str(row.get("whatsapp_channel") or "").strip(),
                               }})
            continue

        profile = known[inf_id]
        changes: dict[str, object] = {}
        before: dict[str, str] = {}

        def _set(field: str, wanted, previous) -> None:
            """Remember the wanted value and what the cell says today."""
            changes[field] = wanted
            before[field] = _text(previous)
        for column in sorted(EDITABLE):
            if column not in row or column in {"name", "active", "approval_channel",
                                               "main_channel", "whatsapp_channel",
                                               "mirror_whatsapp"}:
                continue
            value = str(row.get(column) or "").strip()
            if value == "":
                continue                     # empty = leave as it is
            if column in {"allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon"}:
                parsed = _bool(value)
                if parsed is None:
                    issues.append({"line": line, "name": profile.get("name"),
                                   "detail": f"{column}: '{value}' is not yes/no"})
                    continue
                current = bool(int(profile.get(column) if profile.get(column) is not None else
                                   (1 if column != "only_amazon" else 0)))
                if parsed != current:
                    _set(column, parsed, current)
                continue
            current = str(profile.get(column) or "")
            if value == CLEAR:
                if current:
                    _set(column, "", current)
                continue
            if column == "amazon_tag" and not link_router.amazon_tag_is_usable(value):
                issues.append({"line": line, "name": profile.get("name"),
                               "detail": f"'{value}' is not a usable Amazon tag"})
                continue
            if value != current:
                _set(column, value, current)
        if name and name != str(profile.get("name") or ""):
            _set("name", name, profile.get("name") or "")
        if str(row.get("active") or "").strip() != "":
            parsed = _bool(row.get("active"))
            if parsed is None:
                issues.append({"line": line, "name": profile.get("name"),
                               "detail": f"active: '{row.get('active')}' is not yes/no"})
            elif parsed != bool(profile.get("active")):
                _set("active", parsed, bool(profile.get("active")))

        # The mirror switch lives on the *main* channel: "yes"/"no" sets it
        # explicitly, "inherit" puts it back on the global default.
        mirror = _flag(row.get("mirror_whatsapp", "")) if "mirror_whatsapp" in row else ""
        if mirror and mirror not in {"yes", "no"} and mirror != CLEAR:
            mirror = ""
        if mirror:
            current_main = channels_by_creator.get(inf_id, {}).get("broadcast")
            current_flag = str((current_main or {}).get("mirror_whatsapp") or "")
            wanted_flag = {"yes": "1", "no": "0", CLEAR: ""}.get(mirror, "")
            if current_main and current_flag != wanted_flag:
                _set("_mirror_whatsapp", wanted_flag,
                     {"1": "yes", "0": "no"}.get(current_flag, "inherit"))

        channels: dict[str, str] = {}
        channels_before: dict[str, str] = {}
        current_channels = {role: channel
                            for role, channel in channels_by_creator.get(inf_id, {}).items()
                            if role in {"approval", "broadcast", "whatsapp"}}
        for column, role in (("approval_channel", "approval"), ("main_channel", "broadcast"),
                             ("whatsapp_channel", "whatsapp")):
            if column not in row:
                continue
            wanted = str(row.get(column) or "").strip()
            current = str((current_channels.get(role) or {}).get("identifier") or "")
            if wanted == CLEAR:
                if current:
                    channels[role] = ""
                    channels_before[role] = current
                continue
            if wanted and wanted != current:
                channels[role] = wanted
                channels_before[role] = current
        operations.append({
            "line": line, "creator_id": inf_id, "name": name or profile.get("name"),
            "create": False, "changes": changes, "channels": channels,
            "before": before, "channels_before": channels_before,
        })

    warnings: list[dict] = []
    if assignment_index is not None:
        for operation in operations:
            changes = operation.get("changes") or {}
            channels = operation.get("channels") or {}
            tag = str(changes.get("amazon_tag") or operation.get("tag") or "")
            taken = assignment_index.preflight(
                name=str(operation.get("name") or "") if operation.get("create") else "",
                tag=tag,
                ignore_id=0 if operation.get("create") else int(operation.get("creator_id") or 0),
                approval=str((channels.get("approval") if "approval" in channels else "") or ""),
                main=str((channels.get("broadcast") if "broadcast" in channels else "") or ""),
                whatsapp=str((channels.get("whatsapp") if "whatsapp" in channels else "") or ""),
            )
            if taken["errors"]:
                warnings.append({"line": operation.get("line"), "name": operation.get("name"),
                                 "detail": assignment_index.explain(taken)})

    to_change = [op for op in operations if op["changes"] or op["channels"] or op["create"]]
    for operation in to_change:
        operation.setdefault("before", {})
        operation.setdefault("channels_before", {})
        mirror_flag = operation["changes"].pop("_mirror_whatsapp", None)
        if mirror_flag is not None:
            operation.setdefault("mirror_whatsapp", mirror_flag)
            operation["before"]["mirror_whatsapp"] = operation["before"].pop("_mirror_whatsapp", "")
    return {
        "records": len(records),
        "operations": operations,
        "changed": len(to_change),
        "created": created,
        "unchanged": len(operations) - len(to_change),
        "issues": issues,
        "warnings": warnings,
        "ok": not issues,
    }


def apply_import(plan: dict, *, actor: str = "admin", ip: str = "") -> dict:
    """Apply a reviewed plan. Returns what really happened, row by row."""
    from . import security

    results: list[dict] = []
    updated = failed = 0
    for operation in plan.get("operations", []):
        name = str(operation.get("name") or "?")
        inf_id = int(operation.get("creator_id") or 0)
        try:
            created_row = bool(operation.get("create"))
            if created_row:
                from . import link_router

                tag = str(operation.get("tag") or "").strip()
                if not link_router.amazon_tag_is_usable(tag):
                    tag = link_router.effective_amazon_tag(tag)
                settings = {key: value for key, value in (operation.get("changes") or {}).items()
                            if key in {"allow_amazon", "allow_earnkaro", "allow_hypd", "only_amazon"}}
                inf_id = db.add_influencer(name, tag, added_by=actor, **settings)
                operation["creator_id"] = inf_id
                operation["changes"] = {}
            changes = dict(operation.get("changes") or {})
            # An empty identifier is the "-" clear instruction, so it is kept;
            # a create operation simply finds nothing to delete on a new creator.
            channels = dict(operation.get("channels") or {})
            mirror_flag = operation.get("mirror_whatsapp")
            if changes or channels or created_row or mirror_flag is not None:
                if "active" in changes:
                    db.set_influencer_active(inf_id, bool(changes.pop("active")))
                if changes:
                    db.update_influencer(inf_id, **changes)
                if mirror_flag is not None:
                    main = db.get_channel(inf_id, "broadcast")
                    if main:
                        db.update_channel_details(int(main["id"]), mirror_whatsapp=mirror_flag)
                for role, identifier in channels.items():
                    current = db.get_channel(inf_id, role)
                    if not identifier:
                        if current:
                            db.delete_channel(int(current["id"]))
                        continue
                    platform = "telegram"
                    if role == "whatsapp":
                        platform = ("whatsapp_group"
                                    if identifier.lower().endswith("@g.us")
                                    else "whatsapp_channel")
                    if current:
                        db.update_channel_details(int(current["id"]), identifier=identifier)
                    else:
                        db.add_channel(inf_id, platform, identifier, status="ready", role=role,
                                       mirror_whatsapp="1" if role == "broadcast" else "")
                detail = ", ".join(sorted(list(changes) + list(channels)
                                          + (["mirror_whatsapp"] if mirror_flag is not None else [])))
                if created_row:
                    detail = "new creator" + (f" · {detail}" if detail else "")
                else:
                    updated += 1
                results.append({"line": operation.get("line"), "name": name,
                                "status": "created" if created_row else "updated",
                                "detail": detail})
                security.audit(actor, "admin", "creator_updated", target=name,
                               detail=f"inventory import: {detail}", ip=ip)
            else:
                results.append({"line": operation.get("line"), "name": name,
                                "status": "unchanged", "detail": ""})
        except Exception as exc:  # pragma: no cover - one bad row must not stop the batch
            failed += 1
            results.append({"line": operation.get("line"), "name": name,
                            "status": "failed", "detail": str(exc)[:200]})
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "updated": updated,
        "failed": failed,
        "unchanged": sum(1 for row in results if row["status"] == "unchanged"),
        "created": sum(1 for row in results if row["status"] == "created"),
        "results": results[:200],
    }


def edits(operation: dict) -> list[dict]:
    """Field-by-field ``from → to`` list for one planned operation."""
    out: list[dict] = []
    before = operation.get("before") or {}
    for field, value in sorted((operation.get("changes") or {}).items()):
        out.append({"field": field, "from": before.get(field) or "", "to": _text(value)})
    mirror = operation.get("mirror_whatsapp")
    if mirror is not None:
        out.append({"field": "mirror_whatsapp",
                    "from": (operation.get("before") or {}).get("mirror_whatsapp") or "",
                    "to": {"1": "yes", "0": "no"}.get(mirror, "inherit")})
    channels_before = operation.get("channels_before") or {}
    for role, identifier in sorted((operation.get("channels") or {}).items()):
        out.append({"field": f"{role}_channel", "from": channels_before.get(role) or "",
                    "to": _text(identifier)})
    if operation.get("create"):
        tag = str(operation.get("tag") or "")
        out.append({"field": "amazon_tag", "from": "", "to": tag})
    return out


def render_report(report: dict) -> str:
    """One plain sentence for a flash message or a terminal."""
    bits = []
    if report.get("created"):
        bits.append(f"{report['created']} created")
    if report.get("updated"):
        bits.append(f"{report['updated']} updated")
    if report.get("unchanged"):
        bits.append(f"{report['unchanged']} unchanged")
    if report.get("failed"):
        bits.append(f"{report['failed']} failed")
    return " · ".join(bits) or "nothing to do"


def summary_line() -> str:
    """The card line: how many creators, when the newest one arrived."""
    try:
        rows_now = rows()
    except Exception:  # pragma: no cover - display only
        return "Inventory: unavailable."
    total = len(rows_now)
    team = sum(1 for row in rows_now if row.get("added_by"))
    newest = max((row.get("created_at") or "" for row in rows_now), default="")
    line = f"Inventory: {total} creator(s)"
    if team:
        line += f" · {team} added by the team"
    if newest:
        line += f" · newest {newest[:16].replace('T', ' ')}"
    return line + "."
