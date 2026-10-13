"""Deploying must be boring: preflight, backup, fast-forward, health, rollback.

These tests pin the promises the operator deploys on:

* the preflight is read-only and reports, by name, which ``.env`` keys exist —
  never a value, never a secret;
* a *modified tracked file* stops a deploy (a fast-forward would throw it away),
  while an untracked ``.env``/venv/log does not;
* the plan lists the steps in the order the script runs them, and every step that
  changes something sits after the backup;
* the shell script is valid bash, prints the whole plan under ``--dry-run``,
  changes nothing, and never contains ``npm audit fix``;
* the systemd units the installer ships cover the dashboard, the worker, the
  WhatsApp service and vm-watch.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess

import pytest

from influencer_hub import config, db, deploy

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "deploy.sh"


@pytest.fixture()
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "deploy.sqlite3")
    db.init()
    db.add_influencer("Deploy Creator", "deploy-21")
    return tmp_path


# --------------------------------------------------------------------------
# 1. the shell script
# --------------------------------------------------------------------------
def test_the_deploy_script_is_valid_bash():
    assert SCRIPT.is_file(), "deploy/deploy.sh must ship with the hub"
    assert os.access(SCRIPT, os.X_OK), "the operator has to be able to run it"
    done = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_deploy_script_never_rewrites_the_lock_or_forces_history():
    text = SCRIPT.read_text(encoding="utf-8")
    commands = [line for line in text.splitlines()
                if not line.strip().startswith("#")]     # comments may name the rule
    assert not [line for line in commands if "npm audit fix" in line], (
        "the verified dependency lock must never be rewritten by a deploy"
    )
    assert "npm audit fix" in text, "the script should say out loud that it never runs it"
    assert "npm ci --no-audit --no-fund" in text          # the safe form is used
    assert "merge --ff-only" in text                      # history is never rewritten
    # A hard reset is allowed *only* inside the rollback, where the SHA is printed.
    for number, line in enumerate(text.splitlines(), 1):
        if "reset --hard" in line:
            context = "\n".join(text.splitlines()[max(0, number - 12):number])
            assert "ROLLBACK" in context or "rollback" in context, (
                f"line {number}: reset --hard outside the rollback path"
            )


def test_the_plan_puts_the_backup_before_anything_that_changes_code(hub):
    steps = deploy.plan(repo=REPO, branch="main")
    names = [step["name"] for step in steps]
    assert names[0] == "preflight"
    assert names[1] == "backup"
    for changing in ("fast-forward", "python-deps", "whatsapp-deps", "migrate", "restart"):
        assert names.index(changing) > names.index("backup"), changing
    assert names[-1] == "rollback-on-failure"
    assert all(step["command"] and step["why"] for step in steps)


def test_the_dry_run_prints_the_plan_and_changes_nothing():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--dry-run" in text and "(dry-run)" in text
    assert "DRY RUN · nothing was changed" in text
    # Under --dry-run nothing is executed: every mutating call goes through run(),
    # and run() prints instead of executing when DRY_RUN=1.
    assert "if [[ \"$DRY_RUN\" == \"1\" ]]; then" in text


# --------------------------------------------------------------------------
# 2. the preflight
# --------------------------------------------------------------------------
def test_the_preflight_reports_without_changing_anything(hub):
    before = sorted(path.name for path in hub.iterdir())
    report = deploy.preflight(repo=REPO, branch="", env_path=hub / ".env",
                             backup_dir=hub / "backups")
    assert sorted(path.name for path in hub.iterdir()) == before, (
        "the preflight wrote to the box; it must be read-only"
    )
    names = [check["name"] for check in report["checks"]]
    for expected in ("virtualenv", "python-deps", "database", "disk", "worktree",
                     "env:DASHBOARD_ADMIN_PASSWORD", "env:DASHBOARD_SECRET_KEY",
                     "env:AMAZON_ASSOCIATE_TAG"):
        assert expected in names, f"{expected} missing from the preflight"
    assert report["summary"]
    assert isinstance(report["ok"], bool)
    assert report["next_deploy"], "the preflight must show what a deploy would do"


def test_the_preflight_never_prints_a_secret(hub):
    secret = "super-secret-dashboard-password-16chars"
    env_file = hub / ".env"
    env_file.write_text(
        "DASHBOARD_ADMIN_PASSWORD=" + secret + "\n"
        "DASHBOARD_SECRET_KEY=" + secret + "\n"
        "AMAZON_ASSOCIATE_TAG=deploy-21\n"
        "EARNKARO_API_KEY=" + secret + "\n",
        encoding="utf-8",
    )
    report = deploy.preflight(repo=REPO, branch="", env_path=env_file,
                              backup_dir=hub / "backups")
    text = deploy.as_json(report)
    assert secret not in text, "a deploy report must never carry a credential"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["env:DASHBOARD_ADMIN_PASSWORD"]["ok"] is True
    assert checks["dotenv"]["ok"] is True


def test_a_missing_env_file_is_a_required_failure(hub):
    report = deploy.preflight(repo=REPO, branch="", env_path=hub / "nothing.env",
                              backup_dir=hub / "backups")
    assert report["ok"] is False
    assert "dotenv" in report["failed"]
    assert "fix" in [key for check in report["checks"] for key in check]


def test_a_modified_tracked_file_blocks_a_deploy_but_an_untracked_one_does_not(
        monkeypatch, tmp_path):
    """A fast-forward would lose a modification; a stray file is normal on a box."""
    monkeypatch.setattr(deploy, "_git_state", lambda repo, branch: {
        "head": "abc1234", "branch": branch, "dirty_files": 2, "other_files": 3,
        "requested_branch": branch, "remote_sha": "abc1234", "on_requested_branch": True,
    })
    report = deploy.preflight(repo=REPO, branch="main", env_path=tmp_path / ".env",
                              backup_dir=tmp_path / "backups")
    assert "worktree" in report["failed"]

    monkeypatch.setattr(deploy, "_git_state", lambda repo, branch: {
        "head": "abc1234", "branch": branch, "dirty_files": 0, "other_files": 5,
        "requested_branch": branch, "remote_sha": "abc1234", "on_requested_branch": True,
    })
    report = deploy.preflight(repo=REPO, branch="main", env_path=tmp_path / ".env",
                              backup_dir=tmp_path / "backups")
    assert "worktree" not in report["failed"]
    detail = [check["detail"] for check in report["checks"]
              if check["name"] == "worktree"][0]
    assert "0 modified file(s), 5 untracked" in detail


def test_backups_are_reported_by_age(hub, monkeypatch):
    backup_dir = hub / "backups"
    backup_dir.mkdir()
    newest = backup_dir / "hub-20261014-091102.sqlite3"
    newest.write_bytes(b"not a real database, only a file")
    monkeypatch.setattr(deploy.time, "time", lambda: newest.stat().st_mtime + 3600 * 3)
    report = deploy.newest_backup(backup_dir)
    assert report["files"] == 1
    assert report["newest"] == newest.name
    assert report["age_hours"] == 3.0


# --------------------------------------------------------------------------
# 3. the shipped units
# --------------------------------------------------------------------------
def test_the_systemd_units_cover_every_service():
    units = {path.name: path.read_text(encoding="utf-8")
             for path in (REPO / "deploy").glob("influencer-*.service")}
    for service in deploy.SERVICES:
        assert f"{service}.service" in units, f"{service}.service is missing"
    dashboard = units["influencer-dashboard.service"]
    assert "gunicorn" in dashboard and "dashboard.wsgi:app" in dashboard
    assert "EnvironmentFile=-@REPO_ROOT@/.env" in dashboard, "the .env must be loaded"
    worker = units["influencer-deal-worker.service"]
    assert "influencer_hub.worker" in worker
    wa = units["influencer-wa-hub.service"]
    assert "wa_hub" in wa and "node" in wa


def test_the_guide_documents_the_rollback_and_the_lock_rule():
    guide = (REPO / "deploy" / "DEPLOY.md").read_text(encoding="utf-8")
    assert "--check" in guide and "--dry-run" in guide
    assert "--from-backup" in guide
    assert "npm audit fix" in guide          # named as the thing that never runs
    assert "git reset --hard" in guide       # the code rollback, printed for the operator
    assert "deploy/deploy.sh" in guide


def test_the_cli_exposes_the_preflight(capsys, hub, monkeypatch):
    from influencer_hub import cli

    monkeypatch.setattr(config, "DB_PATH", hub / "deploy.sqlite3")
    args = cli.build_parser().parse_args(["deploy-check", "--json"])
    assert args.func is cli._cmd_deploy_check
    assert args.json is True and args.show_plan is False
    cli._cmd_deploy_check(args)
    report = json.loads(capsys.readouterr().out)
    assert "checks" in report and "next_deploy" in report
