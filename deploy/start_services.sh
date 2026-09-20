#!/usr/bin/env bash
# Start the three long-running processes (dev / screen style).
# For production prefer the systemd units in this folder.
set -euo pipefail
cd "$(dirname "$0")/.."
. .venv/bin/activate
export PYTHONPATH=.

mkdir -p logs

echo "Starting wa_hub (Node)…"
( cd wa_hub && node server.js ) >> logs/wa_hub.log 2>&1 &

echo "Starting dashboard (Flask)…"
( cd dashboard && python app.py ) >> logs/dashboard.log 2>&1 &

echo "Starting VM watch…"
python -m influencer_hub.cli vm-watch --loop >> logs/vm_watch.log 2>&1 &

echo "All three started. Logs in ./logs/"
