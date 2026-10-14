"""Self-check: the plain-language "does everything work?" report.

These tests pin the two things that make such a panel trustworthy: it must be
honest (a missing key, a dead worker or an unpaired phone is never reported as
"ready") and it must never leak a credential, while staying read-only.
"""
from __future__ import annotations

import json
import pathlib

import pytest

import dashboard.app as dashboard_module
from dashboard.app import app
from influencer_hub import config, db, selfcheck

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "selfcheck.sqlite3")
    monkeypatch.setitem(app.config, "TESTING", True)
    db.init()
    return app.test_client()


def _states(report: dict) -> dict[str, str]:
    return {check["id"]: check["state"] for check in report["checks"]}


def _ready_hub(monkeypatch, tmp_path, tag="ravi-21"):
    """Make every local check pass: worker, source, keys, creator, phone."""
    monkeypatch.setattr(config, "AMAZON_ASSOCIATE_TAG", "mama086-21")
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "long-enough-password")
    # A provisioned VM: API credentials, an authorized session file, HTTPS in
    # front (production cookie flags). Without these the report is *supposed*
    # to complain, which the other tests assert.
    # Access is part of a provisioned hub too: the team has its own add-only
    # password and the admin signs in with a second factor.
    from influencer_hub import security

    monkeypatch.setattr(config, "DASHBOARD_TEAM_PASSWORD", "team-password-16chars")
    monkeypatch.setattr(config, "ADMIN_TOTP_SECRET", security.new_totp_secret())
    monkeypatch.setattr(config, "TELEGRAM_API_ID", "12345")
    monkeypatch.setattr(config, "TELEGRAM_API_HASH", "abcdef0123456789")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    session_dir = tmp_path / "influencer_hub"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"{config.TELEGRAM_SESSION}.session").write_bytes(b"")
    monkeypatch.setattr(config, "HUB_ENV", "production")
    monkeypatch.setattr(config, "DASHBOARD_COOKIE_SECURE", True)
    db.set_global_setting("earnkaro_api_key", "ek-secret-value")
    db.set_global_setting("earnkaro_publisher_id", "999")
    db.set_global_setting("hypd_store_id", "93944")
    db.record_worker_heartbeat("running")
    if not db.list_sources(active_only=False):
        db.add_source("Power Loot", "@powerloot")
    inf_id = db.add_influencer("Ready Creator", tag)
    db.add_channel(inf_id, "telegram", "@ready_loots", status="ready", role="broadcast")
    db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                   status="ready", role="whatsapp")
    key = f"inf-{inf_id}-wa"
    db.upsert_wa_session(inf_id, key, phone="919876543210")
    db.set_wa_session_status(key, "connected")
    # A provisioned hub also runs the nightly snapshot (that is what the trend
    # reads), so "ready" stays reachable without weakening the check.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    db.record_ops_snapshot(now.strftime("%Y-%m-%d"),
                           captured_at=now.isoformat(timespec="seconds"),
                           posted=1, attempts=1, ready=1, verdict="ready")
    return inf_id


