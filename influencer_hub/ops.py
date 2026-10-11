"""Operations layer: the digest, the alert policy and the exports.

Three small, testable pieces that make a hub with a thousand creators something
you *operate* instead of watch:

* :func:`digest` — one JSON object (and one short text card) describing the last
  N hours: posts delivered/failed, the conversion-retry queue, worker, sources,
  creator readiness, mirror activity and the current self-check verdict.
* :func:`alert_decision` / :func:`maybe_alert` — a pure policy plus the one
  function that acts on it: notify the ops Telegram channel when the hub gets
  *worse* (a new failure, or a still-broken state after the cooldown), and tell
  it once when everything recovered. Silence otherwise, because an alert that
  fires on every run is an alert nobody reads.
* :func:`readiness_csv` / :func:`bulk_template_csv` / :func:`posts_csv` — the
  checklist, the bulk template and the raw delivery log as spreadsheet-ready
  text, so 1000 creators can be planned and audited offline.
* :func:`record_snapshot` / :func:`trend` — one row per day in ``ops_snapshots``
  and the 7/30-day view built from it, so the hub has a memory and the card can
  say "getting better" instead of only "how it is right now".
* :func:`metrics_text` — the same numbers in Prometheus exposition format, for
  whatever monitoring already runs on the VM.
* :func:`backup_database` / :func:`restore_database` — a consistent copy of the
  hub (SQLite online backup, plus a JSON sidecar of what was inside) and a
  restore that keeps the previous file as ``*.pre-restore-*``.

Nothing here posts to a creator's channel, and no secret value is ever included
in a digest, an alert, a metric or an export.
"""
from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import config, db, readiness, selfcheck

# Global settings used to remember alert state (so a restart does not re-alert).
LAST_VERDICT_KEY = "ops_last_verdict"
LAST_ALERT_KEY = "ops_last_alert_at"
INCIDENT_STARTED_KEY = "ops_incident_started_at"
INCIDENT_ALERTS_KEY = "ops_incident_alerts"
INCIDENT_VERDICT_KEY = "ops_incident_verdict"
DEFAULT_COOLDOWN_MINUTES = 180
SNAPSHOT_RETENTION_DAYS = 400

