"""Self-check: does the hub actually work, told plainly and without secrets.

Every check is read-only and answers one question an operator asks before
trusting a hub with a creator's money:

* is the database there, and has it been migrated (mirror + pairing columns,
  the dashboard index)?
* is there a username for Telegram, a tag for Amazon, a key for EarnKaro, a
  store for HYPD — and is the Telegram session authorized?
* is the worker alive, and are deal sources switched on?
* is the WhatsApp hub reachable, and how many creators still need a phone?
* is the daily history being written (that is what the trend line and the
  7-day view read)?
* do the posts we really sent carry each creator's own Amazon tag (a link with
  someone else's tag pays the wrong person)?
* which creators are ready to post, which are blocked, and is anything still
  signed with the shared Amazon tag?

Results are JSON-serialisable and contain only booleans, counts and short
messages: never an API key, a session string or a token value. ``live=True``
adds the one network call (the loopback WhatsApp hub health) — everything else
is read from the database and configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import accounts, config, db, readiness

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_STATE_ORDER = {FAIL: 3, WARN: 2, OK: 1, SKIP: 0}


def _check(check_id: str, label: str, state: str, detail: str,
           fix: str = "", group: str = "core") -> dict:
    return {
        "id": check_id,
        "label": label,
        "state": state,
        "detail": detail,
        "fix": fix,
        "group": group,
    }


def _has(value: Any) -> bool:
    return bool(str(value or "").strip())


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------
def _schema_check() -> dict:
    """The deployed database must carry every column this build expects."""
    try:
        con = db._connect()
    except Exception as exc:  # pragma: no cover - unreadable DB
        return _check("schema", "Database & schema", FAIL,
                      f"The database could not be opened: {type(exc).__name__}.",
                      "Check HUB_DB_PATH and that the file is writable by the "
                      "service user, then restart the dashboard.")
    try:
        tables = {row["name"] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        columns: dict[str, set[str]] = {}
        for table in ("influencers", "channels", "posts", "wa_sessions"):
            if table in tables:
                columns[table] = {row["name"] for row in
                                  con.execute(f"PRAGMA table_info({table})")}
        indexes = {row["name"] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        con.close()

    missing = []
    for table, wanted in (
        ("influencers", {"amazon_tag", "allow_amazon", "allow_earnkaro", "allow_hypd"}),
        ("channels", {"role", "mirror_whatsapp", "wa_session_key", "status"}),
        ("posts", {"mirror_source_id", "deal_text"}),
        ("wa_sessions", {"pairing_phone", "pairing_code", "pairing_error"}),
    ):
        if table not in tables:
            missing.append(f"table {table}")
            continue
        absent = wanted - columns.get(table, set())
        missing.extend(f"{table}.{name}" for name in sorted(absent))

    if missing:
        return _check("schema", "Database & schema", FAIL,
                      "Missing: " + ", ".join(missing) + ".",
                      "Run `PYTHONPATH=. .venv/bin/python -c \"from influencer_hub "
                      "import db; db.init()\"` once, then restart the dashboard "
                      "and the worker.")
    fast_index = "idx_posts_channel_status" in indexes
    return _check(
        "schema", "Database & schema", OK if fast_index else WARN,
        "Every column is present"
        + ("" if fast_index else " (the posts index for mirror counts is missing)"),
        "" if fast_index else "Run db.init() once; the index is created by migrate().",
    )


def _telegram_check() -> dict:
    name = str(config.TELEGRAM_SESSION or "").strip()
    api_id = _has(config.TELEGRAM_API_ID)
    api_hash = _has(config.TELEGRAM_API_HASH)
    if not (api_id and api_hash):
        return _check("telegram", "Telegram account", FAIL,
                      "TELEGRAM_API_ID / TELEGRAM_API_HASH are not set.",
                      "Fill them in .env (my.telegram.org), then run "
                      "`python -m influencer_hub.cli doctor`.")
    base = Path(config.TELEGRAM_SESSION).expanduser()
    if not base.is_absolute():
        base = config.BASE_DIR / "influencer_hub" / base
    if base.suffix != ".session":
        base = Path(str(base) + ".session")
    session_files = sorted(base.parent.glob(f"{base.stem}*.session")) if base.parent.exists() else []
    if not session_files:
        return _check("telegram", "Telegram account", WARN,
                      f"Credentials are present, but no session file for "
                      f"“{name}” was found in influencer_hub/.",
                      "Authorize once (the CLI prompts for the login code): "
                      "`python -m influencer_hub.cli authorize` — every Telegram "
                      "post needs this session.")
    return _check("telegram", "Telegram account", OK,
                  f"Credentials and a saved session for “{name}” are in place.")


def _amazon_check(plan: dict | None) -> dict:
    tag = str(config.AMAZON_ASSOCIATE_TAG or "").strip()
    if not tag:
        return _check("amazon", "Amazon attribution", FAIL,
                      "No AMAZON_ASSOCIATE_TAG is configured.",
                      "Set the fallback tag in .env, then save every creator's own "
                      "tag in Easy Setup.")
    shared = 0
    if plan:
        shared = int(plan["summary"].get("shared_tag") or 0)
    if shared:
        return _check("amazon", "Amazon attribution", WARN,
                      f"Fallback tag {tag} is set, but {shared} creator(s) still "
                      f"post under that shared id.",
                      "Open the dashboard → 🏷️ Shared Amazon tag filter, then save "
                      "each creator's own Associate tag.")
    return _check("amazon", "Amazon attribution", OK,
                  f"Fallback tag {tag} is set and no creator is on the shared id.")


def _central_accounts_check() -> dict:
    key = accounts.central_earnkaro_api_key()
    publisher = accounts.central_earnkaro_publisher_id()
    store = accounts.central_hypd_store_id()
    problems = []
    if not key:
        problems.append("no EarnKaro API key")
    if not publisher:
        problems.append("no EarnKaro publisher id")
    if not store:
        problems.append("no HYPD store id")
    if problems:
        return _check("central", "Central EarnKaro / HYPD", WARN,
                      "Missing: " + ", ".join(problems) + ".",
                      "Save them once in Vault & Sources (they are shared by all "
                      "creators — never per creator).")
    return _check("central", "Central EarnKaro / HYPD", OK,
                  f"EarnKaro key + publisher id and HYPD store {store} are set "
                  f"for central attribution.")


def _worker_check(heartbeat: dict | None) -> dict:
    if not heartbeat:
        return _check("worker", "Background worker", FAIL,
                      "The worker has never reported a heartbeat.",
                      "Start it: `sudo systemctl start influencer-deal-worker` "
                      "(the provided deploy unit).")
    state = str(heartbeat.get("state") or "").lower()
    alive = state in {"running", "idle", "polling", "healthy"}
    detail = f"Worker state: {state or 'unknown'}"
    if heartbeat.get("last_poll_at"):
        detail += f" · last poll {heartbeat['last_poll_at']}"
    if heartbeat.get("last_error_code"):
        detail += f" · last error {heartbeat['last_error_code']}"
    if alive:
        return _check("worker", "Background worker", OK, detail)
    return _check("worker", "Background worker", WARN, detail,
                  "Check `sudo journalctl -u influencer-deal-worker -n 50` "
                  "and restart the service.")


def _sources_check(sources: list[dict]) -> dict:
    active = [row for row in sources if row.get("active")]
    if not active:
        return _check("sources", "Deal sources", WARN,
                      f"{len(sources)} source(s) configured, none switched on.",
                      "Open Vault & Sources and switch them on (or press “Add "
                      "recommended sources” in Easy Setup).")
    return _check("sources", "Deal sources", OK,
                  f"{len(active)} of {len(sources)} source(s) active.")


def _creators_check(plan: dict | None) -> dict:
    if not plan:
        return _check("creators", "Creator readiness", SKIP,
                      "No creators are configured yet.",
                      "Add one in Easy Setup, or paste a whole list in bulk.")
    summary = plan["summary"]
    total = summary["creators"]
    ready = summary["ready"]
    blocked = summary["blocked"]
    if not total:
        return _check("creators", "Creator readiness", SKIP,
                      "No creators are configured yet.",
                      "Add one in Easy Setup, or paste a whole list in bulk.")
    if blocked:
        return _check("creators", "Creator readiness", WARN,
                      f"{ready} of {total} ready to post · {blocked} blocked",
                      "Dashboard → filter ⚠️ Blocked → do the one action listed "
                      "for each creator (mostly: pair the phone, then send the "
                      "Test post).")
    return _check("creators", "Creator readiness", OK,
                  f"All {total} creator(s) are ready to post.")


def _mirror_check(plan: dict | None) -> dict:
    from . import pipeline

    global_default = pipeline.mirror_main_to_whatsapp_enabled()
    if not plan:
        return _check("mirror", "Main → WhatsApp mirror",
                      OK if global_default else WARN,
                      "Mirror default is " + ("ON" if global_default else "OFF")
                      + "; no creators configured yet.")
    summary = plan["summary"]
    mirroring = summary.get("mirroring") or 0
    if not global_default and not mirroring:
        return _check("mirror", "Main → WhatsApp mirror", WARN,
                      "The global mirror switch is OFF and no creator mirrors.",
                      "Turn it on in Setup Center → Main Channel → WhatsApp Mirror "
                      "(or per creator on their page).")
    if not mirroring:
        return _check("mirror", "Main → WhatsApp mirror", WARN,
                      "Default is ON, but no creator is mirroring yet — a "
                      "WhatsApp destination must pass a visible Test post first.",
                      "Pair a creator's phone, paste their WhatsApp link, then "
                      "press Test.")
    return _check("mirror", "Main → WhatsApp mirror", OK,
                  f"{mirroring} creator(s) mirror main-channel deals to WhatsApp.")


def _whatsapp_check(live: bool) -> dict:
    if not _has(config.WA_HUB_URL):
        return _check("whatsapp", "WhatsApp hub", FAIL,
                      "WA_HUB_URL is not configured.",
                      "Set WA_HUB_URL (default http://127.0.0.1:8088) and start "
                      "the wa_hub service.")
    if not live:
        return _check("whatsapp", "WhatsApp hub", SKIP,
                      f"Not checked (hub {config.WA_HUB_URL}); add ?live=1 to "
                      f"this endpoint to ping it.")
    import urllib.request

    headers = {"Accept": "application/json"}
    if config.WA_HUB_TOKEN:
        headers["Authorization"] = f"Bearer {config.WA_HUB_TOKEN}"
    try:
        request = urllib.request.Request(
            f"{config.WA_HUB_URL.rstrip('/')}/health", headers=headers, method="GET"
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = response.read()
        import json

        data = json.loads(payload or b"{}")
        sessions = len(data.get("sessions") or [])
        return _check("whatsapp", "WhatsApp hub", OK,
                      f"Hub answered at {config.WA_HUB_URL} ({sessions} session(s)).")
    except Exception as exc:
        unauthorized = "401" in str(exc) or "403" in str(exc)
        return _check(
            "whatsapp", "WhatsApp hub", FAIL,
            f"Hub did not answer correctly: {type(exc).__name__}"
            + (" (unauthorized)" if unauthorized else "") + ".",
            "Check `sudo systemctl status influencer-wa-hub`, and that WA_HUB_TOKEN "
            "matches the hub's token."
            + ("" if config.WA_HUB_TOKEN else " WA_HUB_TOKEN is empty here."),
        )


def _dashboard_check() -> dict:
    try:
        password = str(config.DASHBOARD_ADMIN_PASSWORD or "")
    except Exception:  # pragma: no cover - defensive
        password = ""
    if not password:
        return _check("dashboard", "Dashboard access", FAIL,
                      "No dashboard/admin password is configured.",
                      "Set DASHBOARD_ADMIN_PASSWORD in .env and restart; removals "
                      "ask for it once.")
    if config.HUB_ENV != "production" and not config.DASHBOARD_COOKIE_SECURE:
        return _check("dashboard", "Dashboard access", WARN,
                      f"Password set, but HUB_ENV={config.HUB_ENV} so the sign-in "
                      f"cookie is not marked Secure.",
                      "Put HTTPS in front (or Tailscale) and set "
                      "HUB_ENV=production on the VM.")
    return _check("dashboard", "Dashboard access", OK,
                  "Admin password is set and the session cookie is production-grade.")


def _amazon_links_check() -> dict:
    """The promise to every creator: their own Associates tag on every link."""
    from . import amazon_watch

    report = amazon_watch.attribution_report(hours=48, limit=300)
    if not report["posts_with_amazon"]:
        return _check(
            "amazon_links", "Amazon links in real posts", SKIP,
            f"No Amazon post in the last 48h to check ({report['posts_scanned']} post(s) read).",
            "Nothing to fix — this check reads the deals that were actually sent.",
        )
    if report["foreign_tag"]:
        first = report["problems"][0]
        return _check(
            "amazon_links", "Amazon links in real posts", FAIL,
            f"{report['foreign_tag']} of {report['links']} Amazon link(s) carried a tag that "
            f"is not the creator's — e.g. {first['creator']} got '{first['found_tag']}' "
            f"instead of '{first['expected_tag']}'.",
            "That link pays someone else for this creator's subscribers. Check where it came "
            "from: a long Amazon link is retagged automatically, but an opaque amzn.to/a.co "
            "short code is only re-signed at send time — resolve it or ask the source to post "
            "the long product link.",
        )
    detail = (f"{report['own_tag']} of {report['links']} Amazon link(s) in "
              f"{report['posts_with_amazon']} post(s) carry the creator's own tag.")
    if report["untagged"]:
        return _check(
            "amazon_links", "Amazon links in real posts", WARN,
            detail + f" {report['untagged']} link(s) carry no tag at all.",
            "An untagged Amazon link pays nobody. If the source wrote it that way, the hub "
            "adds the creator's tag when it lands on a /dp/ product page — check those posts.",
        )
    if report["short_links"]:
        return _check(
            "amazon_links", "Amazon links in real posts", WARN,
            detail + f" {report['short_links']} were short codes (amzn.to/a.co).",
            "A short code decides its own attribution. The hub tries to resolve it before "
            "sending; where it could not, prefer sources that post the long link.",
        )
    return _check("amazon_links", "Amazon links in real posts", OK,
                  detail + " Every one is signed with its creator's own tag.")


def _history_check() -> dict:
    """The daily snapshot is what turns "how is it now" into a trend."""
    if not db.snapshot_table_exists():
        return _check(
            "history", "Ops history", WARN,
            "The ops_snapshots table is missing, so the trend cannot be drawn.",
            "Run `PYTHONPATH=. .venv/bin/python -c \"from influencer_hub import db; "
            "db.init()\"` once; the table is part of the schema.",
        )
    rows = db.list_ops_snapshots(days=400)
    if not rows:
        return _check(
            "history", "Ops history", WARN,
            "No daily snapshot stored yet — the trend line is empty.",
            "Add one cron line: `PYTHONPATH=. .venv/bin/python -m influencer_hub.cli "
            "snapshot >> /var/log/influencer-snapshot.log 2>&1` (or send the daily "
            "digest, which stores the same row).",
        )
    last = rows[-1]
    captured = str(last.get("captured_at") or "")
    age_hours = None
    try:
        moment = datetime.fromisoformat(captured)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        age_hours = max(0, int((datetime.now(timezone.utc) - moment).total_seconds() // 3600))
    except (TypeError, ValueError):
        age_hours = None
    if age_hours is not None and age_hours > 48:
        return _check(
            "history", "Ops history", WARN,
            f"The last snapshot is {age_hours}h old ({len(rows)} day(s) stored).",
            "Check the snapshot cron/timer — the hub cannot show a trend if the "
            "row is not written: `python -m influencer_hub.cli snapshot`.",
        )
    when = "just now" if age_hours in (None, 0) else f"{age_hours}h ago"
    return _check("history", "Ops history", OK,
                  f"{len(rows)} day(s) stored · last snapshot {when} "
                  f"({last.get('posted', 0)} posted / {last.get('failed', 0)} failed).")


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------
def run_checks(*, live: bool = False, influencers: list[dict] | None = None,
               sources: list[dict] | None = None,
               heartbeat: dict | None = None, plan: dict | None = None) -> dict:
    """Run every check and return ``{ok, verdict, checks, summary}``.

    ``live`` performs the single network call (the loopback WhatsApp hub). The
    caller may pass already-read rows (the dashboard does) so a page render
    does not repeat queries.
    """
    if influencers is None:
        try:
            influencers = db.list_influencers()
        except Exception:  # pragma: no cover - defensive
            influencers = []
    if sources is None:
        try:
            sources = db.list_sources(active_only=False)
        except Exception:  # pragma: no cover - defensive
            sources = []
    if heartbeat is None:
        try:
            heartbeat = db.get_worker_heartbeat()
        except Exception:  # pragma: no cover - defensive
            heartbeat = None
    if influencers is None or sources is None or heartbeat is None:
        # Reads already failed above (fresh or unreadable database); report it in
        # the schema check instead of raising here.
        pass
    if plan is None and influencers:
        try:
            plan = readiness.overview(influencers=influencers)
        except Exception:  # pragma: no cover - defensive
            plan = None
    if plan is not None:
        try:
            # The dashboard builds its plan with a query/limit for the table;
            # the checks need the hub-wide picture.
            plan = {**plan, "summary": plan["summary"]}
        except Exception:  # pragma: no cover - defensive
            plan = None

    # Every check is guarded: a missing table or an unreadable file must show up
    # as a failed check with a fix, never as a traceback in a cron log.
    def _guard(check_id: str, label: str, run) -> dict:
        try:
            return run()
        except Exception as exc:  # pragma: no cover - defensive
            return _check(check_id, label, FAIL,
                          f"Check could not run: {type(exc).__name__}: {str(exc)[:120]}",
                          "Run `PYTHONPATH=. .venv/bin/python -c \"from influencer_hub "
                          "import db; db.init()\"` and check HUB_DB_PATH / file "
                          "permissions, then re-run this check.")

    checks = [
        _guard("schema", "Database & schema", _schema_check),
        _guard("telegram", "Telegram account", _telegram_check),
        _guard("dashboard", "Dashboard access", _dashboard_check),
        _guard("worker", "Background worker", lambda: _worker_check(heartbeat)),
        _guard("sources", "Deal sources", lambda: _sources_check(sources)),
        _guard("amazon", "Amazon attribution", lambda: _amazon_check(plan)),
        _guard("central", "Central EarnKaro / HYPD", _central_accounts_check),
        _guard("creators", "Creator readiness", lambda: _creators_check(plan)),
        _guard("mirror", "Main → WhatsApp mirror", lambda: _mirror_check(plan)),
        _guard("history", "Ops history", _history_check),
        _guard("amazon_links", "Amazon links in real posts", _amazon_links_check),
        _guard("whatsapp", "WhatsApp hub", lambda: _whatsapp_check(live)),
    ]
    worst = max((_STATE_ORDER.get(check["state"], 0) for check in checks), default=0)
    verdict = {3: "fail", 2: "attention", 1: "ready", 0: "unknown"}[worst]
    counts = {state: sum(1 for c in checks if c["state"] == state)
              for state in (OK, WARN, FAIL, SKIP)}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "live": bool(live),
        "verdict": verdict,
        "ok": worst < _STATE_ORDER[FAIL],
        "checks": checks,
        "summary": counts,
        "next": [check for check in checks if check["state"] in {FAIL, WARN}],
    }