# --------------------------------------------------------------------------
# 1. Honesty
# --------------------------------------------------------------------------
def test_an_empty_hub_does_not_claim_everything_is_fine(client, monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_ADMIN_PASSWORD", "")

    report = selfcheck.run_checks(live=False)
    states = _states(report)

    assert report["ok"] is False
    assert report["verdict"] == "fail"
    assert states["dashboard"] == "fail"
    assert states["worker"] == "fail"      # never started
    assert states["sources"] == "warn"
    assert states["creators"] == "skip"    # nothing to check yet, not "ok"
    assert report["next"] and all(c["state"] in {"warn", "fail"} for c in report["next"])
    assert all(c["fix"] for c in report["next"])   # every problem names a fix


def test_a_fully_configured_hub_reads_ready(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)

    report = selfcheck.run_checks(live=False)
    states = _states(report)

    assert report["verdict"] == "ready", report["next"]
    assert report["ok"] is True
    assert states["worker"] == "ok"
    assert states["sources"] == "ok"
    assert states["creators"] == "ok"
    assert states["mirror"] == "ok"
    assert states["schema"] == "ok"
    assert states["central"] == "ok"
    assert report["next"] == []


def test_a_missing_credential_is_reported_without_being_echoed(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)
    db.set_global_setting("earnkaro_api_key", "")
    monkeypatch.setattr(config, "EARNKARO_API_KEY", "")

    report = selfcheck.run_checks(live=False)

    assert _states(report)["central"] == "warn"
    blob = json.dumps(report)
    assert "no EarnKaro API key" in blob
    assert "ek-secret-value" not in blob          # the key itself is never shown


def test_the_shared_amazon_tag_and_dead_worker_are_warnings_not_greens(client, monkeypatch, tmp_path):
    inf_id = _ready_hub(monkeypatch, tmp_path)
    db.update_influencer(inf_id, amazon_tag=config.AMAZON_ASSOCIATE_TAG)
    db.record_worker_heartbeat("degraded", error_code="UpdatePassword")

    report = selfcheck.run_checks(live=False)
    states = _states(report)

    assert states["amazon"] == "warn"
    assert states["worker"] == "warn"
    assert report["verdict"] in {"attention", "fail"}
    worker = next(c for c in report["checks"] if c["id"] == "worker")
    assert "UpdatePassword" in worker["detail"]


def test_a_broken_whatsapp_hub_is_a_live_only_failure(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)

    quiet = selfcheck.run_checks(live=False)
    assert _states(quiet)["whatsapp"] == "skip"
    assert quiet["verdict"] == "ready"      # nothing failed locally

    import urllib.request

    def exploding_urlopen(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", exploding_urlopen)
    live = selfcheck.run_checks(live=True)

    assert _states(live)["whatsapp"] == "fail"
    assert live["ok"] is False
    fix = next(c for c in live["checks"] if c["id"] == "whatsapp")["fix"]
    assert "influencer-wa-hub" in fix


def test_the_history_check_asks_for_the_cron_line_when_there_is_no_history(client, monkeypatch):
    db.record_worker_heartbeat("running")

    report = selfcheck.run_checks(live=False)
    history = next(c for c in report["checks"] if c["id"] == "history")

    assert history["state"] == "warn"
    assert "no daily snapshot" in history["detail"].lower()
    assert "cli snapshot" in history["fix"]


def test_checks_are_read_only(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)
    before = (
        len(db.list_influencers()), len(db.list_channels()), len(db.list_wa_sessions()),
        db.get_worker_heartbeat()["state"],
    )

    selfcheck.run_checks(live=False)

    after = (
        len(db.list_influencers()), len(db.list_channels()), len(db.list_wa_sessions()),
        db.get_worker_heartbeat()["state"],
    )
    assert before == after


# --------------------------------------------------------------------------
# 2. The endpoint and the dashboard panel
# --------------------------------------------------------------------------
def test_the_api_returns_the_same_report_json(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)

    payload = client.get("/api/self-check").get_json()

    assert payload["ok"] is True
    assert payload["verdict"] == "ready"
    assert {check["id"] for check in payload["checks"]} >= {
        "schema", "telegram", "dashboard", "worker", "sources", "amazon",
        "central", "creators", "mirror", "history", "amazon_links", "access",
        "whatsapp",
    }
    narrowed = client.get("/api/self-check?inf_id=1").get_json()
    assert narrowed["ok"] is True
    assert client.get("/api/self-check?inf_id=999999").status_code == 404


def test_the_dashboard_shows_a_self_check_panel_with_the_fixes(client, monkeypatch):
    inf_id = db.add_influencer("Blocked Creator", "blocked-21")
    db.add_channel(inf_id, "telegram", "@blocked_loots", status="ready", role="broadcast")

    body = client.get("/").get_data(as_text=True)

    assert "Self-check — does everything really work?" in body
    assert 'id="self-check"' in body
    assert "PROBLEMS" in body or "ATTENTION" in body
    assert "What to fix, in order" in body
    assert "systemctl start influencer-deal-worker" in body   # the named fix
    assert 'data-live-src="/api/self-check?live=1"' in body


def test_the_panel_never_renders_a_secret(client, monkeypatch, tmp_path):
    _ready_hub(monkeypatch, tmp_path)
    db.set_global_setting("earnkaro_api_key", "top-secret-token-123")

    body = client.get("/").get_data(as_text=True)

    assert "top-secret-token-123" not in body
    assert "ek-secret-value" not in body


# --------------------------------------------------------------------------
# 3. The deploy gate: a warning must never roll a deploy back
# --------------------------------------------------------------------------
def _synthetic_report(*, failures: int, warnings: int) -> dict:
    """The shape ``run_checks`` returns, with only the states that matter here."""
    checks = [{"id": f"fail{index}", "label": f"Broken {index}", "state": "fail",
               "detail": "it is broken", "fix": "the one command that fixes it",
               "ok": False}
              for index in range(failures)]
    checks += [{"id": f"warn{index}", "label": f"Amber {index}", "state": "warn",
                "detail": "not finished being set up", "fix": "the one command "
                "that fixes it", "ok": False}
               for index in range(warnings)]
    return {
        "generated_at": "2026-10-14T00:00:00+00:00",
        "live": False,
        "verdict": "fail" if failures else ("attention" if warnings else "ready"),
        "ok": not failures,
        "checks": checks,
        "summary": {"ok": 0, "warn": warnings, "fail": failures, "skip": 0},
        "next": checks,
    }


def test_only_a_real_failure_blocks_a_deploy(capsys, monkeypatch):
    """`--blocking` is the deploy's gate: ⛔ stops it, ⚠️ is advice, never a stop."""
    from influencer_hub import cli

    warn_only = _synthetic_report(failures=0, warnings=3)
    monkeypatch.setattr(selfcheck, "run_checks", lambda live=False: warn_only)

    assert cli.main(["self-check", "--blocking"]) == 0     # the deploy continues
    assert cli.main(["self-check"]) == 1                   # the plain report is honest
    printed = capsys.readouterr().out
    assert "no failures · 3 warning(s), advisory only" in printed
    assert "rolls a deploy back" in printed                # …and says so in words

    real_failure = _synthetic_report(failures=1, warnings=1)
    monkeypatch.setattr(selfcheck, "run_checks", lambda live=False: real_failure)

    assert cli.main(["self-check", "--blocking"]) == 1     # this one does stop it
    assert cli.main(["self-check"]) == 1
    assert "1 real failure(s) above" in capsys.readouterr().out


def test_a_deploy_only_looks_at_failures():
    """The script and the guide must run the form a deploy can trust."""
    script = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    guide = (REPO / "deploy" / "DEPLOY.md").read_text(encoding="utf-8")

    assert "self-check --blocking" in script
    assert "self-check --blocking" in guide
    assert "⚠️" in guide and "⛔" in guide   # both states are explained to the operator
