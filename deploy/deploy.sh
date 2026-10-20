#!/usr/bin/env bash
# Deploy (or update) the influencer hub on this box — safely, and with a way back.
#
#   ./deploy/deploy.sh --check                 # read-only: what would happen, and what is missing
#   ./deploy/deploy.sh                         # update to the branch this checkout is on
#   ./deploy/deploy.sh --branch <name>         # update to another branch
#   ./deploy/deploy.sh --ref <sha>             # pin exactly one commit
#   ./deploy/deploy.sh --dry-run               # print every command, change nothing
#   ./deploy/deploy.sh --no-restart            # update code + database, leave services running
#   ./deploy/deploy.sh --from-backup <file>    # rollback: put a backup back (asks nothing)
#
# What it does, in order:
#   1. preflight  — refuses to start when a required check fails (branch, deps, .env
#                   keys, database, disk). Nothing is written before this passes.
#   2. backup     — a consistent copy of the live database (safe while the worker runs).
#   3. fetch + fast-forward only — never rewrites history, never discards a local edit.
#   4. dependencies — pip / npm only when the lock files changed. Never `npm audit fix`:
#                   the verified lock in wa_hub/package-lock.json is left exactly as it is.
#   5. migrate    — `cli init` adds the new columns; safe to run repeatedly.
#   6. restart    — systemd units when installed, otherwise deploy/start_services.sh.
#   7. health     — /healthz, the hub self-check and the fleet tag-proof.
#   8. rollback   — if any health check fails: previous commit + the new backup + restart.
#
# No secret is ever printed: the preflight reports which .env keys exist, never values.
set -euo pipefail

# Execute a stable copy: a fast-forward must not replace a running shell's input.
if [[ "${HUB_DEPLOY_SNAPSHOT:-}" != "$0" ]]; then
  export HUB_DEPLOY_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
  snapshot="$(mktemp "${TMPDIR:-/tmp}/hub-deploy.XXXXXXXX.sh")"
  cp -- "$0" "$snapshot"
  export HUB_DEPLOY_SNAPSHOT="$snapshot"
  exec bash "$snapshot" "$@"
fi
trap 'rm -f -- "$HUB_DEPLOY_SNAPSHOT"' EXIT
REPO_ROOT="$HUB_DEPLOY_ROOT"
cd "$REPO_ROOT"

BRANCH=""
REF=""
CHECK_ONLY=0
DRY_RUN=0
NO_RESTART=0
FROM_BACKUP=""
BACKUP_DIR="${HUB_BACKUP_DIR:-$REPO_ROOT/backups}"
HEALTH_URL="${HUB_HEALTH_URL:-http://127.0.0.1:5000/healthz}"
PY=".venv/bin/python"
PREVIOUS_SHA=""
NEW_BACKUP=""
STARTED_AT="$(date -Is)"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '[%s] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) CHECK_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
    --ref) REF="${2:?--ref needs a commit}"; shift ;;
    --from-backup) FROM_BACKUP="${2:?--from-backup needs a file}"; shift ;;
    -h|--help) usage ;;
    *) fail "unknown option: $1"; usage ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
run() {
  # Run a command, or only print it under --dry-run. Never used for secrets.
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  (dry-run) %s\n' "$*"
    return 0
  fi
  "$@"
}

run_sh() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  (dry-run) %s\n' "$*"
    return 0
  fi
  bash -lc "$*"
}

versioned() {
  # True when $1 is newer than the stamp $2 (a dependency file changed).
  [[ -f "$1" ]] || return 1
  [[ -f "$2" ]] || return 0
  [[ "$1" -nt "$2" ]]
}

systemd_units_installed() {
  [[ -d /run/systemd/system ]] || return 1
  systemctl list-unit-files 'influencer-*.service' --no-legend 2>/dev/null \
    | grep -q '^influencer-dashboard.service'
}

restart_services() {
  if systemd_units_installed; then
    log "restarting systemd units (wa-hub -> worker -> dashboard -> vm-watch)"
    run sudo systemctl restart influencer-wa-hub.service
    run sudo systemctl restart influencer-deal-worker.service
    run sudo systemctl restart influencer-dashboard.service
    run sudo systemctl restart influencer-vm-watch.service
  else
    log "restarting the supervised service set (deploy/start_services.sh)"
    run ./deploy/stop_services.sh
    run ./deploy/start_services.sh
  fi
}

