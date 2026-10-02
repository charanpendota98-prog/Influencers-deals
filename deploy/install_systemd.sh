#!/usr/bin/env bash
# Install systemd units for the current checkout and invoking non-root user.
# Run as: sudo ./deploy/install_systemd.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo: sudo ./deploy/install_systemd.sh" >&2
  exit 1
fi
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]] || ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "Could not determine the non-root service user; run via sudo from that user's shell." >&2
  exit 1
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

for unit in "$REPO_ROOT"/deploy/influencer-*.service; do
  [[ -f "$unit" ]] || continue
  destination="/etc/systemd/system/$(basename "$unit")"
  rendered="$(mktemp)"
  sed \
    -e "s|@REPO_ROOT@|$REPO_ROOT|g" \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
    "$unit" > "$rendered"
  install -o root -g root -m 0644 "$rendered" "$destination"
  rm -f "$rendered"
  echo "Installed $(basename "$unit") for $SERVICE_USER at $REPO_ROOT"
done

systemctl daemon-reload
cat <<'EOF'
Units are installed but not started. Before enabling them:
  1. Configure the private .env (including a strong dashboard password,
     persistent DASHBOARD_SECRET_KEY, and HUB_ENV=production).
  2. Back up and verify the database and existing service state.
  3. Configure HTTPS reverse proxy/DNS; the dashboard listens on 127.0.0.1:5000.
EOF
