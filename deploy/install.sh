#!/usr/bin/env bash
# Install all dependencies for the influencer hub on the VM.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Python venv =="
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== Node (wa_hub) =="
cd wa_hub
npm install --no-audit --no-fund
cd ..

echo "== Init DB =="
PYTHONPATH=. HUB_DB_PATH=influencer_hub/hub.sqlite3 \
  python -m influencer_hub.cli init

echo "DONE. Copy .env.example to .env and fill in secrets, then run start_services.sh"