wait_for_health() {
  local tries="${1:-20}"
  local i=1
  while [[ "$i" -le "$tries" ]]; do
    if curl -fsS --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
      log "dashboard answered on $HEALTH_URL"
      return 0
    fi
    sleep 3
    i=$((i + 1))
  done
  return 1
}

rollback() {
  local reason="$1"
  fail "$reason"
  if [[ -z "$PREVIOUS_SHA" ]]; then
    fail "no previous commit recorded; fix forward by hand (git log, cli self-check)"
    return 1
  fi
  log "ROLLBACK: returning to $PREVIOUS_SHA and the backup taken before this deploy"
  run git reset --hard "$PREVIOUS_SHA"
  if [[ -n "$NEW_BACKUP" && -f "$NEW_BACKUP" ]]; then
    run "$PY" -m influencer_hub.cli restore "$NEW_BACKUP" --yes
  fi
  if [[ "$NO_RESTART" != "1" ]]; then
    restart_services || true
    wait_for_health 10 || fail "the rolled-back hub still is not answering; check journalctl -u influencer-dashboard"
  fi
  log "rollback finished; the hub is as it was before this deploy"
  return 1
}

# ---------------------------------------------------------------------------
# 0. rollback-only mode
# ---------------------------------------------------------------------------
if [[ -n "$FROM_BACKUP" ]]; then
  log "restoring the database from $FROM_BACKUP"
  run "$PY" -m influencer_hub.cli restore "$FROM_BACKUP" --dry-run
  run "$PY" -m influencer_hub.cli restore "$FROM_BACKUP" --yes
  if [[ "$NO_RESTART" != "1" ]]; then
    restart_services
    wait_for_health 20 || fail "the dashboard did not answer after the restore"
  fi
  log "restore done. Old code is untouched; use --ref <sha> if you also need older code."
  exit 0
fi

