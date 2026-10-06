#!/usr/bin/env python3
"""Deal flow: prove and see that deals keep posting from every source.

    worker     -> records per-source activity (deals seen, posted, failed, why)
    db         -> source_activity table + channel_post_activity()
    app.py     -> _flow_snapshot() and the read-only /api/flow endpoint
    Easy Setup -> a "Deal flow" card: worker heartbeat, per-source last deal,
                  per-channel posted/failed counts and the hourly-loot switch

What changes
------------
* `influencer_hub/worker.py` still holds a source cursor on a failed delivery,
  and now also records posted/failed counts and the failure reason per source.
* `influencer_hub/db.py` gains the `source_activity` table and the readers the
  dashboard uses. No existing table or column changes.
* `dashboard/app.py` gains `_flow_snapshot()` and `GET /api/flow` (pure reads,
  no network calls) and passes the snapshot to Easy Setup.
* `dashboard/templates/easy_setup.html` renders the flow card, with the
  per-source detail folded into the Advanced section.

Usage (from the repo root)
--------------------------
    python3 patches/apply-deal-flow.py
    python3 patches/apply-deal-flow.py --check
    python3 patches/apply-deal-flow.py --with-tests
    python3 patches/apply-deal-flow.py --dry-run

Exit codes: 0 applied (or `--check` says it is in place), 1 already applied,
2 the file is not the verified revision (nothing is written).

Independent of the other appliers: it touches different blocks of `app.py` and
`db.py`, so it can be applied before or after them.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH_DIR = Path(__file__).resolve().parent
TESTS_DIR = PATCH_DIR / "tests"
FLOW_MARKER = "def _flow_snapshot("
DB_MARKER = "CREATE TABLE IF NOT EXISTS source_activity"


class PatchError(RuntimeError):
    """Raised when a file does not look like the revision this patch expects."""


EDITS = [
    (
        'influencer_hub/db.py',
        """CREATE TABLE IF NOT EXISTS worker_offsets (
    source_key       TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- First-party, auditable Amazon redirects. The short public URL still carries
-- the creator's Associates tag; targets are restricted in the redirect route.
""",
        """CREATE TABLE IF NOT EXISTS worker_offsets (
    source_key       TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-source deal flow: when a source last produced a deal, how many were
-- dispatched, and how many failed. This is what makes "is posting still
-- flowing?" answerable per source instead of guessing from logs.
CREATE TABLE IF NOT EXISTS source_activity (
    source_key        TEXT PRIMARY KEY,
    last_seen_at      REAL NOT NULL DEFAULT 0,
    last_message_at   REAL NOT NULL DEFAULT 0,
    last_message_id   INTEGER NOT NULL DEFAULT 0,
    deals_seen        INTEGER NOT NULL DEFAULT 0,
    posts_dispatched  INTEGER NOT NULL DEFAULT 0,
    failures          INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- First-party, auditable Amazon redirects. The short public URL still carries
-- the creator's Associates tag; targets are restricted in the redirect route.
""",
        'influencer_hub/db.py: hunk 1',
    ),
    (
        'influencer_hub/db.py',
        """    finally:
        con.close()


def reserve_whatsapp_send(
    session_key: str,
    gap_seconds: float,
    rest_seconds: float,
""",
        '    finally:\n        con.close()\n\n\ndef record_source_activity(\n    source_key: str,\n    *,\n    message_id: int = 0,\n    posted: int = 0,\n    failed: int = 0,\n    error: str = "",\n    now: float | None = None,\n) -> None:\n    """Record that one source produced a deal and what happened to it.\n\n    Called by the worker for every handled message, so the dashboard can show a\n    per-source flow instead of a single global heartbeat.\n    """\n    key = str(source_key or "").strip()\n    if not key:\n        return\n    stamp = time.time() if now is None else float(now)\n    message_id = int(message_id or 0)\n    con = _connect()\n    try:\n        con.execute(\n            "INSERT INTO source_activity (source_key, last_seen_at, last_message_at, "\n            "last_message_id, deals_seen, posts_dispatched, failures, last_error, updated_at) "\n            "VALUES (?,?,?,?,?,?,?,?,?) "\n            "ON CONFLICT(source_key) DO UPDATE SET "\n            "last_seen_at=excluded.last_seen_at, "\n            "last_message_at=CASE WHEN excluded.last_message_id > source_activity.last_message_id "\n            "THEN excluded.last_message_at ELSE source_activity.last_message_at END, "\n            "last_message_id=MAX(source_activity.last_message_id, excluded.last_message_id), "\n            "deals_seen=source_activity.deals_seen + excluded.deals_seen, "\n            "posts_dispatched=source_activity.posts_dispatched + excluded.posts_dispatched, "\n            "failures=source_activity.failures + excluded.failures, "\n            "last_error=CASE WHEN excluded.last_error <> \'\' THEN excluded.last_error "\n            "ELSE source_activity.last_error END, "\n            "updated_at=excluded.updated_at",\n            (key, stamp, stamp if message_id else 0.0, message_id, 1,\n             int(posted or 0), int(failed or 0), str(error or "")[:300], _now()),\n        )\n        con.commit()\n    finally:\n        con.close()\n\n\ndef list_source_activity() -> list[dict]:\n    """Every source that has produced at least one deal, newest activity last."""\n    con = _connect()\n    try:\n        rows = con.execute(\n            "SELECT source_key, last_seen_at, last_message_at, last_message_id, "\n            "deals_seen, posts_dispatched, failures, last_error, updated_at "\n            "FROM source_activity ORDER BY last_seen_at ASC"\n        ).fetchall()\n        return [dict(row) for row in rows]\n    finally:\n        con.close()\n\n\ndef channel_post_activity(hours: int = 24) -> list[dict]:\n    """Per channel: the last successful post, posts in the window, failures.\n\n    ``posts.posted_at`` is only stamped for successful posts, so failures are\n    reported all-time (``failed_total``) rather than pretending they are dated.\n    """\n    try:\n        window = max(1, int(hours))\n    except (TypeError, ValueError):\n        window = 24\n    from datetime import datetime, timedelta, timezone\n\n    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat(\n        timespec="seconds"\n    )\n    con = _connect()\n    try:\n        rows = con.execute(\n            "SELECT c.id AS channel_id, c.influencer_id, c.platform, c.identifier, "\n            "c.status, c.role, i.name AS influencer_name, "\n            "MAX(p.posted_at) AS last_posted_at, "\n            "COALESCE(SUM(CASE WHEN p.status=\'posted\' AND p.posted_at >= ? THEN 1 ELSE 0 END), 0) "\n            "AS posted_in_window, "\n            "COALESCE(SUM(CASE WHEN p.status=\'failed\' THEN 1 ELSE 0 END), 0) "\n            "AS failed_total "\n            "FROM channels c LEFT JOIN influencers i ON i.id = c.influencer_id "\n            "LEFT JOIN posts p ON p.channel_id = c.id "\n            "GROUP BY c.id ORDER BY c.influencer_id, c.id",\n            (cutoff,),\n        ).fetchall()\n        return [dict(row) for row in rows]\n    finally:\n        con.close()\n\n\ndef reserve_whatsapp_send(\n    session_key: str,\n    gap_seconds: float,\n    rest_seconds: float,\n',
        'influencer_hub/db.py: hunk 2',
    ),
    (
        'influencer_hub/worker.py',
        """        str(status).strip().lower().startswith("failed")
        for per_channel in results.values()
        for status in per_channel.values()
    )


def _record_heartbeat(state: str, *, poll_completed: bool = False, error_code: str = "") -> None:
    try:
""",
        '        str(status).strip().lower().startswith("failed")\n        for per_channel in results.values()\n        for status in per_channel.values()\n    )\n\n\ndef _delivery_counts(results: dict) -> tuple[int, int]:\n    """Count posted and failed destinations across one pipeline result."""\n    posted = failed = 0\n    for per_channel in (results or {}).values():\n        for status in (per_channel or {}).values():\n            normalized = str(status).strip().lower()\n            if normalized.startswith("posted"):\n                posted += 1\n            elif normalized.startswith("failed"):\n                failed += 1\n    return posted, failed\n\n\ndef _first_failure_reason(results: dict) -> str:\n    """The first failure detail, so the flow board can show why a source stalled."""\n    for per_channel in (results or {}).values():\n        for status in (per_channel or {}).values():\n            normalized = str(status).strip()\n            if normalized.lower().startswith("failed"):\n                return normalized[:300]\n    return ""\n\n\ndef _record_source_activity(\n    source_key: str, *, message_id: int = 0, posted: int = 0, failed: int = 0,\n    error: str = "",\n) -> None:\n    """Per-source flow bookkeeping; never allowed to stop ingestion."""\n    try:\n        db.record_source_activity(\n            source_key, message_id=message_id, posted=posted, failed=failed, error=error\n        )\n    except Exception:\n        logger.warning("Could not record source activity", exc_info=True)\n\n\ndef _record_heartbeat(state: str, *, poll_completed: bool = False, error_code: str = "") -> None:\n    try:\n',
        'influencer_hub/worker.py: hunk 1',
    ),
    (
        'influencer_hub/worker.py',
        """                        continue
                    seen_sigs_this_batch.add(_sig)
                except Exception:
                    pass
            if text:
                deal = {"text": text, "source": str(record.get("source") or "")}
                try:
                    result = await pipeline.run_once([deal])
                except Exception:
                    logger.exception("Pipeline failed for source %s message %s", source_key, message_id)
                    retried += 1
                    break
                if _failed_delivery(result):
                    # Some target(s) may have succeeded; pipeline dedup makes
                    # their retry idempotent. Keep this source cursor unchanged
                    # so the failed target gets another attempt next poll.
                    logger.warning(
                        "Delivery failure for source %s message %s; cursor held for retry",
                        source_key, message_id,
                    )
                    retried += 1
                    break

            # Empty/media-only Telegram posts have no text parser input; mark
            # them handled so they cannot block later deals forever.
            db.set_worker_offset(source_key, message_id)
""",
        """                        continue
                    seen_sigs_this_batch.add(_sig)
                except Exception:
                    pass
            posted = failed = 0
            failure_note = ""
            if text:
                deal = {"text": text, "source": str(record.get("source") or "")}
                try:
                    result = await pipeline.run_once([deal])
                except Exception as exc:
                    logger.exception("Pipeline failed for source %s message %s", source_key, message_id)
                    _record_source_activity(
                        source_key, message_id=message_id, failed=1,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    retried += 1
                    break
                posted, failed = _delivery_counts(result)
                if _failed_delivery(result):
                    # Some target(s) may have succeeded; pipeline dedup makes
                    # their retry idempotent. Keep this source cursor unchanged
                    # so the failed target gets another attempt next poll.
                    logger.warning(
                        "Delivery failure for source %s message %s; cursor held for retry",
                        source_key, message_id,
                    )
                    failure_note = _first_failure_reason(result)
                    _record_source_activity(
                        source_key, message_id=message_id, posted=posted,
                        failed=max(1, failed), error=failure_note,
                    )
                    retried += 1
                    break
                _record_source_activity(
                    source_key, message_id=message_id, posted=posted, failed=failed
                )

            # Empty/media-only Telegram posts have no text parser input; mark
            # them handled so they cannot block later deals forever.
            db.set_worker_offset(source_key, message_id)
""",
        'influencer_hub/worker.py: hunk 2',
    ),
    (
        'dashboard/app.py',
        """    ("Plain info link", "https://example.com/deal-news", "other"),
)


def _earnkaro_ready() -> bool:
    return bool(db.get_global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY)


""",
        '    ("Plain info link", "https://example.com/deal-news", "other"),\n)\n\n\ndef _flow_snapshot(stall_seconds: int = 3 * 3600, window_hours: int = 24) -> dict:\n    """Is posting still flowing from the sources? One honest answer.\n\n    Sources that have produced a deal before but nothing for `stall_seconds` are\n    marked stalled, channels show their last successful post, and the worker\'s\n    heartbeat says whether anything is pulling at all. Pure reads; no network.\n    """\n    now = time.time()\n    note_list: list[str] = []\n\n    worker = db.get_worker_heartbeat()\n    worker_age = None\n    if worker and worker.get("heartbeat_at"):\n        worker_age = max(0.0, now - float(worker["heartbeat_at"]))\n    worker_alive = worker_age is not None and worker_age <= max(\n        120, 3 * int(getattr(config, "DEAL_WORKER_POLL_INTERVAL", 30) or 30)\n    )\n    if worker is None:\n        note_list.append(\n            "The deal worker has never reported in. Posts cannot flow until the "\n            "influencer-deal-worker service is running."\n        )\n    elif not worker_alive:\n        note_list.append(\n            f"No fresh worker heartbeat ({int(worker_age or 0)}s ago). Check "\n            "systemctl status influencer-deal-worker."\n        )\n\n    try:\n        sources = db.list_sources(active_only=False)\n    except Exception:\n        sources = []\n    active_specs = [\n        str(source.get("spec") or "").strip()\n        for source in sources\n        if source.get("active")\n    ]\n    activity = {row["source_key"]: row for row in db.list_source_activity()}\n    flow_sources: list[dict] = []\n    for spec in active_specs:\n        row = activity.get(spec) or {}\n        last_seen = float(row.get("last_seen_at") or 0)\n        delivered = bool(row)\n        flow_sources.append({\n            "spec": spec,\n            "deals_seen": int(row.get("deals_seen") or 0),\n            "posts_dispatched": int(row.get("posts_dispatched") or 0),\n            "failures": int(row.get("failures") or 0),\n            "last_error": str(row.get("last_error") or ""),\n            "last_seen_at": last_seen or None,\n            "age_seconds": int(now - last_seen) if last_seen else None,\n            "never_delivered": not delivered,\n            "stalled": delivered and (now - last_seen) > stall_seconds,\n        })\n    flow_sources.sort(\n        key=lambda item: (\n            item["never_delivered"],\n            item["age_seconds"] if item["age_seconds"] is not None else 0,\n        ),\n        reverse=True,\n    )\n    stalled = [row for row in flow_sources if row["stalled"]]\n    never = [row for row in flow_sources if row["never_delivered"]]\n    if active_specs and never:\n        note_list.append(\n            f"{len(never)} of {len(active_specs)} active sources have not delivered a "\n            "deal yet — they may not be joined by the Telegram account, or the "\n            "source is quiet."\n        )\n    if stalled:\n        note_list.append(\n            f"{len(stalled)} source(s) delivered before but nothing for over "\n            f"{stall_seconds // 3600}h."\n        )\n\n    try:\n        channels = db.channel_post_activity(hours=window_hours)\n    except Exception:\n        channels = []\n    live_channels = [\n        channel for channel in channels\n        if str(channel.get("status") or "").strip().lower() in {"ready", "active"}\n    ]\n    posted_window = sum(int(channel.get("posted_in_window") or 0) for channel in channels)\n    failed_total = sum(int(channel.get("failed_total") or 0) for channel in channels)\n    try:\n        hourly_loot = str(db.get_global_setting("hourly_loot_enabled", "0")).strip().lower() in {\n            "1", "true", "yes", "on"\n        }\n    except Exception:\n        hourly_loot = False\n    only_earning = bool(getattr(config, "ONLY_EARNING_DEALS", False))\n    if live_channels and not posted_window and worker_alive:\n        note_list.append(\n            f"No post in the last {window_hours}h across {len(live_channels)} ready "\n            "channel(s): either the sources are quiet or every deal was filtered."\n        )\n        if not hourly_loot:\n            note_list.append(\n                "The hourly loot sweep is off (global setting hourly_loot_enabled=1 "\n                "turns on an extra hourly pass over the sources)."\n            )\n\n    return {\n        "ok": True,\n        "generated_at": now,\n        "worker": {\n            "state": (worker or {}).get("state") or "not_reporting",\n            "alive": worker_alive,\n            "age_seconds": int(worker_age) if worker_age is not None else None,\n            "last_poll_at": (worker or {}).get("last_poll_at") or None,\n            "last_error_code": (worker or {}).get("last_error_code") or "",\n            "poll_interval_seconds": int(getattr(config, "DEAL_WORKER_POLL_INTERVAL", 30) or 30),\n        },\n        "sources": {\n            "active": len(active_specs),\n            "paused": max(0, len(sources) - len(active_specs)),\n            "delivered": len([row for row in flow_sources if not row["never_delivered"]]),\n            "stalled": len(stalled),\n            "never_delivered": len(never),\n            "rows": flow_sources,\n        },\n        "channels": {\n            "ready": len(live_channels),\n            "posted_in_window": posted_window,\n            "failed_total": failed_total,\n            "window_hours": window_hours,\n            "rows": channels,\n        },\n        "settings": {\n            "hourly_loot_enabled": hourly_loot,\n            "only_earning_deals": only_earning,\n        },\n        "notes": note_list,\n        "stall_seconds": stall_seconds,\n    }\n\n\ndef _earnkaro_ready() -> bool:\n    return bool(db.get_global_setting("earnkaro_api_key", "") or config.EARNKARO_API_KEY)\n\n\n',
        'dashboard/app.py: hunk 1',
    ),
    (
        'dashboard/app.py',
        """        errors=errors,
        switches=ROUTING_SWITCHES,
        source_count=len(sources),
        active_source_count=active_sources,
    )


def _apply_easy_setup(values: dict, approval_ident: str, main_ident: str) -> dict:
""",
        """        errors=errors,
        switches=ROUTING_SWITCHES,
        source_count=len(sources),
        active_source_count=active_sources,
        flow=_flow_snapshot(),
    )


def _apply_easy_setup(values: dict, approval_ident: str, main_ident: str) -> dict:
""",
        'dashboard/app.py: hunk 2',
    ),
    (
        'dashboard/app.py',
        """        "updated": updated,
        "existing_channels": len(channels),
        "active_sources": sum(1 for source in sources if source.get("active")),
    }


@app.route("/api/routing-preview")
def routing_preview_api():
""",
        '        "updated": updated,\n        "existing_channels": len(channels),\n        "active_sources": sum(1 for source in sources if source.get("active")),\n    }\n\n\n@app.route("/api/flow")\ndef api_flow():\n    """Read-only deal-flow status: worker, sources, channels (no network calls)."""\n    try:\n        stall_seconds = max(600, int(request.args.get("stall_seconds", 3 * 3600)))\n    except (TypeError, ValueError):\n        stall_seconds = 3 * 3600\n    try:\n        window_hours = max(1, min(168, int(request.args.get("window_hours", 24))))\n    except (TypeError, ValueError):\n        window_hours = 24\n    return jsonify(_flow_snapshot(stall_seconds=stall_seconds, window_hours=window_hours))\n\n\n@app.route("/api/routing-preview")\ndef routing_preview_api():\n',
        'dashboard/app.py: hunk 3',
    ),
    (
        'dashboard/templates/easy_setup.html',
        """    <button type="submit" class="btn btn-primary">💾 Save &amp; start posting</button>
    <span class="hint">Easy Setup saves immediately — no password. Only removals (delete a channel, a creator or a deal source) ask for the password, once per unlock window.</span>
  </div>
</form>
{% endblock %}
""",
        """    <button type="submit" class="btn btn-primary">💾 Save &amp; start posting</button>
    <span class="hint">Easy Setup saves immediately — no password. Only removals (delete a channel, a creator or a deal source) ask for the password, once per unlock window.</span>
  </div>
</form>

{% if flow %}
<section class="easy-card" id="deal-flow">
  <h2>🔄 Deal flow — sources → posts</h2>
  <p class="hint">
    {% if flow.worker.alive %}
      ✅ Worker <strong>{{ flow.worker.state }}</strong> — last poll
      {{ flow.worker.age_seconds }}s ago, checking every
      {{ flow.worker.poll_interval_seconds }}s.
    {% else %}
      ⚠️ Worker <strong>{{ flow.worker.state }}</strong>{% if flow.worker.age_seconds %} (last seen {{ flow.worker.age_seconds }}s ago){% endif %} — posts cannot flow without it.
      <code>sudo systemctl status influencer-deal-worker</code>
    {% endif %}
  </p>
  <p class="hint">
    Sources: <strong>{{ flow.sources.active }} active</strong>
    ({{ flow.sources.paused }} paused) ·
    <strong>{{ flow.sources.delivered }}</strong> delivered a deal ·
    {{ flow.sources.never_delivered }} never delivered ·
    {{ flow.sources.stalled }} stalled.
    Channels: <strong>{{ flow.channels.ready }} ready</strong> ·
    {{ flow.channels.posted_in_window }} posted and
    {{ flow.channels.failed_total }} failed (all-time).
    Hourly loot sweep: <strong>{{ 'on' if flow.settings.hourly_loot_enabled else 'off' }}</strong>.
  </p>
  {% for note in flow.notes %}
    <p class="routing-note is-warn">⚠️ {{ note }}</p>
  {% endfor %}

  <details class="easy-advanced">
    <summary>Per-source detail (advanced)</summary>
    <table class="routing-table">
      <thead>
        <tr><th>Source</th><th>Last deal</th><th>Deals</th><th>Posted</th><th>State</th></tr>
      </thead>
      <tbody>
        {% for row in flow.sources.rows %}
          <tr class="routing-row is-{{ 'warn' if row.stalled or row.never_delivered else 'ok' }}">
            <td><code>{{ row.spec }}</code></td>
            <td>{% if row.age_seconds is not none %}{{ (row.age_seconds // 3600) }}h {{ ((row.age_seconds % 3600) // 60) }}m ago{% else %}—{% endif %}</td>
            <td>{{ row.deals_seen }}</td>
            <td>{{ row.posts_dispatched }}</td>
            <td>
              {% if row.stalled %}⚠️ stalled{% elif row.never_delivered %}· no deal yet{% elif row.failures %}⚠️ {{ row.failures }} failure(s){% else %}✅ flowing{% endif %}
              {% if row.last_error %}<span class="routing-note">{{ row.last_error }}</span>{% endif %}
            </td>
          </tr>
        {% else %}
          <tr><td colspan="5">No active sources yet. Add them above or in Vault &amp; Sources.</td></tr>
        {% endfor %}
      </tbody>
    </table>

    <table class="routing-table">
      <thead>
        <tr><th>Channel</th><th>Last post</th><th>Posted {{ flow.channels.window_hours }}h</th><th>Failed (all-time)</th></tr>
      </thead>
      <tbody>
        {% for row in flow.channels.rows %}
          <tr class="routing-row is-{{ 'warn' if row.failed_total else 'ok' }}">
            <td><strong>{{ row.influencer_name }}</strong><code>{{ row.platform }} · {{ row.identifier }}</code></td>
            <td>{{ row.last_posted_at or '—' }}</td>
            <td>{{ row.posted_in_window }}</td>
            <td>{{ row.failed_total }}</td>
          </tr>
        {% else %}
          <tr><td colspan="4">No channels configured yet.</td></tr>
        {% endfor %}
      </tbody>
    </table>
    <p class="hint">Machine-readable: <code>/api/flow</code> (add <code>?stall_seconds=</code> or <code>?window_hours=</code>).</p>
  </details>
</section>
{% endif %}
{% endblock %}
""",
        'dashboard/templates/easy_setup.html: hunk 1',
    ),
]


def _stage(path: Path, pending: dict[Path, str]) -> str:
    text = pending.get(path)
    if text is None:
        if not path.is_file():
            raise PatchError(f"missing file {path.relative_to(REPO_ROOT)}")
        text = path.read_text(encoding="utf-8")
        pending[path] = text
    return text


def apply_edits(pending: dict[Path, str], stats: list[str]) -> None:
    for relative, old, new, label in EDITS:
        path = REPO_ROOT / relative
        text = _stage(path, pending)
        count = text.count(old)
        if count != 1:
            raise PatchError(
                f"{label}: expected 1 match in {relative}, found {count}. "
                "This file is not the revision the patch was verified against."
            )
        pending[path] = text.replace(old, new, 1)
        stats.append(f"edit  {label}")


def flow_in_place() -> bool:
    """True when the deal-flow board is already installed."""
    app_path = REPO_ROOT / "dashboard" / "app.py"
    db_path = REPO_ROOT / "influencer_hub" / "db.py"
    if not app_path.is_file() or not db_path.is_file():
        return False
    return (
        FLOW_MARKER in app_path.read_text(encoding="utf-8")
        and DB_MARKER in db_path.read_text(encoding="utf-8")
    )


def copy_test_updates(stats: list[str]) -> None:
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.is_dir():
        stats.append("skip  tests/ not present on this checkout")
        return
    if not TESTS_DIR.is_dir():
        raise PatchError("patches/tests/ is missing; re-download the patch bundle")
    for source in sorted(TESTS_DIR.glob("test_*.py")):
        target = tests_dir / source.name
        action = "update" if target.exists() else "add"
        shutil.copyfile(source, target)
        stats.append(f"{action}  tests/{source.name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the deal-flow board (sources -> posts, visible)."
    )
    parser.add_argument("--check", action="store_true",
                        help="report whether the flow board is in place; write nothing")
    parser.add_argument("--with-tests", action="store_true",
                        help="also install tests from patches/tests/ (optional)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate every edit, write nothing")
    args = parser.parse_args(argv)

    if flow_in_place():
        if args.with_tests:
            try:
                test_stats: list[str] = []
                copy_test_updates(test_stats)
            except PatchError as error:
                print(f"\u26a0\ufe0f  Test refresh skipped: {error}", file=sys.stderr)
            else:
                for line in test_stats:
                    print(f"   {line}")
        print(
            "\u2705 Deal-flow board already applied \u2014 nothing to do.\n"
            "   (Per-source activity, /api/flow and the Easy Setup card are present.)\n"
            "   Double-applying is refused on purpose."
        )
        return 0 if args.check else 1

    pending: dict[Path, str] = {}
    stats: list[str] = []
    try:
        apply_edits(pending, stats)
    except PatchError as error:
        print(f"\u274c Aborted, nothing was written:\n   {error}", file=sys.stderr)
        return 2

    for line in stats:
        print(f"   {line}")

    if args.dry_run:
        print("\U0001f50e Dry run complete \u2014 all edits validated, nothing written.")
        return 0

    for path, text in pending.items():
        path.write_text(text, encoding="utf-8")
    print(f"\u2705 Deal-flow board applied ({len(pending)} files edited).")

    if args.with_tests:
        try:
            test_stats = []
            copy_test_updates(test_stats)
        except PatchError as error:
            print(f"\u26a0\ufe0f  Test refresh skipped: {error}", file=sys.stderr)
        else:
            for line in test_stats:
                print(f"   {line}")

    print(
        "\nNext:\n"
        "   sudo systemctl restart influencer-deal-worker influencer-dashboard\n"
        "   curl -s localhost:5000/api/flow | head -c 400\n"
        "   pytest -q tests/test_deal_flow.py\n"
        "\n"
        "On the dashboard, Easy Setup now ends with a \"Deal flow\" card:\n"
        "per-source last deal, posted/failed per channel, worker heartbeat.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
