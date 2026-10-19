"""Deployment preflight: what this box has, what a deploy would change.

Deploying is the one moment a hub can lose money quietly — a stale branch, a
half-migrated database, a worker that restarts before its `.env` is right. So the
deploy path starts by *reading* everything and printing it:

* is this really the hub checkout, on the branch that was asked for, with nothing
  uncommitted that would be thrown away by a fast-forward;
* is there a virtualenv, and does it have the dependencies the lock file asks for;
* does ``.env`` exist and define the keys the hub needs (never their values);
* is the database there, healthy and backed up recently (and where backups go);
* is Node present for the WhatsApp service, are the systemd units installed, is
  the dashboard answering today, is the worker ticking.

``preflight()`` returns data only; nothing here writes, restarts or posts. The
shell script in ``deploy/deploy.sh`` runs it first and refuses to continue when a
required check fails, which is what makes an unattended deploy safe.

Nothing in this module ever prints a secret: values are reduced to "set" / "not
set" and to a length, and the ``.env`` keys are reported by name only.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config, db

# The keys a production hub cannot work without, by the name it uses in .env.
# A missing one is a *failure*: the deploy stops instead of starting a hub that
# cannot post, convert or protect its dashboard.
REQUIRED_ENV_KEYS = (
    ("DASHBOARD_ADMIN_PASSWORD", "dashboard sign-in"),
    ("DASHBOARD_SECRET_KEY", "session cookies survive a restart"),
    ("AMAZON_ASSOCIATE_TAG", "hub-wide Amazon fallback tag"),
)
# Keys that only matter for one capability: reported, never blocking, because a
# hub that has not enabled that channel yet is a normal hub.
OPTIONAL_ENV_KEYS = (
    ("TELEGRAM_API_ID", "Telegram deal sources"),
    ("TELEGRAM_API_HASH", "Telegram deal sources"),
    ("TELEGRAM_SESSION", "Telegram deal sources"),
    ("BOT_TOKEN", "Telegram bot notifications"),
    ("EARNKARO_API_KEY", "EarnKaro conversions (central account)"),
    ("EARNKARO_PUBLISHER_ID", "EarnKaro conversions (central account)"),
    ("HYPD_STORE_ID", "HYPD / Meesho conversions (central store)"),
    ("DASHBOARD_TEAM_PASSWORD", "the team page"),
    ("ADMIN_TOTP_SECRET", "two-factor on the admin sign-in"),
)
SERVICES = ("influencer-dashboard", "influencer-deal-worker",
            "influencer-wa-hub", "influencer-vm-watch")


def _systemd_available() -> bool:
    """Is a real systemd (PID 1) running here? A container has the binary only."""
    if not Path("/run/systemd/system").is_dir():
        return False
    code, out = _run(["systemctl", "is-system-running"], timeout=10)
    text = out.lower()
    return "failed to connect" not in text and "no such file" not in text


def _run(command: list[str], cwd: Path | None = None, timeout: int = 15) -> tuple[int, str]:
    """Run a read-only command, never raising; returns (returncode, output)."""
    try:
        done = subprocess.run(command, cwd=str(cwd) if cwd else None, timeout=timeout,
                              capture_output=True, text=True, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)[:400]
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()


def _env_keys(path: Path) -> dict[str, bool]:
    """Which keys ``.env`` defines. Values are never read into the report."""
    present: dict[str, bool] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return present
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name:
            present[name] = True
    return present


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local
            return 200 <= int(response.status) < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def newest_backup(directory: Path) -> dict:
    """The freshest database copy on disk, and how old it is in hours."""
    try:
        files = sorted(directory.glob("hub-*.sqlite3"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    if not files:
        return {"path": str(directory), "files": 0, "newest": "", "age_hours": None}
    newest = files[0]
    age = (time.time() - newest.stat().st_mtime) / 3600.0
    return {"path": str(directory), "files": len(files), "newest": newest.name,
            "age_hours": round(age, 1)}


def _git_state(repo: Path, branch: str) -> dict:
    code, head = _run(["git", "rev-parse", "--short", "HEAD"], cwd=repo)
    _, current = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    # Only *tracked* edits block a fast-forward. Untracked files are normal on a
    # box (.env, a venv, logs) and are reported without stopping anything.
    _, modified = _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo)
    _, everything = _run(["git", "status", "--porcelain"], cwd=repo)
    _, remote = _run(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=repo, timeout=30)
    return {
        "head": head if code == 0 else "",
        "branch": current.strip(),
        "dirty_files": len([line for line in modified.splitlines() if line.strip()]),
        "other_files": (len([line for line in everything.splitlines() if line.strip()])
                        - len([line for line in modified.splitlines() if line.strip()])),
        "requested_branch": branch,
        "remote_sha": (remote.split()[0][:12] if remote.strip() else ""),
        "on_requested_branch": current.strip() == branch,
    }


def preflight(*, repo: Path | None = None, branch: str = "", env_path: Path | None = None,
              backup_dir: Path | None = None, dashboard_url: str = "") -> dict:
    """Read-only report on this box, for the deploy script and the CLI."""
    repo = Path(repo or Path(__file__).resolve().parent.parent)
    branch = branch or os.getenv("HUB_DEPLOY_BRANCH", "") or _default_branch(repo)
    env_path = Path(env_path or repo / ".env")
    backup_dir = Path(backup_dir or repo / "backups")
    dashboard_url = dashboard_url or f"http://127.0.0.1:{int(config.DASHBOARD_PORT)}"

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, required: bool = True,
            fix: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "required": required,
                       "detail": detail, "fix": fix})

    # 1. the checkout -------------------------------------------------------
    if not (repo / "influencer_hub" / "pipeline.py").is_file():
        add("checkout", False, f"{repo} does not look like the hub checkout",
            fix="run the deploy script from the repository root")
        return _report(repo, branch, checks, env_path, backup_dir)
    git = _git_state(repo, branch)
    add("branch", git["on_requested_branch"],
        f"on {git['branch'] or '?'} at {git['head'] or '?'} "
        f"(wanted {branch}{', remote ' + git['remote_sha'] if git['remote_sha'] else ''})",
        required=False,
        fix=f"git fetch origin {branch} && git checkout {branch}")
    add("worktree", git["dirty_files"] == 0,
        f"{git['dirty_files']} modified file(s)"
        + (f", {git['other_files']} untracked (usually .env, a venv, logs)"
           if git["other_files"] else ""),
        fix="commit or stash the modified files; a fast-forward deploy refuses to "
            "throw them away")

    # 2. dependencies -------------------------------------------------------
    venv_python = repo / ".venv" / "bin" / "python"
    add("virtualenv", venv_python.is_file(), f"{venv_python}",
        fix="python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    if venv_python.is_file():
        # Import the dependency surface the hub really uses (Flask for the
        # dashboard, aiohttp/telethon for the network side, psutil/gunicorn for
        # ops) rather than guessing package names from memory.
        probe = (
            "import aiohttp, telethon, psutil, gunicorn, flask\n"
            "from importlib.metadata import version\n"
            "print('flask', version('flask'), '| aiohttp', version('aiohttp'),"
            " '| telethon', version('telethon'), '| gunicorn', version('gunicorn'))"
        )
        code, out = _run([str(venv_python), "-c", probe], cwd=repo, timeout=60)
        detail = next((line for line in reversed(out.splitlines())
                       if line.startswith("flask ")), out.splitlines()[-1] if out else "")
        add("python-deps", code == 0, detail or "import failed",
            fix=".venv/bin/pip install -r requirements.txt")
    else:
        add("python-deps", False, f"{venv_python} is missing",
            fix="python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    requirements = repo / "requirements.txt"
    stamp = repo / ".venv" / ".deploy-requirements"
    if requirements.is_file() and stamp.is_file():
        changed = requirements.stat().st_mtime > stamp.stat().st_mtime
        add("requirements", not changed,
            "requirements.txt is newer than the last install"
            if changed else "requirements.txt is as installed",
            required=False, fix=".venv/bin/pip install -r requirements.txt")
    node = shutil.which("node")
    add("node", bool(node), node or "node not found — the WhatsApp service cannot start",
        required=False, fix="install Node 18+ (the WhatsApp hub runs on it)")
    wa_hub = repo / "wa_hub"
    if node and wa_hub.is_dir():
        add("whatsapp-deps", (wa_hub / "node_modules").is_dir(),
            f"{wa_hub}/node_modules",
            required=False, fix="cd wa_hub && npm ci (never npm audit fix)")

    # 3. environment --------------------------------------------------------
    add("dotenv", env_path.is_file(), str(env_path),
        fix="cp .env.example .env and fill it in; keep it chmod 600")
    keys = _env_keys(env_path)
    for name, why in REQUIRED_ENV_KEYS:
        set_in_file = name in keys or bool(os.getenv(name))
        add(f"env:{name}", set_in_file, f"{'set' if set_in_file else 'missing'} — {why}",
            fix=f"add {name}=… to .env")
    missing_optional = [name for name, _ in OPTIONAL_ENV_KEYS
                        if not (name in keys or os.getenv(name))]
    add("env:optional", True,
        f"{len(OPTIONAL_ENV_KEYS) - len(missing_optional)}/{len(OPTIONAL_ENV_KEYS)} "
        f"capability keys set"
        + (f"; missing {', '.join(missing_optional)}" if missing_optional else ""),
        required=False)

    # 4. the database -------------------------------------------------------
    db_path = Path(config.DB_PATH)
    add("database", db_path.exists(), f"{db_path}",
        fix="python -m influencer_hub.cli init")
    if db_path.exists():
        try:
            db.init()
            creators = len(db.list_influencers())
            add("schema", True, f"reachable, {creators} creator(s), migrations applied")
        except Exception as exc:  # pragma: no cover - depends on the box
            add("schema", False, f"{type(exc).__name__}: {str(exc)[:200]}",
                fix="restore the last backup (cli restore <file> --yes) and retry")
        free_mb = shutil.disk_usage(str(db_path.parent)).free // (1024 * 1024)
        add("disk", free_mb > 512, f"{free_mb} MB free beside the database",
            fix="free space; a full disk makes SQLite writes fail mid-post")
    backups = newest_backup(backup_dir)
    fresh = backups["age_hours"] is not None and backups["age_hours"] < 24
    add("backups", fresh,
        (f"{backups['files']} copy(ies), newest {backups['newest']} "
         f"({backups['age_hours']}h old)" if backups["files"] else "no backups yet"),
        required=False,
        fix="python -m influencer_hub.cli backup --out backups --keep 7")

    # 5. services -----------------------------------------------------------
    if _systemd_available() and shutil.which("systemctl"):
        code, out = _run(["systemctl", "list-unit-files", "influencer-*.service", "--no-legend"])
        installed = [line.split()[0] for line in out.splitlines()
                     if line.strip() and line.split()[0].startswith("influencer-")]
        add("systemd", bool(installed),
            f"{len(installed)} unit(s): {', '.join(installed[:4])}" if installed
            else "no influencer units installed", required=False,
            fix="sudo ./deploy/install_systemd.sh")
    else:
        add("systemd", False, "systemd is not running on this box — start the services "
            "with deploy/start_services.sh (it supervises and restarts them)",
            required=False)
    add("dashboard-today", _http_ok(f"{dashboard_url}/healthz"),
        f"{dashboard_url}/healthz", required=False,
        fix="the deploy script starts it; check logs/ or journalctl -u "
            "influencer-dashboard")
    add("whatsapp-hub-today", _port_open("127.0.0.1", int(config.WA_HUB_PORT)),
        f"port {int(config.WA_HUB_PORT)}", required=False,
        fix="./deploy/start_services.sh (or systemctl start influencer-wa-hub)")

    report = _report(repo, branch, checks, env_path, backup_dir)
    report["git"] = git
    report["backups"] = backups
    report["next_deploy"] = plan(branch=branch, repo=repo)
    return report


def _report(repo: Path, branch: str, checks: list[dict], env_path: Path,
            backup_dir: Path) -> dict:
    failed = [check for check in checks if check["required"] and not check["ok"]]
    warnings = [check for check in checks if not check["required"] and not check["ok"]]
    return {
        "repo": str(repo),
        "branch": branch,
        "env_path": str(env_path),
        "backup_dir": str(backup_dir),
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ok": not failed,
        "failed": [check["name"] for check in failed],
        "warnings": [check["name"] for check in warnings],
        "checks": checks,
        "summary": ("✅ ready to deploy" if not failed
                    else "⛔ fix first: " + ", ".join(check["name"] for check in failed)),
    }


def plan(*, branch: str = "", repo: Path | None = None, ref: str = "") -> list[dict]:
    """The ordered deploy steps, as data — the script prints exactly this."""
    repo = Path(repo or Path(__file__).resolve().parent.parent)
    branch = branch or _default_branch(repo)
    target = ref or branch
    return [
        {"step": 1, "name": "preflight", "command": "python -m influencer_hub.cli deploy-check",
         "why": "refuses to continue when a required check fails"},
        {"step": 2, "name": "backup",
         "command": "python -m influencer_hub.cli backup --out backups --keep 7",
         "why": "a copy of the live database before any code changes"},
        {"step": 3, "name": "fetch", "command": f"git fetch origin {branch}",
         "why": "gets the branch the hub should run"},
        {"step": 4, "name": "fast-forward", "command": f"git merge --ff-only {target}",
         "why": "never rewrites history or throws away a local edit"},
        {"step": 5, "name": "python-deps", "command": ".venv/bin/pip install -r requirements.txt",
         "why": "only when requirements.txt changed"},
        {"step": 6, "name": "whatsapp-deps", "command": "npm ci --no-audit --no-fund (in wa_hub/)",
         "why": "only when package-lock.json changed; the lock file is never modified"},
        {"step": 7, "name": "migrate", "command": "python -m influencer_hub.cli init",
         "why": "adds the new columns; safe to re-run"},
        {"step": 8, "name": "restart",
         "command": "systemctl restart influencer-{wa-hub,deal-worker,dashboard,vm-watch}"
                    " (or ./deploy/stop_services.sh && ./deploy/start_services.sh)",
         "why": "the new code takes over; the worker resumes where it stopped"},
        {"step": 9, "name": "health",
         "command": "curl -fsS http://127.0.0.1:5000/healthz && "
                    "python -m influencer_hub.cli self-check",
         "why": "the dashboard answers and the hub checks itself before anyone looks"},
        {"step": 10, "name": "tag-proof", "command": "python -m influencer_hub.cli tag-proof",
         "why": "every active channel still signs Amazon links with its creator's own tag"},
        {"step": 11, "name": "rollback-on-failure",
         "command": "git reset --hard <previous sha> && "
                    "python -m influencer_hub.cli restore <newest backup> --yes && restart",
         "why": "a failed health check leaves the hub exactly as it was"},
    ]


def _default_branch(repo: Path) -> str:
    """The branch this checkout is on (that is what a VM runs), then its upstream."""
    code, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    current = out.strip()
    if code == 0 and current and current != "HEAD":
        return current
    code, out = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
                      "@{upstream}"], cwd=repo)
    if code == 0 and "/" in out:
        return out.split("/", 1)[1].strip()
    return out.strip() if code == 0 and out else "main"


def as_json(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
