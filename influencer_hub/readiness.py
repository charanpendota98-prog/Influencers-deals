"""One honest answer to "is this creator ready, and what is the next step?"

The dashboard and Easy Setup both need the same thing: a plain-language,
read-only checklist for every creator — own Amazon tag saved, approval channel,
main channel, WhatsApp destination, phone paired, destination activated, mirror
running — plus the single next action that unblocks the most.

Nothing here sends, creates, or changes anything. It only reads the database
(and, if the caller passes it, a live WhatsApp-hub snapshot) and returns
JSON-serialisable dicts, which is why ``/api/readiness`` can serve the exact
same structure the server-rendered panels use.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from . import accounts, age, config, db, pipeline, tag_proof

# Order matters: the first unfinished step is the "next step" shown to the
# operator, so it follows the order a person actually onboards a creator.
STEP_LABELS = {
    "tag": "Amazon tag",
    "approval": "Approval channel",
    "main": "Main channel",
    "whatsapp": "WhatsApp channel",
    "phone": "Phone paired",
    "activate": "WhatsApp live",
    "mirror": "Main → WhatsApp mirror",
}

WA_PLATFORMS = {"whatsapp_group", "whatsapp_channel"}
CONNECTED_STATUSES = {"connected", "ready", "online", "open"}
# The approval channel is recommended, not mandatory: a creator who never wants
# an Amazon preview must not sit at "not ready" forever. Everything else in the
# list is what "ready to post" actually means.
REQUIRED_STEPS = {"tag", "main", "whatsapp", "phone", "activate"}


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _ready(channel: dict) -> bool:
    return _status(channel.get("status")) in pipeline.ACTIVE_CHANNEL_STATUSES


def _is_telegram(channel: dict) -> bool:
    return _status(channel.get("platform")) == "telegram"


def _role(channel: dict) -> str:
    return _status(channel.get("role")) or "broadcast"


def _is_approval(channel: dict) -> bool:
    return _is_telegram(channel) and _role(channel) == "approval"


def _is_main(channel: dict) -> bool:
    return _is_telegram(channel) and _role(channel) == "broadcast"


def _is_wa(channel: dict) -> bool:
    return _status(channel.get("platform")) in WA_PLATFORMS


def _identifiers(channels: Iterable[dict]) -> str:
    names = [str(ch.get("identifier") or "").strip() for ch in channels]
    return ", ".join(name for name in names if name)


def _step(step_id: str, done: bool, *, detail: str, action: str = "",
          url: str = "", state: str = "") -> dict:
    if not state:
        state = "done" if done else "next"
    return {
        "id": step_id,
        "label": STEP_LABELS.get(step_id, step_id),
        "done": bool(done),
        "state": state,
        "detail": detail,
        "action": action,
        "url": url,
    }


def creator_readiness(inf: dict, *, channels: list[dict] | None = None,
                      wa_sessions: list[dict] | None = None,
                      plan: dict | None = None,
                      mirrored_counts: dict[int, int] | None = None,
                      mirror_global_default: bool | None = None) -> dict:
    """Steps for one creator, in onboarding order, with the next action named."""
    inf = dict(inf or {})
    inf_id = int(inf.get("id") or 0)
    page = f"/influencer/{inf_id}" if inf_id else "/setup"
    try:
        channels = list(channels if channels is not None else db.list_channels(inf_id))
    except Exception:  # pragma: no cover - a panel must never break the page
        channels = []
    try:
        wa_sessions = list(
            wa_sessions if wa_sessions is not None else db.list_wa_sessions(inf_id)
        )
    except Exception:  # pragma: no cover - display only
        wa_sessions = []
    if plan is None:
        try:
            plan = pipeline.mirror_plan(
                inf, channels=channels, mirrored_counts=mirrored_counts,
                global_default=mirror_global_default,
            )
        except Exception:  # pragma: no cover - display only
            plan = {"active": False, "sources": [], "targets": [], "mirrored_posts_24h": 0}

    approvals = [ch for ch in channels if _is_approval(ch)]
    mains = [ch for ch in channels if _is_main(ch)]
    wa_channels = [ch for ch in channels if _is_wa(ch)]
    ready_wa = [ch for ch in wa_channels if _ready(ch)]
    pending_wa = [ch for ch in wa_channels if not _ready(ch)]

    tag = accounts.creator_amazon_tag(inf)
    own_tag = accounts.amazon_tag_is_creators_own(inf)
    tag_source = accounts.amazon_tag_source(inf)
    # A stored tag is always present (the DB fills it from the shared default so
    # a deal is never dropped). That makes "the tag is the same as the hub-wide
    # default" the honest warning: the post earns, but under the shared id, so
    # the creator's own commissions would be mixed with everyone else's.
    shared_default = str(config.AMAZON_ASSOCIATE_TAG or "").strip()
    tag_is_shared_default = bool(
        own_tag and tag and shared_default and tag == shared_default
    )

    if not own_tag:
        tag_detail = (
            f"No own tag saved — Amazon links would fall back to the shared "
            f"tag {tag or '(not configured)'}, which is not this creator's earning."
        )
    elif tag_is_shared_default:
        tag_detail = (
            f"⚠ Amazon links are signed with {tag} — the same id as the hub's shared "
            "default. Paste this creator's own Associate tag so their deals earn "
            "under their id."
        )
    else:
        tag_detail = f"Amazon links are signed with {tag} (the creator's own id)."

    tag_step = _step(
        "tag",
        own_tag,
        detail=tag_detail,
        action="" if own_tag and not tag_is_shared_default
        else "Save this creator's Amazon Associate tag",
        url=f"{page}#profile-card",
        state="done" if own_tag else "next",
    )

    def _channel_step(step_id: str, present: list[dict], *, missing: str,
                      pending: str, ok: str, action: str, anchor: str) -> dict:
        if present and any(_ready(ch) for ch in present):
            return _step(step_id, True, detail=ok.format(names=_identifiers(present)))
        if present:
            return _step(step_id, False, detail=pending.format(names=_identifiers(present)),
                         action=action, url=f"{page}{anchor}", state="blocked")
        return _step(step_id, False, detail=missing, action=action,
                     url=f"{page}{anchor}", state="next")

    approval_step = _channel_step(
        "approval", approvals,
        missing="No Amazon approval (preview) channel yet — optional, but it is where you "
                "check an Amazon deal looks right before the main channel gets it.",
        pending="Approval channel {names} is saved but not active yet.",
        ok="Approval channel {names} is active.",
        action="Add or activate the approval channel", anchor="#channels",
    )
    main_step = _channel_step(
        "main", mains,
        missing="No main Telegram channel yet — this is the channel deals are posted to.",
        pending="Main channel {names} is saved but not active yet.",
        ok="Main channel {names} is active.",
        action="Add or activate the main channel", anchor="#channels",
    )
    # "WhatsApp channel" is the *link* step: once the destination is registered
    # (even as Pending) the link work is done and pairing/activation take over,
    # so the next action never tells the operator to paste a link they pasted.
    if wa_channels:
        whatsapp_step = _step(
            "whatsapp", True,
            detail=(
                f"WhatsApp destination {_identifiers(wa_channels)} is registered"
                + (" and live." if ready_wa else
                   " and saved as Pending — the next steps pair the phone and activate it.")
            ),
        )
    else:
        whatsapp_step = _step(
            "whatsapp", False,
            detail="No WhatsApp channel/group yet — paste its link and it is saved as Pending.",
            action="Paste the WhatsApp channel link", url=f"{page}#whatsapp",
        )

    session_statuses = [
        _status(row.get("status")) for row in wa_sessions if row.get("session_key")
    ]
    phone_done = any(status in CONNECTED_STATUSES for status in session_statuses)
    if phone_done:
        phone_step = _step(
            "phone", True,
            detail="The creator's WhatsApp phone is linked (QR or login code).",
        )
    else:
        phone_step = _step(
            "phone", False,
            detail=(
                "The creator's phone is not linked to the WhatsApp hub yet, so nothing "
                "can be sent from it."
            ),
            action="Link the phone — scan the QR or type the login code",
            url=f"{page}#whatsapp-pair",
        )

    if ready_wa:
        activate_step = _step(
            "activate", True,
            detail=f"{_identifiers(ready_wa)} passed a visible Test post and is live.",
        )
    elif pending_wa:
        activate_step = _step(
            "activate", False,
            detail=(
                f"{_identifiers(pending_wa)} is Pending: a destination only goes live "
                "after one successful, visible Test post."
            ),
            action="Send one Test post to activate it",
            url=f"{page}{'#whatsapp' if phone_done else '#whatsapp-pair'}",
        )
    else:
        activate_step = _step(
            "activate", False,
            detail="Activates automatically once a WhatsApp destination exists and posts.",
            state="pending",
        )

    if plan.get("active"):
        mirror_step = _step(
            "mirror", True,
            detail=(
                "Mirror is ON: "
                + ", ".join(str(s.get("identifier")) for s in plan.get("sources") or [])
                + " → "
                + ", ".join(str(t.get("identifier")) for t in plan.get("targets") or [])
                + f" ({plan.get('mirrored_posts_24h', 0)} mirrored in 24h)."
            ),
        )
    elif plan.get("targets"):
        mirror_step = _step(
            "mirror", False,
            detail=(
                "A WhatsApp destination exists but nothing is mirrored yet"
                + (" — the destination must be live first." if pending_wa else ".")
            ),
            action="Turn the mirror on for the main channel",
            url=f"{page}#mirror-card",
            state="pending" if pending_wa else "next",
        )
    elif plan.get("opted_out"):
        mirror_step = _step(
            "mirror", False,
            detail=(
                "Mirror is switched OFF for "
                + ", ".join(str(o.get("identifier")) for o in plan.get("opted_out") or [])
                + "."
            ),
            action="Switch the mirror back to ON / inherit",
            url=f"{page}#mirror-card",
        )
    else:
        mirror_step = _step(
            "mirror", False,
            detail="Needs a ready main channel and a WhatsApp destination.",
            state="pending",
        )

    steps = [tag_step, approval_step, main_step, whatsapp_step, phone_step,
             activate_step, mirror_step]
    warnings: list[str] = []
    if tag_is_shared_default:
        warnings.append(
            f"{inf.get('name') or 'This creator'} still posts Amazon links with the "
            f"shared default tag {tag} — paste their own Associate tag."
        )
    if not phone_done and wa_channels:
        warnings.append(
            "A WhatsApp destination is saved but the creator's phone is not linked, "
            "so nothing can be sent from it."
        )
    required = [step for step in steps if step["id"] in REQUIRED_STEPS]
    # The headline "next step" is always a mandatory one; optional extras
    # (approval preview, mirror) are offered only after the creator can post.
    next_step = next((s for s in steps if not s["done"] and s["id"] in REQUIRED_STEPS),
                     None) or next((s for s in steps if not s["done"]), None)
    if next_step:
        for step in steps:
            if not step["done"] and step is not next_step:
                step["state"] = "pending"
    return {
        "id": inf_id,
        "name": str(inf.get("name") or f"Creator #{inf_id}"),
        "active": bool(inf.get("active", 1)),
        "amazon_tag": tag,
        "tag_source": tag_source,
        "own_tag": own_tag,
        "tag_is_shared_default": tag_is_shared_default,
        "warnings": warnings,
        "steps": steps,
        "next_step": next_step,
        "done_count": sum(1 for step in steps if step["done"]),
        "total_count": len(steps),
        "ready": all(step["done"] for step in required),
        "optional_pending": [step["id"] for step in steps
                             if not step["done"] and step["id"] not in REQUIRED_STEPS],
        "mirror_active": bool(plan.get("active")),
        "mirrored_posts_24h": int(plan.get("mirrored_posts_24h") or 0),
        "channels": {
            "approval": _identifiers(approvals),
            "main": _identifiers(mains),
            "whatsapp": _identifiers(wa_channels),
        },
        "wa_status": session_statuses[0] if session_statuses else "not_paired",
        # When and by whom: the team's work is stamped, so the admin can see it
        # in the same tables (and in the exports) without another query.
        "added_by": str(inf.get("added_by") or ""),
        "created_at": str(inf.get("created_at") or ""),
        "created_date": str(inf.get("created_at") or "")[:10],
        # How long this creator has been set up, in the owner's words: "2
        # months", "1 year 3 months" — plus one ready-to-render line.
        "age_days": age.days_since(inf.get("created_at")),
        "age": age.human_age(inf.get("created_at")),
        "age_long": age.long_age(inf.get("created_at")),
        "setup_label": age.setup_label(inf.get("created_at"),
                                      member=str(inf.get("added_by") or "")),
    }


def global_checks(*, heartbeat: dict | None = None,
                  sources: list[dict] | None = None) -> dict:
    """The two steps that are not per-creator: deals must arrive, and be sent."""
    heartbeat = heartbeat if heartbeat is not None else db.get_worker_heartbeat()
    rows = db.list_sources(active_only=False) if sources is None else list(sources)
    total = len(rows)
    active = sum(1 for row in rows if row.get("active"))
    worker_alive = bool(heartbeat and _status(heartbeat.get("state")) in
                        {"running", "idle", "polling", "healthy"})
    return {
        "sources": {
            "id": "sources",
            "label": "Deal sources",
            "done": active > 0,
            "state": "done" if active else "next",
            "detail": (
                f"{active} active source(s) of {total} configured." if active
                else "No active deal source — the worker would have nothing to read."
            ),
            "action": "" if active else "Add the recommended sources once",
            "url": "/easy-setup",
        },
        "worker": {
            "id": "worker",
            "label": "Worker running",
            "done": worker_alive,
            "state": "done" if worker_alive else "next",
            "detail": (
                f"Worker is {heartbeat.get('state')}."
                if worker_alive else
                "The background worker is not running, so nothing is posted even "
                "when every creator is ready."
            ),
            "action": "" if worker_alive else "Start the worker service",
            "url": "/vm",
        },
    }


FILTER_STATES = {
    "", "all", "ready", "blocked", "mirroring", "paused", "warning",
    "no_phone", "no_whatsapp", "no_main", "shared_tag",
}


def _matches(creator: dict, query: str, state: str) -> bool:
    """Filter one creator for search (name/id/tag/channels) and a state chip."""
    if query:
        needle = query.lower()
        haystack = " ".join([
            str(creator.get("name") or ""),
            f"#{creator.get('id')}",
            str(creator.get("amazon_tag") or ""),
            " ".join(str(v) for v in (creator.get("channels") or {}).values()),
        ]).lower()
        if needle not in haystack:
            return False
    state = (state or "").lower()
    if state in {"", "all"}:
        return True
    steps = {step["id"]: step for step in creator.get("steps") or []}
    if state == "ready":
        return bool(creator.get("ready"))
    if state == "blocked":
        return bool(creator.get("active")) and not creator.get("ready")
    if state == "paused":
        return not bool(creator.get("active"))
    if state == "mirroring":
        return bool(creator.get("mirror_active"))
    if state == "warning":
        return bool(creator.get("warnings"))
    if state == "shared_tag":
        return bool(creator.get("tag_is_shared_default"))
    if state == "no_phone":
        return not bool((steps.get("phone") or {}).get("done"))
    if state == "no_whatsapp":
        return not bool((steps.get("whatsapp") or {}).get("done"))
    if state == "no_main":
        return not bool((steps.get("main") or {}).get("done"))
    return True


def _bulk_inputs() -> tuple[dict[int, list[dict]], dict[int, list[dict]], dict[int, int]]:
    """One query each for channels, sessions and mirror counts.

    A hub with a thousand creators must not pay three queries per creator every
    time the command center refreshes.
    """
    channels_by_inf: dict[int, list[dict]] = {}
    for channel in db.list_channels():
        channels_by_inf.setdefault(int(channel.get("influencer_id") or 0), []).append(channel)
    sessions_by_inf: dict[int, list[dict]] = {}
    for row in db.list_wa_sessions():
        sessions_by_inf.setdefault(int(row.get("influencer_id") or 0), []).append(row)
    try:
        counts = db.mirrored_post_counts(hours=24)
    except Exception:  # pragma: no cover - counts are display only
        counts = {}
    return channels_by_inf, sessions_by_inf, counts


def overview(*, live_wa: dict[str, str] | None = None,
             heartbeat: dict | None = None,
             influencers: list[dict] | None = None,
             sources: list[dict] | None = None,
             query: str = "", state: str = "",
             limit: int = 0, offset: int = 0,
             max_actions: int = 200, max_warnings: int = 50) -> dict:
    """Readiness for every creator plus the two global checks, ready to JSON.

    ``summary`` and ``globals`` always describe the whole hub; ``creators`` is
    the (optionally filtered, paginated) slice, so the dashboard can render a
    thousand creators without shipping a thousand rows to the browser.
    """
    influencers = db.list_influencers() if influencers is None else list(influencers)
    channels_by_inf, sessions_by_inf, mirrored_counts = _bulk_inputs()
    try:
        # One read for the whole hub: the per-channel 'inherit' value must not
        # cost one global-settings query per channel.
        mirror_global_default = pipeline.mirror_main_to_whatsapp_enabled()
    except Exception:  # pragma: no cover - display only
        mirror_global_default = bool(config.MIRROR_MAIN_TO_WHATSAPP)
    creators: list[dict] = []
    for inf in influencers:
        # [] (not None) so a creator without a session never triggers the
        # per-creator fallback query inside creator_readiness().
        rows = sessions_by_inf.get(int(inf.get("id") or 0)) or []
        if live_wa:
            # The caller (dashboard /api/readiness?live=1) already talked to the
            # hub; overlay the live status on this creator's stored sessions.
            rows = [dict(row) for row in (rows or [])]
            for idx, existing in enumerate(rows):
                live = live_wa.get(str(existing.get("session_key") or ""))
                if live:
                    rows[idx] = {**existing, "status": live}
        creators.append(creator_readiness(
            inf,
            channels=channels_by_inf.get(int(inf.get("id") or 0), []),
            wa_sessions=rows,
            mirrored_counts=mirrored_counts,
            mirror_global_default=mirror_global_default,
        ))

    globals_ = global_checks(heartbeat=heartbeat, sources=sources)
    all_creators = creators
    matched = [creator for creator in creators
               if _matches(creator, query=query.strip(), state=state)]
    warnings: list[dict] = []
    for creator in all_creators:
        for message in creator.get("warnings") or []:
            warnings.append({
                "creator_id": creator["id"], "creator": creator["name"],
                "message": message,
                "url": f"/influencer/{creator['id']}#profile-card"
                if creator.get("tag_is_shared_default") else f"/influencer/{creator['id']}",
            })
    actions: list[dict] = []
    for creator in all_creators:
        if creator["active"] and creator["next_step"]:
            step = creator["next_step"]
            actions.append({
                "creator_id": creator["id"],
                "creator": creator["name"],
                "step": step["id"],
                "label": step["label"],
                "action": step["action"] or step["detail"],
                "url": step["url"],
                "state": step["state"],
            })
    for key in ("sources", "worker"):
        check = globals_[key]
        if not check["done"]:
            actions.append({
                "creator_id": 0,
                "creator": "Whole hub",
                "step": check["id"],
                "label": check["label"],
                "action": check["action"] or check["detail"],
                "url": check["url"],
                "state": check["state"],
            })

    total_steps = sum(c["total_count"] for c in all_creators) + 2
    done_steps = sum(c["done_count"] for c in all_creators) + sum(
        1 for key in ("sources", "worker") if globals_[key]["done"]
    )
    try:
        offset = max(0, int(offset))
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        offset, limit = 0, 0
    page = matched[offset:] if offset else matched
    if limit:
        page = page[:limit]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "creators": page,
        "globals": globals_,
        "actions": actions[:max_actions],
        "actions_total": len(actions),
        # A thousand-creator hub can have a thousand warnings; the list is a
        # preview, the count is the truth (the table shows the per-creator state).
        "warnings": warnings[:max_warnings],
        "warnings_total": len(warnings),
        "query": query.strip(),
        "state": state.strip().lower(),
        "total": len(all_creators),
        "matched": len(matched),
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "summary": {
            "creators": len(all_creators),
            "ready": sum(1 for c in all_creators if c["ready"]),
            "mirroring": sum(1 for c in all_creators if c["mirror_active"]),
            "blocked": sum(1 for c in all_creators if c["active"] and not c["ready"]),
            "shared_tag": sum(1 for c in all_creators if c.get("tag_is_shared_default")),
            "steps_done": done_steps,
            "steps_total": total_steps,
            "percent": round(100 * done_steps / total_steps) if total_steps else 0,
        },
    }


def creator_overview(inf_id: int) -> dict:
    """Same structure as :func:`overview` narrowed to one creator (Easy Setup)."""
    inf = db.get_influencer(inf_id)
    if not inf:  # pragma: no cover - an unknown id has nothing to report

        return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "creators": [], "globals": global_checks(), "actions": [],
                "warnings": [], "warnings_total": 0, "actions_total": 0,
                "summary": {"creators": 0, "ready": 0, "mirroring": 0, "blocked": 0,
                            "shared_tag": 0, "steps_done": 0, "steps_total": 0,
                            "percent": 0}}
    return overview(influencers=[inf])


def example_tagged_link(inf: dict) -> str:
    """The real Amazon URL carrying this creator's tag — shown in the UI.

    Not a placeholder: the same canonical link the pipeline builds (real ASIN,
    native ``?tag=``), so "what will my posts look like?" is answered with the
    url itself rather than a lookalike. ``tag_proof`` owns that construction.
    """
    return tag_proof.tagged_link(inf)
