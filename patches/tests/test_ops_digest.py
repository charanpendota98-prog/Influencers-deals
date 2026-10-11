"""Ops layer: the digest, the alert policy and the exports.

The alert policy is the part that can annoy a human, so it is pinned hard: it
speaks when the hub gets *worse*, when it is still broken after the cooldown, and
once when it recovers — and stays silent otherwise. The digest must count
failures (not just successes) in its window, and the exports must be usable by a
spreadsheet without leaking a credential.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from influencer_hub import config, db, ops, readiness


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "ops.sqlite3")
    db.init()
    return db


def _creator_with_activity(*, mirror_ready=True):
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    main_id = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready",
                             role="broadcast")
    wa_id = db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                           status="ready" if mirror_ready else "pending", role="whatsapp")
    for index in range(3):
        sig = f"deal-{index}"
        db.record_post(inf_id, main_id, sig, status="posted", deal_text=f"Deal {index}")
        if mirror_ready:
            db.record_post(inf_id, wa_id, sig, status="posted", deal_text=f"Deal {index}",
                           mirror_source_id=main_id)
    db.record_post(inf_id, main_id, "deal-bad", status="failed",
                   error="Telegram flood wait 42s")
    db.record_post(inf_id, main_id, "deal-skip", status="skipped", error="no earning link")
    return inf_id


# --------------------------------------------------------------------------
# 1. Digest
# --------------------------------------------------------------------------
def test_the_digest_counts_successes_failures_and_mirrors_in_the_window(hub):
    _creator_with_activity()
    db.add_source("Power Loot", "@powerloot")
    db.record_worker_heartbeat("running")
    db.defer_deal("Meesho raw link", source="Power Loot", reason="awaiting conversion")

    data = ops.digest(hours=24)

    assert data["posts"]["posted"] == 6          # 3 main + 3 mirrored
    assert data["posts"]["mirrored"] == 3
    # Failed and skipped attempts carry no posted_at; the window uses created_at,
    # so a bad hour cannot look like a quiet hour.
    assert data["posts"]["failed"] == 1
    assert data["posts"]["skipped"] == 1
    assert data["posts"]["attempts"] == 8
    assert data["posts"]["top_failures"][0]["reason"].startswith("Telegram flood wait")
    assert data["retry_queue"] == 1
    assert data["worker"]["state"] == "running"
    assert data["sources"]["active"] == 1
    assert data["creators"]["total"] == 1
    assert data["creators"]["mirroring"] == 1


def test_an_old_post_falls_outside_the_window(hub):
    inf_id = _creator_with_activity()
    con = db._connect()
    old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    con.execute("UPDATE posts SET posted_at=?, created_at=?", (old, old))
    con.commit()
    con.close()

    assert ops.digest(hours=24)["posts"]["posted"] == 0
    assert ops.digest(hours=48)["posts"]["posted"] == 6


def test_the_card_is_short_plain_text_with_no_secret(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "@ops")
    db.set_global_setting("earnkaro_api_key", "super-secret-key")
    _creator_with_activity()

    text = ops.render_digest(ops.digest(hours=24))

    assert text.startswith(("🟢", "🟡", "🔴", "⚪"))
    assert "Posts:" in text and "Self-check:" in text
    assert "super-secret-key" not in text
    assert len(text.splitlines()) <= 8


# --------------------------------------------------------------------------
# 2. Alert policy
# --------------------------------------------------------------------------
def _report(verdict, *checks):
    return {"verdict": verdict, "checks": list(checks)}


def test_alert_policy_speaks_when_it_gets_worse_and_when_it_recovers(hub):
    now = datetime.now(timezone.utc)

    first = ops.alert_decision(_report("fail"), now=now)
    assert first["alert"] and first["reason"] == "got_worse"

    same = ops.alert_decision(_report("fail"), last_verdict="fail",
                              last_alert_at=now.isoformat(), now=now)
    assert not same["alert"] and same["reason"] == "cooldown"
    assert same["cooldown_remaining_minutes"] > 0

    worse = ops.alert_decision(_report("fail"), last_verdict="attention",
                               last_alert_at=now.isoformat(), now=now)
    assert worse["alert"] and worse["reason"] == "got_worse"

    later = ops.alert_decision(_report("attention"), last_verdict="attention",
                               last_alert_at=(now - timedelta(hours=5)).isoformat(),
                               now=now)
    assert later["alert"] and later["reason"] == "still_broken"

    fixed = ops.alert_decision(_report("ready"), last_verdict="fail", now=now)
    assert fixed["alert"] and fixed["reason"] == "recovered"

    quiet = ops.alert_decision(_report("ready"), last_verdict="ready", now=now)
    assert not quiet["alert"] and quiet["reason"] == "all_green"


def test_an_alert_names_the_problem_and_the_fix(hub):
    report = _report(
        "fail",
        {"id": "worker", "label": "Background worker", "state": "fail",
         "detail": "The worker has never reported a heartbeat.",
         "fix": "sudo systemctl start influencer-deal-worker"},
        {"id": "sources", "label": "Deal sources", "state": "ok", "detail": "1 active", "fix": ""},
    )

    text = ops.render_alert(report, ops.alert_decision(report))

    assert "got worse" in text
    assert "Background worker" in text and "systemctl start" in text
    assert "Deal sources" not in text          # green checks are not noise


def test_maybe_alert_sends_once_then_respects_the_cooldown(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "@ops")
    sent: list[str] = []
    report = _report("fail", {"id": "worker", "label": "Worker", "state": "fail",
                              "detail": "never started", "fix": "start it"})

    first = ops.maybe_alert(report, send=sent.append)
    second = ops.maybe_alert(report, send=sent.append)

    assert first["sent"] is True and len(sent) == 1
    assert second["sent"] is False and second["decision"]["reason"] == "cooldown"
    assert db.get_global_setting(ops.LAST_VERDICT_KEY) == "fail"
    assert db.get_global_setting(ops.LAST_ALERT_KEY)      # recorded, so a restart is quiet

    recovered = ops.maybe_alert(_report("ready"), send=sent.append)
    assert recovered["sent"] is True and len(sent) == 2
    assert db.get_global_setting(ops.LAST_VERDICT_KEY) == "ready"


def test_no_ops_channel_means_no_message_and_no_state_change(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "")

    result = ops.maybe_alert(_report("fail"))

    assert result["sent"] is False
    assert result["ops_channel_configured"] is False
    assert "OPS_TELEGRAM_CHANNEL is not configured" in result["error"]
    # Nothing was recorded, so the first configured run still alerts.
    assert db.get_global_setting(ops.LAST_VERDICT_KEY, "") == ""


def test_force_is_for_runbooks_not_for_spam(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "@ops")
    sent: list[str] = []
    report = _report("attention", {"id": "creators", "label": "Creator readiness",
                                   "state": "warn", "detail": "3 blocked", "fix": "pair phones"})

    quiet = ops.maybe_alert(_report("ready"), send=sent.append)
    assert quiet["sent"] is False                       # recovered from nothing

    assert ops.maybe_alert(report, send=sent.append, force=True)["sent"] is True
    assert len(sent) == 1


# --------------------------------------------------------------------------
# 3. Exports
# --------------------------------------------------------------------------
def test_the_readiness_csv_has_one_row_per_creator_and_no_secrets(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "@ops")
    db.set_global_setting("earnkaro_api_key", "top-secret-token")
    inf_id = _creator_with_activity(mirror_ready=False)
    db.add_influencer("Second Creator", "second-21")

    plan = readiness.overview()
    text = ops.readiness_csv(plan)
    lines = text.strip().splitlines()

    assert lines[0].split(",")[:6] == [
        "creator_id", "creator", "active", "ready", "amazon_tag", "tag_source"
    ]
    assert len(lines) == 1 + plan["summary"]["creators"]
    assert "top-secret-token" not in text
    # The blocked creator row names the next step, so the CSV is actionable.
    blocked = next(line for line in lines if "Ravi Loots" in line)
    assert "phone" in blocked and "120363012345678901@newsletter" in blocked


def test_the_bulk_template_round_trips_through_the_parser(hub):
    import dashboard.app as dashboard_module

    text = ops.bulk_template_csv()
    rows = dashboard_module.parse_bulk_creators(text)

    assert [row["name"] for row in rows] == ["Ravi Loots", "Sita Deals", "Kiran Deals"]
    assert rows[0]["tag"] == "ravi-21"
    assert rows[0]["whatsapp"] == "120363012345678901@newsletter"


# --------------------------------------------------------------------------
# 4. CLI + HTTP surfaces
# --------------------------------------------------------------------------
def test_the_cli_self_check_returns_a_status_code_and_json(hub, monkeypatch, capsys):
    from influencer_hub import cli

    assert cli.main(["self-check", "--json"]) == 1        # not green yet
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["verdict"] in {"fail", "attention"}
    assert payload["checks"]

    assert cli.main(["digest", "--hours", "6", "--json"]) == 0
    digest = __import__("json").loads(capsys.readouterr().out)
    assert digest["hours"] == 6 and digest["posts"]["posted"] == 0


def test_the_http_surfaces_serve_the_same_data(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "http.sqlite3")
    db.init()
    _creator_with_activity()
    import dashboard.app as dashboard_module
    from dashboard.app import app

    app.config.update(TESTING=True)
    client = app.test_client()

    digest = client.get("/api/digest?hours=24").get_json()
    assert digest["ok"] is True and digest["posts"]["posted"] == 6
    assert "Hub digest" in digest["card"]

    csv_response = client.get("/api/readiness.csv")
    assert csv_response.status_code == 200
    assert csv_response.mimetype == "text/csv"
    assert "attachment; filename=creator-readiness.csv" in csv_response.headers["Content-Disposition"]
    body = csv_response.get_data(as_text=True)
    assert "Ravi Loots" in body

    # The same filters as /api/readiness apply, so a spreadsheet can be produced
    # per state: this creator is blocked (no phone linked), not ready.
    blocked = client.get("/api/readiness.csv?state=blocked").get_data(as_text=True)
    assert "Ravi Loots" in blocked
    ready_only = client.get("/api/readiness.csv?state=ready").get_data(as_text=True)
    assert "Ravi Loots" not in ready_only
    assert len(ready_only.strip().splitlines()) == 1     # header only

    template = client.get("/api/easy-setup/template.csv").get_data(as_text=True)
    assert template.startswith("name,amazon_tag,main_channel,approval_channel,whatsapp_id")

    page = client.get("/").get_data(as_text=True)
    assert 'id="ops-digest"' in page
    assert "Creator checklist (CSV)" in page and "Bulk template (CSV)" in page
    assert "cli self-check --live" in page
