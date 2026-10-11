"""Ops, part two: history, incident continuity, metrics, audit log, backups.

These are the parts that make a hub with a thousand creators *operable over
time*: it remembers yesterday, it keeps one story per outage instead of a ping
storm, it can be scraped by the VM's monitor, every attempt can be exported, and
the database can be copied and put back without guessing.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from influencer_hub import config, db, ops, readiness


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "advanced.sqlite3")
    db.init()
    return db


def _activity():
    inf_id = db.add_influencer("Ravi Loots", "ravi-21")
    main = db.add_channel(inf_id, "telegram", "@ravi_loots", status="ready", role="broadcast")
    wa = db.add_channel(inf_id, "whatsapp_channel", "120363012345678901@newsletter",
                        status="ready", role="whatsapp")
    for index in range(4):
        db.record_post(inf_id, main, f"deal-{index}", status="posted", deal_text=f"Deal {index}")
        db.record_post(inf_id, wa, f"deal-{index}", status="posted", deal_text=f"Deal {index}",
                       mirror_source_id=main)
    db.record_post(inf_id, main, "bad", status="failed", error="Telegram flood wait 120s")
    db.add_influencer("Second Creator", "second-21")
    return inf_id


def _seed_history(now: datetime) -> None:
    for offset, (posted, failed, verdict) in enumerate(
        [(1, 4, "fail"), (2, 2, "fail"), (4, 1, "attention"), (8, 0, "ready")]
    ):
        moment = now - timedelta(days=3 - offset)
        db.record_ops_snapshot(
            moment.strftime("%Y-%m-%d"), captured_at=moment.isoformat(timespec="seconds"),
            posted=posted, failed=failed, attempts=posted + failed,
            ready=offset, blocked=3 - offset, setup_percent=30 + offset * 10, verdict=verdict,
        )


# --------------------------------------------------------------------------
# 1. History and trends
# --------------------------------------------------------------------------
def test_the_history_stores_one_row_per_day_and_reads_back_as_a_trend(hub):
    now = datetime.now(timezone.utc)
    _seed_history(now)

    data = ops.trend(days=7)

    assert data["captured_days"] == 4
    assert [point["delivery_pct"] for point in data["points"]] == [20, 50, 80, 100]
    assert data["sparkline"] == ops.sparkline([20, 50, 80, 100])
    assert data["green_streak"] == 1                  # only the last day is ready
    assert data["delta_pct"] == 20                    # 100% today vs 80% yesterday


def test_writing_the_same_day_twice_refreshes_instead_of_duplicating(hub):
    now = datetime.now(timezone.utc)
    ops.record_snapshot(now=now)
    ops.record_snapshot(now=now)

    rows = db.list_ops_snapshots(days=7)

    assert len(rows) == 1 and rows[0]["day"] == now.strftime("%Y-%m-%d")


def test_the_sparkline_scales_and_survives_rubbish_input():
    assert ops.sparkline([]) == ""
    assert ops.sparkline([100, 100, 100]) == "▅▅▅"
    assert ops.sparkline([0, 50, 100]) == "▁▅█"      # 0 / 50 / 100 on an 8-block scale
    assert len(ops.sparkline([1, 2, 3, 4, 5], width=3)) == 3
    assert ops.sparkline([None, None]) == ""


def test_the_digest_names_the_biggest_blocker_and_the_top_creator(hub):
    _activity()
    blocked = db.add_influencer("Blocked One", "blocked-21")

    data = ops.digest(hours=24)

    assert data["creators"]["biggest_blocker"] == {"step": "main", "count": 2}
    assert data["creators"]["blocked_by_step"]["main"] == 2
    assert data["creators"]["top"][0] == {"name": "Ravi Loots", "posted": 8, "mirrored": 4}
    assert blocked  # the fixture really has two blocked creators, not one


# --------------------------------------------------------------------------
# 2. One story per outage
# --------------------------------------------------------------------------
def test_an_outage_is_one_incident_however_many_alerts_it_takes(hub, monkeypatch):
    monkeypatch.setattr(config, "OPS_TELEGRAM_CHANNEL", "@ops")
    sent: list[str] = []
    broken = {"verdict": "fail", "checks": [
        {"id": "worker", "label": "Worker", "state": "fail", "detail": "no heartbeat",
         "fix": "start it"}]}
    now = datetime.now(timezone.utc)

    first = ops.maybe_alert(broken, send=sent.append, now=now)
    second = ops.maybe_alert(broken, send=sent.append, now=now + timedelta(hours=4))
    third = ops.maybe_alert(broken, send=sent.append, now=now + timedelta(hours=8))

    assert first["message"].splitlines()[0].endswith("fail")     # no "open 0m" noise
    assert "#2" in second["message"] and "4h" in second["message"]
    assert "#3" in third["message"]
    assert db.get_global_setting(ops.INCIDENT_ALERTS_KEY) == "3"
    assert db.get_global_setting(ops.INCIDENT_VERDICT_KEY) == "fail"

    recovered = ops.maybe_alert({"verdict": "ready", "checks": []}, send=sent.append,
                                now=now + timedelta(days=1))
    assert "recovered" in recovered["message"].splitlines()[0]
    assert "1d" in recovered["message"]                     # the outage lasted a day
    assert db.get_global_setting(ops.INCIDENT_STARTED_KEY) == ""
    assert db.get_global_setting(ops.INCIDENT_ALERTS_KEY) == "0"
    assert len(sent) == 4


def test_durations_read_like_a_human_wrote_them():
    assert ops.humanize_minutes(None) == "unknown"
    assert ops.humanize_minutes(0) == "0m"
    assert ops.humanize_minutes(45) == "45m"
    assert ops.humanize_minutes(60) == "1h"
    assert ops.humanize_minutes(212) == "3h 32m"
    assert ops.humanize_minutes(60 * 30) == "1d 6h"


# --------------------------------------------------------------------------
# 3. Metrics and the audit log
# --------------------------------------------------------------------------
def test_the_metrics_endpoint_speaks_prometheus(hub):
    db.set_global_setting("earnkaro_api_key", "sk-do-not-leak")
    _activity()
    db.record_worker_heartbeat("running")
    ops.record_snapshot()

    text = ops.metrics_text()

    assert 'hub_posts_attempts{status="posted"} 8.0' in text
    assert "hub_worker_up 1.0" in text
    assert 'hub_creators{state="total"} 2.0' in text
    assert 'hub_selfcheck_state{verdict="fail"} 1' in text
    assert text.count("# TYPE") >= 10
    assert "sk-do-not-leak" not in text
    assert all(line.startswith(("#", "hub_")) for line in text.strip().splitlines())


def test_the_posts_log_is_an_exportable_audit_trail(hub):
    _activity()

    text = ops.posts_csv(hours=24)
    lines = text.strip().splitlines()

    assert lines[0] == ",".join(ops.POST_LOG_COLUMNS)
    assert len(lines) == 10                       # 8 posted + 1 failed + header
    assert "Telegram flood wait 120s" in text
    assert any(",@ravi_loots," in line for line in lines[1:])
    assert ",yes," in text                        # mirrored rows are marked as such


# --------------------------------------------------------------------------
# 4. Backup and restore
# --------------------------------------------------------------------------
def test_a_backup_is_consistent_and_carries_a_sidecar(hub, tmp_path):
    _activity()
    ops.record_snapshot()

    result = ops.backup_database(tmp_path / "backups", keep=3)

    target = Path(result["path"])
    assert target.exists() and target.name.startswith("hub-")
    assert result["size_bytes"] > 0
    assert result["counts"]["influencers"] == 2
    sidecar = json.loads(Path(result["sidecar"]).read_text())
    assert sidecar["counts"]["posts"] == 9
    assert "ops_snapshots" in sidecar["tables"]
    # The copy is a working database, not a partial file.
    con = sqlite3.connect(target)
    assert con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 9
    con.close()
    assert result["kept"] == [target.name]


def test_retention_keeps_the_newest_copies_and_never_the_current_one(hub, tmp_path):
    _activity()
    now = datetime.now(timezone.utc)
    out = tmp_path / "backups"
    for hours_ago in (30, 20, 10, 0):
        ops.backup_database(out, keep=2, now=now - timedelta(hours=hours_ago))

    files = sorted(path.name for path in out.glob("hub-*.sqlite3"))

    assert len(files) == 2
    assert (out / f"hub-{now.strftime('%Y%m%d-%H%M%S')}.sqlite3").exists()


def test_backup_refuses_when_there_is_no_database(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "gone.sqlite3")

    with pytest.raises(FileNotFoundError):
        ops.backup_database(tmp_path / "backups")


def test_a_restore_is_reversible_and_refuses_foreign_files(hub, tmp_path):
    _activity()
    backup = ops.backup_database(tmp_path / "backups")["path"]
    db.add_influencer("Added After The Backup", "later-21")
    assert len(db.list_influencers()) == 3

    junk = tmp_path / "not-a-hub.sqlite3"
    junk.write_text("this is not a database")
    assert ops.inspect_backup(junk)["ok"] is False
    with pytest.raises(ValueError):
        ops.restore_database(junk)

    result = ops.restore_database(backup)

    assert Path(result["previous_saved_as"]).exists()          # the old file is kept
    assert len(db.list_influencers()) == 2                     # ...and the backup is live
    assert db.get_global_setting  # the module still works against the restored file
    checked = ops.inspect_backup(backup)
    assert checked["ok"] is True and checked["counts"]["posts"] == 9


def test_the_cli_can_snapshot_back_up_and_restore(hub, tmp_path, capsys):
    from influencer_hub import cli

    _activity()
    assert cli.main(["snapshot", "--days", "3"]) == 0
    assert "attempts" in capsys.readouterr().out

    assert cli.main(["backup", "--out", str(tmp_path / "backups"), "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert "note: .env secrets are NOT in a backup" in out

    backup_file = next((tmp_path / "backups").glob("hub-*.sqlite3"))
    # Without --yes nothing is replaced (exit 2 = "asked, did nothing").
    assert cli.main(["restore", str(backup_file)]) == 2
    assert "run again with --yes" in capsys.readouterr().out
    assert cli.main(["restore", str(backup_file), "--yes"]) == 0
    assert "previous file kept as" in capsys.readouterr().out

    assert cli.main(["restore", str(tmp_path / "nope.sqlite3")]) == 1
    assert "not a hub database" in capsys.readouterr().out


def test_the_cli_digest_reports_history_when_it_exists(hub, capsys):
    from influencer_hub import cli

    _activity()
    ops.record_snapshot()

    assert cli.main(["digest", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trend"]["captured_days"] == 1
    assert payload["creators"]["top"][0]["name"] == "Ravi Loots"


# --------------------------------------------------------------------------
# 5. The dashboard surfaces
# --------------------------------------------------------------------------
def test_a_paged_plan_never_shrinks_the_fleet_wide_numbers(hub):
    """The dashboard renders 25 rows; the digest must still describe all 1000."""
    for index in range(30):
        creator = db.add_influencer(f"Creator {index:02d}", f"creator{index:02d}-21")
        db.add_channel(creator, "telegram", f"@creator{index:02d}", status="ready",
                       role="broadcast")
        db.add_channel(creator, "whatsapp_channel", f"120360000{index:02d}@newsletter",
                       status="ready", role="whatsapp")

    partial = readiness.overview(limit=5)

    assert partial["returned"] == 5 and partial["summary"]["creators"] == 30
    data = ops.digest(plan=partial)
    assert data["creators"]["total"] == 30
    assert sum(data["creators"]["blocked_by_step"].values()) == 30
    assert data["creators"]["biggest_blocker"] == {"step": "phone", "count": 30}


def test_the_dashboard_serves_metrics_the_audit_log_and_the_trend(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "http2.sqlite3")
    db.init()
    _activity()
    ops.record_snapshot()
    from dashboard.app import app

    app.config.update(TESTING=True)
    client = app.test_client()

    metrics = client.get("/api/metrics")
    assert metrics.status_code == 200
    assert metrics.mimetype == "text/plain"
    # One charset in the header, not two ("text/plain; charset=utf-8; charset=utf-8").
    assert metrics.headers["Content-Type"].count("charset") == 1
    assert b'hub_posts_attempts{status="posted"} 8.0' in metrics.get_data()

    posts = client.get("/api/posts.csv?hours=6")
    assert posts.status_code == 200
    assert posts.mimetype == "text/csv"
    assert posts.headers["Content-Type"].count("charset") == 1
    assert "posts-last-6h.csv" in posts.headers["Content-Disposition"]
    assert "Telegram flood wait 120s" in posts.get_data(as_text=True)

    digest = client.get("/api/digest").get_json()
    assert digest["trend"]["captured_days"] == 1
    assert digest["creators"]["biggest_blocker"]["step"] == "main"

    page = client.get("/").get_data(as_text=True)
    assert 'data-live-ops="trend"' in page and 'data-live-ops="blocker"' in page
    assert "Posts log (CSV)" in page
    assert "cli snapshot --days 30" in page
    assert "cli backup --out /var/backups/hub" in page
