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
* :func:`readiness_csv` / :func:`bulk_template_csv` — the checklist and the bulk
  template as spreadsheet-ready text, so 1000 creators can be planned offline.

Nothing here posts to a creator's channel, and no secret value is ever included
in a digest, an alert or an export.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import config, db, readiness, selfcheck

# Global settings used to remember alert state (so a restart does not re-alert).
LAST_VERDICT_KEY = "ops_last_verdict"
LAST_ALERT_KEY = "ops_last_alert_at"
DEFAULT_COOLDOWN_MINUTES = 180

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
    active_sources = sum(1 for row in sources if row.get("active"))
    try:
        deferred = db.count_deferred_deals()
    except Exception:  # pragma: no cover - display only
        deferred = 0
    worker_state = str((heartbeat or {}).get("state") or "never started")
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
        },
        "selfcheck": {
            "verdict": (report or {}).get("verdict", "unknown"),
            "summary": (report or {}).get("summary", {}),
        },
        "delivery_rate_pct": (insights or {}).get("delivery_rate_pct"),
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
    failures = posts.get("top_failures") or []
    if failures:
        lines.append("Top failures: " + "; ".join(
            f"{row['count']}× {row['reason'][:60]}" for row in failures[:2]
        ))
    return "\n".join(lines)


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


def render_alert(report: dict, decision: dict) -> str:
    """The message the ops channel receives: what is wrong and how to fix it."""
    head = {
        "got_worse": "🔴 Hub self-check got worse",
        "still_broken": "🟠 Hub self-check still not green",
        "recovered": "🟢 Hub self-check recovered",
    }.get(str(decision.get("reason")), "⚪ Hub self-check")
    lines = [f"{head} · {decision.get('verdict')}"]
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

    message = render_alert(report or {}, decision)
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
        try:
            db.set_global_setting(LAST_VERDICT_KEY, verdict)
            if sent:
                db.set_global_setting(LAST_ALERT_KEY, now.isoformat(timespec="seconds"))
        except Exception:  # pragma: no cover - settings write failure
            pass

    return {
        "sent": sent,
        "error": error,
        "message": message,
        "decision": decision,
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
