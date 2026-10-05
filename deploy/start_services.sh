#!/usr/bin/env bash
# Start and supervise the production service set from a cloned checkout.
# For systemd-managed installations, install the unit files in deploy/ instead.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run ./deploy/install.sh first." >&2
  exit 1
fi
# Node does not read .env itself. Export it so the WA hub and all child services
# receive the same deployment configuration. Keep .env private and shell-safe.
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
. .venv/bin/activate
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"

mkdir -p logs
.venv/bin/python -m influencer_hub.cli init

start_supervised() {
  local name="$1"
  local logfile="logs/${name}.log"
  local pidfile="logs/${name}.pid"

  if [[ -f "$pidfile" ]]; then
    local existing_pid
    existing_pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
      echo "$name already supervised (pid $existing_pid); leaving it running."
      return 0
    fi
    rm -f "$pidfile"
  fi

  echo "Starting supervised $name; log: $logfile"
  nohup bash -c '
    name="$1"; logfile="$2"; shift 2
    child=""
    stop_child() {
      trap - TERM INT
      if [[ -n "$child" ]]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
      fi
      exit 0
    }
    trap stop_child TERM INT
    while true; do
      printf "[%s] starting %s\n" "$(date -Is)" "$name"
      "$@" >> "$logfile" 2>&1 &
      child=$!
      wait "$child"
      rc=$?
      child=""
      printf "[%s] %s exited (%s); restarting in 5 seconds\n" "$(date -Is)" "$name" "$rc"
      sleep 5 &
      child=$!
      wait "$child" || true
      child=""
    done
  ' influencer-service-supervisor "$name" "$logfile" "$@" >/dev/null 2>&1 </dev/null &
  echo "$!" > "$pidfile"
}

# The worker is a first-class always-on service. It also catches transient
# errors internally; this wrapper restarts it if its process ever exits.
start_supervised "deal-worker" env HUB_TELEGRAM_SESSION_SLOT=worker .venv/bin/python -m influencer_hub.worker
start_supervised "wa-hub" env NODE_ENV=production node wa_hub/server.js
start_supervised "dashboard" env HUB_TELEGRAM_SESSION_SLOT=dashboard .venv/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile - dashboard.wsgi:app
start_supervised "vm-watch" .venv/bin/python -m influencer_hub.cli vm-watch --loop

echo "All services started under restart supervisors. Logs and pid files are in ./logs/."
echo "Use ./deploy/stop_services.sh to stop them."