ALERTABLE_STATES = {"warn", "fail"}
VERDICT_RANK = {"ready": 0, "attention": 1, "fail": 2, "unknown": 3}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Digest
# --------------------------------------------------------------------------
def digest(*, hours: int = 24, plan: dict | None = None,
           report: dict | None = None, insights: dict | None = None,
           sources: list[dict] | None = None,
           heartbeat: dict | None = None,
           now: datetime | None = None) -> dict:
    """Operational summary for the last ``hours`` (default 24)."""
    hours = max(1, min(24 * 30, int(hours or 24)))
    now = now or _now()
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    by_status: dict[str, int] = {}
    failed_reasons: list = []
    mirrored_count = 0
    try:
        con = db._connect()
        try:
            rows = con.execute(
                "SELECT status AS status, COUNT(*) AS n FROM posts "
                "WHERE COALESCE(NULLIF(posted_at, ''), created_at, '') >= ? GROUP BY status",
                (cutoff,),
            ).fetchall()
            by_status = {str(row["status"]): int(row["n"]) for row in rows}
            mirrored = con.execute(
                "SELECT COUNT(*) AS n FROM posts WHERE status='posted' "
                "AND COALESCE(mirror_source_id,0) <> 0 "
                "AND COALESCE(NULLIF(posted_at, ''), created_at, '') >= ?",
                (cutoff,),
            ).fetchone()
            mirrored_count = int(mirrored["n"] if mirrored else 0)
            failed_reasons = con.execute(
                "SELECT COALESCE(NULLIF(TRIM(error), ''), 'no reason recorded') AS reason, "
                "COUNT(*) AS n FROM posts "
                "WHERE status='failed' AND COALESCE(NULLIF(posted_at, ''), created_at, '') >= ? "
                "GROUP BY reason ORDER BY n DESC LIMIT 3",
                (cutoff,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        # A digest must be printable (and alertable) on a fresh or unmigrated
        # database, where the posts table may not exist yet.
        by_status, failed_reasons, mirrored_count = {}, [], 0

    if insights is None:
        try:
            insights = db.get_live_deal_insights()
        except Exception:  # pragma: no cover - display only
            insights = {}
    if sources is None:
        try:
            sources = db.list_sources(active_only=False)
        except Exception:  # pragma: no cover - display only
            sources = []
    if heartbeat is None:
        try:
            heartbeat = db.get_worker_heartbeat()
        except Exception:  # pragma: no cover - display only
            heartbeat = None
    if plan is None:
        try:
            plan = readiness.overview()
        except Exception:  # pragma: no cover - display only
            plan = None
    if report is None:
        try:
            report = selfcheck.run_checks(live=False, plan=plan, sources=sources,
                                          heartbeat=heartbeat)
        except Exception as exc:  # pragma: no cover - display only
            report = {"verdict": "unknown", "summary": {},
                      "checks": [{"id": "selfcheck", "label": "Self-check",
                                  "state": "fail", "detail": str(exc)[:120],
                                  "fix": "run `python -m influencer_hub.cli self-check`"}]}

    summary = (plan or {}).get("summary") or {}
    # The dashboard passes the *page* it is rendering (25 rows of a thousand).
    # Counts and the blocker histogram must always describe the whole fleet, so
    # a partial plan is replaced by the full one here.
    try:
        shown = len((plan or {}).get("creators") or [])
        total = int(summary.get("creators") or 0)
        if plan is not None and total and shown < total:
            plan = readiness.overview()
            summary = (plan or {}).get("summary") or summary
    except Exception:  # pragma: no cover - display only
        pass
    active_sources = sum(1 for row in sources if row.get("active"))
    try:
        deferred = db.count_deferred_deals()
    except Exception:  # pragma: no cover - display only
        deferred = 0
    worker_state = str((heartbeat or {}).get("state") or "never started")

    # Which single step is holding the fleet back? With a thousand creators the
    # useful number is not "996 blocked" but "700 of them only need the phone".
    blocked_by_step: dict[str, int] = {}
    for creator in (plan or {}).get("creators", []):
        if creator.get("ready"):
            continue
        step = (creator.get("next_step") or {}).get("id") or "unknown"
        blocked_by_step[step] = blocked_by_step.get(step, 0) + 1
    blocked_ranked = sorted(blocked_by_step.items(), key=lambda item: (-item[1], item[0]))

    try:
        top_creators = db.top_posting_creators(hours=hours, limit=5)
    except Exception:  # pragma: no cover - display only
        top_creators = []

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "hours": hours,
        "posts": {
            "posted": int(by_status.get("posted", 0)),
            "failed": int(by_status.get("failed", 0)),
            "skipped": int(by_status.get("skipped", 0)),
            "queued": int(by_status.get("queued", 0)),
            "mirrored": mirrored_count,
            "attempts": sum(int(value) for value in by_status.values()),
            "top_failures": [
                {"reason": str(row["reason"])[:160], "count": int(row["n"])}
                for row in failed_reasons
            ],
        },
        "retry_queue": deferred,
        "worker": {"state": worker_state, "last_poll_at": (heartbeat or {}).get("last_poll_at")},
        "sources": {"active": active_sources, "total": len(sources)},
        "creators": {
            "total": summary.get("creators", 0),
            "ready": summary.get("ready", 0),
            "blocked": summary.get("blocked", 0),
            "mirroring": summary.get("mirroring", 0),
            "setup_percent": summary.get("percent", 0),
            "blocked_by_step": dict(blocked_ranked),
            "biggest_blocker": (
                {"step": blocked_ranked[0][0], "count": blocked_ranked[0][1]}
                if blocked_ranked else None
            ),
            "top": top_creators,
        },
        "selfcheck": {
            "verdict": (report or {}).get("verdict", "unknown"),
            "summary": (report or {}).get("summary", {}),
        },
        "delivery_rate_pct": (insights or {}).get("delivery_rate_pct"),
        "trend": trend(days=7, now=now),
    }


def render_digest(data: dict) -> str:
    """A compact card for the ops Telegram channel (plain text, no secrets)."""
    posts = data.get("posts") or {}
    creators = data.get("creators") or {}
    verdict = (data.get("selfcheck") or {}).get("verdict", "unknown")
    icon = {"ready": "🟢", "attention": "🟡", "fail": "🔴"}.get(verdict, "⚪")
    lines = [
        f"{icon} Hub digest · {data.get('hours', 24)}h",
        f"Posts: {posts.get('posted', 0)} delivered · {posts.get('failed', 0)} failed"
        + (f" · {posts.get('mirrored', 0)} mirrored" if posts.get("mirrored") else ""),
        f"Retry queue: {data.get('retry_queue', 0)} parked deal(s)",
        f"Worker: {data.get('worker', {}).get('state', 'unknown')}"
        f" · sources: {(data.get('sources') or {}).get('active', 0)}"
        f"/{(data.get('sources') or {}).get('total', 0)}",
        f"Creators: {creators.get('ready', 0)} ready · {creators.get('blocked', 0)} blocked"
        f" · {creators.get('mirroring', 0)} mirroring ({creators.get('setup_percent', 0)}% setup)",
        f"Self-check: {verdict}",
    ]
    trend_data = data.get("trend") or {}
    if trend_data.get("sparkline"):
        trend_line = f"{trend_data.get('days', 7)}d: {trend_data['sparkline']}"
        if trend_data.get("green_streak"):
            trend_line += f" · green {trend_data['green_streak']}d"
        elif trend_data.get("delta_pct") is not None:
            trend_line += f" · {trend_data['delta_pct']:+d}% vs previous day"
        lines.append(trend_line)

    blocker = creators.get("biggest_blocker")
    if blocker and blocker.get("count"):
        lines.append(f"Biggest blocker: {blocker['count']} need {_STEP_LABELS.get(blocker['step'], blocker['step'])}")
    top = creators.get("top") or []
    if top and top[0].get("posted"):
        lines.append(f"Top creator: {top[0]['name']} ({top[0]['posted']} posted)")

    failures = posts.get("top_failures") or []
    if failures:
        lines.append("Top failures: " + "; ".join(
            f"{row['count']}× {row['reason'][:60]}" for row in failures[:2]
        ))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# History: the daily snapshot and the trend built from it
# --------------------------------------------------------------------------
# Plain-language names for the readiness steps, so a digest line reads like a
# sentence instead of a code ("700 need phone pairing").
_STEP_LABELS = {
    "tag": "their own Amazon tag",
    "approval": "the approval channel",
    "main": "the main channel",
    "whatsapp": "the WhatsApp channel",
    "phone": "phone pairing",
    "activate": "activation",
    "mirror": "WhatsApp mirroring",
}


def sparkline(values: list[float], *, width: int = 16) -> str:
    """A tiny bar chart in Unicode blocks (``▁▂▃▅▇``) for the digest card."""
    numbers = [float(v) for v in (values or []) if v is not None]
    if not numbers:
        return ""
    if len(numbers) > width:
        numbers = numbers[-width:]
    low, high = min(numbers), max(numbers)
    blocks = "▁▂▃▄▅▆▇█"
    if high == low:
        return blocks[4] * len(numbers)
    span = high - low
    return "".join(blocks[min(7, int(round((value - low) / span * 7)))] for value in numbers)


def _delivery_pct(row: dict) -> int | None:
    attempts = int(row.get("attempts") or 0)
    if attempts <= 0:
        return None
    return int(round(100 * int(row.get("posted") or 0) / attempts))


def trend(*, days: int = 7, now: datetime | None = None) -> dict:
    """The last ``days`` snapshots as a chart-ready trend (never raises)."""
    now = now or _now()
    days = max(2, min(90, int(days or 7)))
    try:
        rows = db.list_ops_snapshots(days=days)
    except Exception:  # pragma: no cover - fresh/unmigrated database
        rows = []
    points = [{
        "day": str(row.get("day", "")),
        "posted": int(row.get("posted") or 0),
        "failed": int(row.get("failed") or 0),
        "attempts": int(row.get("attempts") or 0),
        "ready": int(row.get("ready") or 0),
        "verdict": str(row.get("verdict") or "unknown"),
        "delivery_pct": _delivery_pct(row),
    } for row in rows]

    streak = 0
    for point in reversed(points):
        if point["verdict"] == "ready":
            streak += 1
        else:
            break
    delta = None
    if len(points) >= 2 and points[-1]["delivery_pct"] is not None \
            and points[-2]["delivery_pct"] is not None:
        delta = points[-1]["delivery_pct"] - points[-2]["delivery_pct"]
    return {
        "days": days,
        "points": points,
        "captured_days": len(points),
        "sparkline": sparkline([p["delivery_pct"] for p in points if p["delivery_pct"] is not None]),
        "green_streak": streak,
        "delta_pct": delta,
        "today": points[-1] if points else None,
    }


def record_snapshot(*, hours: int = 24, now: datetime | None = None, notes: str = "",
                    data: dict | None = None, prune_days: int = SNAPSHOT_RETENTION_DAYS) -> dict:
    """Freeze today's digest numbers into ``ops_snapshots`` (upsert per day).

    Called by ``cli snapshot`` (a nightly cron) and by ``cli digest --send``.
    Writing twice on the same day simply refreshes that row, so running it by
    hand while testing never fills the history with duplicates.
    """
    now = now or _now()
    payload = data or digest(hours=hours, now=now)
    posts = payload.get("posts") or {}
    creators = payload.get("creators") or {}
    day = now.strftime("%Y-%m-%d")
    db.record_ops_snapshot(
        day,
        captured_at=now.isoformat(timespec="seconds"),
        posted=posts.get("posted", 0),
        failed=posts.get("failed", 0),
        skipped=posts.get("skipped", 0),
        mirrored=posts.get("mirrored", 0),
        attempts=posts.get("attempts", 0),
        ready=creators.get("ready", 0),
        blocked=creators.get("blocked", 0),
        setup_percent=creators.get("setup_percent", 0),
        verdict=(payload.get("selfcheck") or {}).get("verdict", "unknown"),
        notes=notes,
    )
    try:
        db.prune_ops_snapshots(keep_days=prune_days)
    except Exception:  # pragma: no cover - retention only
        pass
    return {"day": day, "captured_at": now.isoformat(timespec="seconds"),
            "posted": posts.get("posted", 0), "failed": posts.get("failed", 0),
            "attempts": posts.get("attempts", 0),
            "verdict": (payload.get("selfcheck") or {}).get("verdict", "unknown")}


# --------------------------------------------------------------------------
# Alert policy
# --------------------------------------------------------------------------
def alert_decision(report: dict | None, *, last_verdict: str = "",
                   last_alert_at: str = "", now: datetime | None = None,
                   cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES) -> dict:
    """Decide whether the ops channel should be told, and why.

    Rules (deliberately boring so nobody mutes the channel):

    * nothing to say while the verdict is ``ready`` unless it *was* worse —
      then one short "recovered" message;
    * a verdict **worse** than the last one alerts immediately;
    * the same (or a better but still not ready) verdict re-alerts only after
      the cooldown has passed, so a long outage reminds gently instead of
      spamming every run;
    * an unknown verdict (no checks at all) alerts, because silence is worse.
    """
    now = now or _now()
    verdict = str((report or {}).get("verdict") or "unknown")
    previous = str(last_verdict or "")
    if verdict == "ready":
        if previous and previous != "ready" and VERDICT_RANK.get(previous, 3) >= VERDICT_RANK["attention"]:
            return {"alert": True, "reason": "recovered", "verdict": verdict,
                    "previous": previous, "cooldown_remaining_minutes": 0}
        return {"alert": False, "reason": "all_green", "verdict": verdict,
                "previous": previous, "cooldown_remaining_minutes": 0}

    worse = VERDICT_RANK.get(verdict, 3) > VERDICT_RANK.get(previous, -1) if previous else True
    sent_at = _parse_iso(last_alert_at) if last_alert_at else None
    remaining = 0
    if sent_at is not None:
        elapsed = (now - sent_at).total_seconds() / 60
        remaining = max(0, int(cooldown_minutes - elapsed) + (1 if cooldown_minutes - elapsed > 0 else 0))
    if worse:
        return {"alert": True, "reason": "got_worse", "verdict": verdict,
                "previous": previous, "cooldown_remaining_minutes": 0}
    if sent_at is None or remaining == 0:
        return {"alert": True, "reason": "still_broken", "verdict": verdict,
                "previous": previous, "cooldown_remaining_minutes": 0}
    return {"alert": False, "reason": "cooldown", "verdict": verdict,
            "previous": previous, "cooldown_remaining_minutes": remaining}


def _minutes_between(start: str, end: datetime) -> int | None:
    started = _parse_iso(start) if start else None
    if started is None:
        return None
    return max(0, int((end - started).total_seconds() // 60))


def humanize_minutes(minutes: int | None) -> str:
    """``212`` → ``"3h 32m"`` — durations a human reads at a glance."""
    if minutes is None:
        return "unknown"
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {rest}m" if rest else f"{hours}h"
    days, rest_hours = divmod(hours, 24)
    return f"{days}d {rest_hours}h" if rest_hours else f"{days}d"


def _incident(now: datetime) -> dict:
    started = db.get_global_setting(INCIDENT_STARTED_KEY, "")
    try:
        alerts = int(db.get_global_setting(INCIDENT_ALERTS_KEY, "0") or 0)
    except ValueError:
        alerts = 0
    minutes = _minutes_between(started, now) if started else None
    return {"open": bool(started), "started_at": started, "alerts": alerts,
            "minutes_open": minutes, "duration": humanize_minutes(minutes) if started else ""}


def render_alert(report: dict, decision: dict, incident: dict | None = None) -> str:
    """The message the ops channel receives: what is wrong and how to fix it.

    When an incident is already open the message carries its age and how many
    alerts have been sent, so a long outage reads as one story instead of a
    stream of identical pings.
    """
    head = {
        "got_worse": "🔴 Hub self-check got worse",
        "still_broken": "🟠 Hub self-check still not green",
        "recovered": "🟢 Hub self-check recovered",
        "forced": "🔵 Hub self-check — manual runbook check",
    }.get(str(decision.get("reason")), "⚪ Hub self-check")
    title = f"{head} · {decision.get('verdict')}"
    incident = incident or {}
    if decision.get("reason") == "recovered" and incident.get("duration"):
        title += f" (it was broken for {incident['duration']}"
        title += f" · {incident['alerts']} alert(s))" if incident.get("alerts") else ")"
    elif incident.get("duration") and incident.get("open"):
        title += f" (open {incident['duration']} · alert #{incident['alerts']})"
    lines = [title]
    problems = [c for c in (report or {}).get("checks", [])
                if c.get("state") in ALERTABLE_STATES]
    for check in problems[:5]:
        line = f"• {check.get('label')}: {check.get('detail')}"
        if check.get("fix"):
            line += f"\n   → {check['fix']}"
        lines.append(line)
    if len(problems) > 5:
        lines.append(f"… and {len(problems) - 5} more (dashboard → Self-check).")
    if not problems:
        lines.append("• every check is green again")
    return "\n".join(lines)


def maybe_alert(report: dict | None, *, send: Callable[[str], Any] | None = None,
                now: datetime | None = None,
                cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
                force: bool = False) -> dict:
    """Alert the ops channel per :func:`alert_decision`; returns what happened.

    ``send`` defaults to posting on ``OPS_TELEGRAM_CHANNEL``. When no ops channel
    is configured nothing is sent (and nothing is recorded), so a hub that has
    not opted in never gets surprise messages. Tests pass their own ``send``.
    """
    now = now or _now()
    verdict = str((report or {}).get("verdict") or "unknown")
    last_verdict = db.get_global_setting(LAST_VERDICT_KEY, "")
    last_alert_at = db.get_global_setting(LAST_ALERT_KEY, "")
    decision = alert_decision(
        report, last_verdict=last_verdict, last_alert_at=last_alert_at,
        now=now, cooldown_minutes=cooldown_minutes,
    )
    if force and verdict != "ready":
        decision = {"alert": True, "reason": "forced", "verdict": verdict,
                    "previous": last_verdict, "cooldown_remaining_minutes": 0}

    incident = _incident(now)
    if decision["alert"] and decision.get("reason") != "recovered" and verdict != "ready":
        # A repeat alert can say which alert of the incident it is; the first
        # one of a fresh incident stays short (nobody needs "open 0m").
        incident = {**incident, "open": True,
                    "alerts": (incident["alerts"] + 1) if incident["open"] else 1}
    message = render_alert(report or {}, decision, incident)
    sent = False
    error = ""
    if decision["alert"]:
        channel = str(config.OPS_TELEGRAM_CHANNEL or "").strip()
        sender = send
        if sender is None and channel:
            from . import telegram_ops

            def sender(text: str, _channel: str = channel):  # pragma: no cover - network
                import asyncio

                return asyncio.run(telegram_ops.post_to_channel(_channel, text))
        if sender is not None:
            try:
                sender(message)
                sent = True
            except Exception as exc:  # pragma: no cover - network/report failure
                error = str(exc)[:200]
        elif not channel:
            error = "OPS_TELEGRAM_CHANNEL is not configured"

    # Remember the state only when there was somewhere to report it, so an
    # unconfigured hub still alerts on its first configured run.
    if sent or not decision["alert"]:
        stamp = now.isoformat(timespec="seconds")
        try:
            db.set_global_setting(LAST_VERDICT_KEY, verdict)
            if sent:
                db.set_global_setting(LAST_ALERT_KEY, stamp)
                if decision.get("reason") == "recovered":
                    db.set_global_setting(INCIDENT_STARTED_KEY, "")
                    db.set_global_setting(INCIDENT_ALERTS_KEY, "0")
                    db.set_global_setting(INCIDENT_VERDICT_KEY, "")
                elif verdict != "ready":
                    # One incident per outage, however many alerts it takes.
                    if not db.get_global_setting(INCIDENT_STARTED_KEY, ""):
                        db.set_global_setting(INCIDENT_STARTED_KEY, stamp)
                        db.set_global_setting(INCIDENT_ALERTS_KEY, "1")
                    else:
                        db.set_global_setting(INCIDENT_ALERTS_KEY, str(incident["alerts"]))
                    db.set_global_setting(INCIDENT_VERDICT_KEY, verdict)
        except Exception:  # pragma: no cover - settings write failure
            pass

    return {
        "sent": sent,
        "error": error,
        "message": message,
        "decision": decision,
        "incident": incident,
        "ops_channel_configured": bool(str(config.OPS_TELEGRAM_CHANNEL or "").strip()),
    }


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------
READINESS_COLUMNS = [
    "creator_id", "creator", "active", "ready", "amazon_tag", "tag_source",
    "approval_channel", "main_channel", "whatsapp_channel", "wa_status",
    "next_step", "next_action", "mirror_active", "mirrored_posts_24h",
    "warnings", "setup_step",
]


def readiness_csv(plan: dict) -> str:
    """The checklist as CSV: who is blocked, on what, and what to do next."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(READINESS_COLUMNS)
    for creator in (plan or {}).get("creators", []):
        steps = {step["id"]: step for step in creator.get("steps") or []}
        next_step = creator.get("next_step") or {}
        channels = creator.get("channels") or {}
        writer.writerow([
            creator.get("id"),
            creator.get("name"),
            "yes" if creator.get("active") else "no",
            "yes" if creator.get("ready") else "no",
            creator.get("amazon_tag"),
            creator.get("tag_source"),
            channels.get("approval", ""),
            channels.get("main", ""),
            channels.get("whatsapp", ""),
            creator.get("wa_status", ""),
            next_step.get("id", ""),
            next_step.get("action") or next_step.get("detail", ""),
            "yes" if creator.get("mirror_active") else "no",
            creator.get("mirrored_posts_24h", 0),
            " | ".join(creator.get("warnings") or []),
            "/".join(
                f"{step_id}:{'done' if step['done'] else step['state']}"
                for step_id, step in steps.items()
            ),
        ])
    return buffer.getvalue()


BULK_TEMPLATE_ROWS = [
    ["name", "amazon_tag", "main_channel", "approval_channel", "whatsapp_id"],
    ["Ravi Loots", "ravi-21", "@ravi_loots", "@ravi_amazon_deals",
     "120363012345678901@newsletter"],
    ["Sita Deals", "sita-21", "@sita_loots", "@sita_amazon_deals", ""],
    ["Kiran Deals", "kiran-21", "@kiran_loots", "", ""],
]


def bulk_template_csv() -> str:
    """A ready-to-fill CSV: the same columns the bulk paste accepts."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(BULK_TEMPLATE_ROWS)
    return buffer.getvalue()


POST_LOG_COLUMNS = [
    "id", "created_at", "posted_at", "creator", "platform", "identifier",
    "status", "deal_sig", "mirrored_from", "error",
]


def posts_csv(*, hours: int = 24, limit: int = 2000) -> str:
    """Every delivery attempt in the window — the audit log behind the counts."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(POST_LOG_COLUMNS)
    for row in db.list_recent_posts(hours=hours, limit=limit):
        writer.writerow([
            row.get("id"),
            row.get("created_at") or "",
            row.get("posted_at") or "",
            row.get("creator") or "",
            row.get("platform") or "",
            row.get("identifier") or "",
            row.get("status") or "",
            row.get("deal_sig") or "",
            "yes" if int(row.get("mirror_source_id") or 0) else "",
            " ".join(str(row.get("error") or "").split())[:300],
        ])
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Metrics (Prometheus text format) — for whatever already watches the VM
# --------------------------------------------------------------------------
def _metric_number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _metric_label(value: Any) -> str:
    text = str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return text[:80]


def metrics_text(data: dict | None = None, *, now: datetime | None = None) -> str:
    """The digest as Prometheus text, so ``/api/metrics`` can be scraped."""
    payload = data or digest(now=now)
    posts = payload.get("posts") or {}
    creators = payload.get("creators") or {}
    worker = payload.get("worker") or {}
    sources = payload.get("sources") or {}
    verdict = str((payload.get("selfcheck") or {}).get("verdict") or "unknown")
    checks = (payload.get("selfcheck") or {}).get("summary") or {}
    trend_data = payload.get("trend") or {}
    state = str(worker.get("state") or "never started")
    lines = [
        "# HELP hub_posts_attempts Delivery attempts in the window, by status.",
        "# TYPE hub_posts_attempts gauge",
    ]
    for key in ("posted", "failed", "skipped", "queued"):
        lines.append(f'hub_posts_attempts{{status="{key}"}} {_metric_number(posts.get(key))}')
    lines += [
        "# HELP hub_posts_mirrored Posts mirrored from a main channel to WhatsApp.",
        "# TYPE hub_posts_mirrored gauge",
        f"hub_posts_mirrored {_metric_number(posts.get('mirrored'))}",
        "# HELP hub_retry_queue Deals parked for a conversion retry.",
        "# TYPE hub_retry_queue gauge",
        f"hub_retry_queue {_metric_number(payload.get('retry_queue'))}",
        "# HELP hub_worker_up 1 when the background worker is running.",
        "# TYPE hub_worker_up gauge",
        "hub_worker_up " + str(_metric_number(
            1 if state in {"running", "idle", "polling", "healthy"} else 0
        )),
        "# HELP hub_sources_active Deal sources that are switched on.",
        "# TYPE hub_sources_active gauge",
        f"hub_sources_active {_metric_number(sources.get('active'))}",
        "# HELP hub_creators Creators by readiness.",
        "# TYPE hub_creators gauge",
        f'hub_creators{{state="ready"}} {_metric_number(creators.get("ready"))}',
        f'hub_creators{{state="blocked"}} {_metric_number(creators.get("blocked"))}',
        f'hub_creators{{state="mirroring"}} {_metric_number(creators.get("mirroring"))}',
        f'hub_creators{{state="total"}} {_metric_number(creators.get("total"))}',
        "# HELP hub_setup_percent Percentage of all creator setup steps done.",
        "# TYPE hub_setup_percent gauge",
        f"hub_setup_percent {_metric_number(creators.get('setup_percent'))}",
        "# HELP hub_delivery_rate_pct Delivered share of recent attempts.",
        "# TYPE hub_delivery_rate_pct gauge",
        f"hub_delivery_rate_pct {_metric_number(payload.get('delivery_rate_pct'), -1)}",
        "# HELP hub_selfcheck_state 1 for the current self-check verdict.",
        "# TYPE hub_selfcheck_state gauge",
        f'hub_selfcheck_state{{verdict="{_metric_label(verdict)}"}} 1',
        "# HELP hub_selfcheck_problems Checks that are not green right now.",
        "# TYPE hub_selfcheck_problems gauge",
        "hub_selfcheck_problems " + str(
            _metric_number(checks.get("warn")) + _metric_number(checks.get("fail"))
        ),
        "# HELP hub_ops_snapshots Days of history stored.",
        "# TYPE hub_ops_snapshots gauge",
        f"hub_ops_snapshots {_metric_number(trend_data.get('captured_days'))}",
        "# HELP hub_green_streak_days Consecutive green days in the history.",
        "# TYPE hub_green_streak_days gauge",
        f"hub_green_streak_days {_metric_number(trend_data.get('green_streak'))}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Backup and restore
# --------------------------------------------------------------------------
def _sidecar(path: Path, source: Path) -> dict:
    """A small JSON next to the backup saying what is inside it."""
    try:
        con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            counts = {}
            for table in ("influencers", "channels", "posts", "sources", "ops_snapshots"):
                if table in tables:
                    counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except Exception as exc:  # pragma: no cover - corrupt source
        tables, counts = [], {"error": str(exc)[:120]}
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": str(source),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "tables": tables,
        "counts": counts,
    }
    path.with_suffix(path.suffix + ".json").write_text(json.dumps(payload, indent=2))
    return payload


def backup_database(out_dir: str | Path = "backups", *, keep: int = 7,
                    now: datetime | None = None) -> dict:
    """A consistent snapshot of the hub database + a JSON sidecar.

    Uses SQLite's online backup API, so it is safe to run while the worker is
    posting (a plain ``cp`` of a WAL database is not). Secrets stay in ``.env``
    and are deliberately *not* part of a backup, which makes the copy safe to
    move to another machine.
    """
    now = now or _now()
    source = Path(config.DB_PATH)
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    target_dir = Path(out_dir).expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"hub-{now.strftime('%Y%m%d-%H%M%S')}.sqlite3"

    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dest = sqlite3.connect(str(target))
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()

    size_bytes = target.stat().st_size
    sidecar = _sidecar(target, source)
    # Retention: keep the newest `keep` files, never the one just written (a
    # clock skew or an old --now must not delete the backup we are reporting).
    kept, pruned = [], []
    keep = max(1, int(keep or 7))
    existing = sorted(target_dir.glob("hub-*.sqlite3"), key=lambda item: item.name)
    for old_file in existing[:-keep]:
        if old_file == target:
            continue
        pruned.append(old_file.name)
        try:
            old_file.unlink()
            old_file.with_suffix(old_file.suffix + ".json").unlink(missing_ok=True)
        except OSError:  # pragma: no cover - retention is best-effort
            pass
    kept = [item.name for item in existing if item.name not in pruned and item.exists()]
    return {
        "path": str(target),
        "sidecar": str(target) + ".json",
        "size_bytes": size_bytes,
        "counts": sidecar.get("counts", {}),
        "kept": kept,
        "pruned": pruned,
        "source": str(source),
    }


REQUIRED_TABLES = {"influencers", "channels", "posts", "global_settings"}


def inspect_backup(path: str | Path) -> dict:
    """Is this file a hub database? Never raises; reports what it found."""
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return {"ok": False, "reason": f"file not found: {candidate}", "tables": []}
    try:
        con = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
        try:
            tables = [row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )]
            counts = {}
            for table in ("influencers", "channels", "posts"):
                if table in tables:
                    counts[table] = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    except Exception as exc:
        return {"ok": False, "reason": f"not a readable SQLite database: {str(exc)[:120]}", "tables": []}
    missing = sorted(REQUIRED_TABLES - set(tables))
    return {"ok": not missing, "reason": "" if not missing else f"missing tables: {', '.join(missing)}",
            "tables": tables, "counts": counts, "size_bytes": candidate.stat().st_size}


def restore_database(path: str | Path, *, now: datetime | None = None,
                     dry_run: bool = False) -> dict:
    """Replace the live database with ``path``, keeping the previous one aside.

    Refuses anything that does not look like a hub database, and moves the old
    file to ``<db>.pre-restore-<timestamp>`` first, so a restore is always
    reversible. The stale ``-wal``/``-shm`` files are removed: keeping them
    would let SQLite replay writes from the database that was just replaced.
    """
    now = now or _now()
    check = inspect_backup(path)
    if not check["ok"]:
        raise ValueError(f"refusing to restore: {check['reason']}")
    source = Path(path).expanduser()
    target = Path(config.DB_PATH)
    previous = None
    if target.exists():
        previous = target.with_name(f"{target.name}.pre-restore-{now.strftime('%Y%m%d-%H%M%S')}")
        if not dry_run:
            shutil.copy2(target, previous)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        try:  # the schema may be older than this build; migrate instead of failing
            db.migrate()
        except Exception:  # pragma: no cover - reported by the self-check next run
            pass
    return {
        "restored_from": str(source),
        "previous_saved_as": str(previous) if previous else "",
        "dry_run": bool(dry_run),
        "counts": check.get("counts", {}),
        "tables": len(check.get("tables", [])),
    }
