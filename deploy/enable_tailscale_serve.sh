#!/usr/bin/env bash
# Expose the loopback-only production dashboard over private Tailscale HTTPS.
# This script does NOT install Tailscale, change cloud firewall rules, or enable Funnel.
set -euo pipefail

APP_HOST="127.0.0.1"
APP_PORT="5000"
WA_HUB_PORT="8088"

if [[ "${EUID}" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

command -v tailscale >/dev/null 2>&1 || fail "Tailscale is not installed. Install it from Tailscale's official Ubuntu instructions first."
command -v ss >/dev/null 2>&1 || fail "The 'ss' utility is missing; install the iproute2 package."
command -v curl >/dev/null 2>&1 || fail "curl is required for the local dashboard health check."

"${SUDO[@]}" tailscale status >/dev/null 2>&1 || fail "Tailscale is not authenticated/connected. Run 'sudo tailscale up' and finish sign-in first."

funnel_status="$("${SUDO[@]}" tailscale funnel status 2>/dev/null || true)"
if printf '%s\n' "$funnel_status" | grep -Eiq 'Funnel on|Funnel is enabled'; then
  fail "Tailscale Funnel appears enabled. Turn it off before private-only setup; Funnel is public."
fi

listeners="$("${SUDO[@]}" ss -ltnH "sport = :${APP_PORT}" 2>/dev/null || true)"
[[ -n "$listeners" ]] || fail "Nothing is listening on port ${APP_PORT}; start the production dashboard first."
printf '%s\n' "$listeners" | grep -Eq '127\.0\.0\.1:5000|\[::1\]:5000' || fail "Dashboard is not bound to loopback. Fix the service bind; do not expose port 5000."
printf '%s\n' "$listeners" | grep -Eq '0\.0\.0\.0:5000|\[::\]:5000|\*:5000' && fail "Dashboard has a public/wildcard listener on port 5000. Stop and fix it before continuing."

hub_listeners="$("${SUDO[@]}" ss -ltnH "sport = :${WA_HUB_PORT}" 2>/dev/null || true)"
if printf '%s\n' "$hub_listeners" | grep -Eq '0\.0\.0\.0:8088|\[::\]:8088|\*:8088'; then
  fail "WhatsApp hub has a public/wildcard listener on port 8088. Stop and fix it before continuing."
fi

curl --fail --silent --show-error --max-time 5 "http://${APP_HOST}:${APP_PORT}/healthz" >/dev/null \
  || fail "Dashboard health check failed at http://${APP_HOST}:${APP_PORT}/healthz."

# Tailscale Serve publishes only to the authenticated tailnet and proxies to
# the loopback dashboard. This deliberately never invokes 'tailscale funnel'.
"${SUDO[@]}" tailscale serve --bg --https=443 "http://${APP_HOST}:${APP_PORT}"
"${SUDO[@]}" tailscale serve status

echo
cat <<'NOTICE'
Private Serve configured. Confirm the printed URL is a *.ts.net hostname.
In the Tailscale admin console, enable device approval and approve only the VM
and the specific phones/laptops that should access this dashboard. Do not enable
Funnel and do not open ports 5000 or 8088 in the cloud firewall.
NOTICE
