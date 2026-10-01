#!/usr/bin/env bash
# Gracefully stop processes started by deploy/start_services.sh.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

for name in deal-worker wa-hub dashboard vm-watch; do
  pidfile="logs/${name}.pid"
  [[ -f "$pidfile" ]] || continue
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Stopping $name (pid $pid)…"
    kill -TERM "$pid" 2>/dev/null || true
    # The supervisor trap stops its child and exits. Bound the wait to avoid a
    # terminal hang if a broken child ignores SIGTERM.
    for _ in {1..10}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
done
