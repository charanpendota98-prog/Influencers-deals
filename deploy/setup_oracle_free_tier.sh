#!/usr/bin/env bash
# =========================================================================
# Base host bootstrap for Oracle Cloud Free Tier (1GB RAM).
# Installs dependencies; it does not configure a public TLS reverse proxy.
# =========================================================================
set -euo pipefail

echo "== [1/6] Setting up 2GB Swap Memory for 1GB Micro Instance =="
if [ ! -f /swapfile ]; then
    sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
    echo "Swap created successfully!"
else
    echo "Swapfile already exists."
fi

echo "== [2/6] Updating packages & installing Python3, Git, Node.js =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git curl ufw

# Install Node.js 20 LTS for WhatsApp Baileys Hub
if ! command -v node &> /dev/null; then
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

echo "== [3/6] Keeping the dashboard port private =="
echo "Port 5000 is intentionally not opened. Configure DNS, TLS, and a reverse proxy first."
echo "Allow public access only to the TLS proxy; restrict 5000 to localhost/private traffic."

echo "== [4/6] Installing Python Virtual Environment & Requirements =="
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "== [5/6] Installing WhatsApp Node Hub =="
cd wa_hub
npm install --no-audit --no-fund
cd ..

echo "== [6/6] Initializing Database =="
PYTHONPATH=. .venv/bin/python -m influencer_hub.cli init

echo "=========================================================="
echo "🎉 SETUP COMPLETE! Run ./deploy/start_services.sh to start"
echo "=========================================================="