# ---------------------------------------------------------------------------
# 1. preflight
# ---------------------------------------------------------------------------
log "preflight (read-only)"
# The services read .env through systemd; a shell invocation has to export it so
# the preflight inspects the same database and the same credentials the hub uses.
if [[ -f .env && "${HUB_SKIP_DOTENV:-}" != "1" ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
if [[ ! -x "$PY" ]]; then
  fail "no virtualenv at $REPO_ROOT/.venv — run ./deploy/install.sh first"
  exit 2
fi
if [[ -z "$BRANCH" ]]; then
  BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
fi
PREVIOUS_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
log "branch: $BRANCH · current commit: ${PREVIOUS_SHA:0:12}${REF:+ → $REF}"

set +e
"$PY" -m influencer_hub.cli deploy-check --branch "$BRANCH" --backups "$BACKUP_DIR"
PREFLIGHT_RC=$?
set -e
if [[ "$PREFLIGHT_RC" != "0" ]]; then
  if [[ "$CHECK_ONLY" == "1" ]]; then
    fail "not ready to deploy: fix the [XX] items above and run --check again."
    exit 1
  fi
  fail "preflight found a required problem (see above). Fix it, then deploy again."
  exit 1
fi
if [[ "$CHECK_ONLY" == "1" ]]; then
  log "--check only: nothing was changed."
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. backup
# ---------------------------------------------------------------------------
log "backing up the live database to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR" 2>/dev/null || true
if [[ "$DRY_RUN" == "1" ]]; then
  printf '  (dry-run) %s\n' "$PY -m influencer_hub.cli backup --out $BACKUP_DIR --keep 7"
else
  BACKUP_JSON="$("$PY" -m influencer_hub.cli backup --out "$BACKUP_DIR" --keep 7 --json)" || {
    fail "the backup failed; refusing to deploy without one"; exit 1; }
  NEW_BACKUP="$(printf '%s' "$BACKUP_JSON" | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("path",""))' 2>/dev/null || true)"
  log "backup: ${NEW_BACKUP:-created in $BACKUP_DIR}"
fi

# ---------------------------------------------------------------------------
# 3. fetch + fast-forward
# ---------------------------------------------------------------------------
log "fetching origin/$BRANCH"
run git fetch --prune origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
TARGET="${REF:-origin/$BRANCH}"
if ! run git merge --ff-only "$TARGET"; then
  rollback "cannot fast-forward to $TARGET: local commits or edits would be lost"
  exit 1
fi
if [[ "$DRY_RUN" != "1" ]]; then
  log "now at $(git rev-parse --short HEAD): $(git log -1 --pretty=%s)"
fi

# ---------------------------------------------------------------------------
# 4. dependencies (only when the locks changed)
# ---------------------------------------------------------------------------
if versioned requirements.txt .venv/.deploy-requirements; then
  log "requirements.txt changed → installing Python dependencies"
  run "$PY" -m pip install -r requirements.txt
  [[ "$DRY_RUN" == "1" ]] || touch .venv/.deploy-requirements
else
  log "Python dependencies unchanged"
fi
if versioned wa_hub/package-lock.json wa_hub/node_modules/.deploy-lock; then
  log "package-lock.json changed → npm ci (the verified lock is used as-is)"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  (dry-run) cd wa_hub && npm ci --no-audit --no-fund\n'
  else
    (cd wa_hub && npm ci --no-audit --no-fund) && touch wa_hub/node_modules/.deploy-lock
  fi
else
  log "WhatsApp dependencies unchanged"
fi

# ---------------------------------------------------------------------------
# 5. migrate
# ---------------------------------------------------------------------------
log "applying database migrations (safe to re-run)"
run "$PY" -m influencer_hub.cli init

# ---------------------------------------------------------------------------
# 6. restart
# ---------------------------------------------------------------------------
if [[ "$NO_RESTART" == "1" ]]; then
  log "--no-restart: leaving the running services untouched (new code starts on the next restart)"
else
  restart_services
fi

# ---------------------------------------------------------------------------
# 7. health
# ---------------------------------------------------------------------------
if [[ "$NO_RESTART" != "1" && "$DRY_RUN" != "1" ]]; then
  wait_for_health 20 || rollback "the dashboard never answered $HEALTH_URL"
fi
log "running the hub self-check"
# `--blocking` is deliberate: only a *real* failure (a check in the ⛔ state)
# rolls a deploy back. Warnings are how a healthy hub looks mid-setup — a worker
# that is still starting, a creator whose phone is not paired yet, a snapshot not
# taken yet — and rolling back over those would undo every deploy on a hub that
# has not finished onboarding. The warnings are printed, with their fixes.
if ! run "$PY" -m influencer_hub.cli self-check --blocking; then
  rollback "the hub self-check found a real failure after the deploy"
  exit 1
fi
log "checking Amazon tag safety (unsafe rendered links block; held creators are reported)"
# A creator with no own Associate tag is safely held by the posting pipeline.
# That onboarding gap must not roll back unrelated fixes for every other creator.
# A malformed/mis-signed rendered link still fails this gate and triggers rollback.
if ! run "$PY" -m influencer_hub.cli tag-proof --allow-held; then
  if [[ "$DRY_RUN" != "1" ]]; then
    rollback "tag-proof found a malformed or mis-signed rendered Amazon link"
    exit 1
  fi
fi

if [[ "$DRY_RUN" == "1" ]]; then
  cat <<EOF
[$(date -Is)] DRY RUN · nothing was changed
  would deploy: branch $BRANCH${REF:+ at $REF} · from commit $(git rev-parse --short HEAD 2>/dev/null || echo '?')
  every command it would run is printed above, in order
  run it for real with: ./deploy/deploy.sh${REF:+ --ref $REF}
EOF
  exit 0
fi

cat <<EOF
[$(date -Is)] DEPLOY OK · branch $BRANCH · commit $(git rev-parse --short HEAD 2>/dev/null || echo '?')
  started:    $STARTED_AT
  backup:     ${NEW_BACKUP:-$BACKUP_DIR}
  health:     $HEALTH_URL answered · self-check: no failures · tag-proof passed
  rollback:   ./deploy/deploy.sh --from-backup ${NEW_BACKUP:-<backup>}   (database only)
              git reset --hard ${PREVIOUS_SHA:0:12} && ./deploy/deploy.sh --no-restart  (code)
  dashboard:  http://<this-box>:5000  ·  logs: journalctl -u influencer-dashboard -f
EOF
