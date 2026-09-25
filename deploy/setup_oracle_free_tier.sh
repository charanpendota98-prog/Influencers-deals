#!/usr/bin/env bash
# =========================================================================
# 1-CLICK PRODUCTION SETUP SCRIPT FOR ORACLE CLOUD FREE TIER (1GB RAM)
# Configures 2GB Swap Memory, Firewall Ports, Node.js, Python, & Auto-Start Services
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

echo "== [3/6] Opening Firewall Ports (Port 5000 for Dashboard) =="
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 5000 -j ACCEPT || true
sudo netfilter-persistent save || true

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
