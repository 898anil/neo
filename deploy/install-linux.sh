#!/usr/bin/env bash
# Install or update neo on a Linux server (systemd), run as root from the service account's checkout.
#
#   sudo deploy/install-linux.sh           # install, but leave the bot stopped
#   sudo deploy/install-linux.sh --start   # install and (re)start it
#
# The bot runs as the account that owns this checkout (e.g. `neo`). Start it only after stopping any other copy:
# Slack splits Socket Mode events across every connected copy of the bot.
#
# Idempotent. It:
#   1. installs the Python dependencies (Poetry, as the service account) and runs the tests
#   2. publishes the dashboard on your tailnet with `tailscale serve` on its own HTTPS port
#      (NEO_TAILSCALE_HTTPS_PORT, default 8443), leaving any other serve config on this machine alone
#   3. adds dashboard settings to .env if they are missing (never touches the Slack tokens)
#   4. writes the systemd unit, and with --start enables and (re)starts it
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
START=0
[ "${1:-}" = "--start" ] && START=1
say() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "Run with sudo: it writes /etc/systemd/system and configures tailscale serve"; exit 1; }
[ -f .env ] || { echo "Missing $ROOT/.env (copy .env.example and add the Slack tokens)"; exit 1; }
SVC_USER="$(stat -c %U "$ROOT")"
SVC_HOME="$(getent passwd "$SVC_USER" | cut -d: -f6)"
[ "$SVC_USER" != root ] || { echo "The checkout is owned by root; clone it as the service account (e.g. neo)"; exit 1; }
as_svc() { sudo -u "$SVC_USER" -H env PATH="/usr/local/bin:/usr/bin:/bin" "$@"; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
env_set_default() {  # add KEY=VALUE only when KEY is not already set
  if ! grep -qE "^$1=" .env; then printf '%s=%s\n' "$1" "$2" >> .env; echo "  .env: added $1"; fi
}
env_unset() {
  if grep -qE "^$1=" .env; then grep -vE "^$1=" .env > .env.tmp && cat .env.tmp > .env && rm .env.tmp; echo "  .env: removed $1"; fi
}
UNIT="$(env_get NEO_SYSTEMD_UNIT)"; UNIT="${UNIT:-neo}"
LOG_DIR="$SVC_HOME/.neo/logs"
LOG="$LOG_DIR/neo-slack-bot.log"

say "Tools"
for tool in claude codex herdr gh git; do
  if command -v "$tool" >/dev/null; then echo "  $tool: $($tool --version 2>/dev/null | head -1)"; else echo "  $tool: missing"; fi
done
command -v poetry >/dev/null || { echo "  poetry: missing (sudo PIPX_HOME=/opt/pipx PIPX_BIN_DIR=/usr/local/bin pipx install poetry)"; exit 1; }

say "Python dependencies (as $SVC_USER)"
as_svc poetry config virtualenvs.in-project true
as_svc poetry install -q
PY="$ROOT/.venv/bin/python"
echo "  $PY"

say "Tests"
as_svc "$PY" -m unittest discover -s tests 2>&1 | tail -1

PORT="$(env_get NEO_DASHBOARD_PORT)"; PORT="${PORT:-8788}"
env_set_default NEO_DASHBOARD_PORT "$PORT"
HTTPS_PORT="$(env_get NEO_TAILSCALE_HTTPS_PORT)"; HTTPS_PORT="${HTTPS_PORT:-8443}"
if [ "$(env_get NEO_TAILSCALE_SERVE)" = "0" ]; then
  echo "  NEO_TAILSCALE_SERVE=0: dashboard on $(env_get NEO_DASHBOARD_URL) (host/token from .env)"
elif command -v tailscale >/dev/null; then
  say "Dashboard on your tailnet (HTTPS port $HTTPS_PORT)"
  read -r DNS LOGIN < <(timeout 10 tailscale status --json | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
me = d["Self"]
user = (d.get("User") or {}).get(str(me.get("UserID")), {})
print(me["DNSName"].rstrip("."), user.get("LoginName", "-"))' 2>/dev/null || true) || true
  if [ -z "${DNS:-}" ]; then
    echo "  Tailscale is not logged in or not responding; dashboard stays on http://127.0.0.1:$PORT"
  elif timeout 12 tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$PORT" >/dev/null; then
    # Listen on 127.0.0.1 behind serve; drop a tailnet address copied from another machine.
    env_unset NEO_DASHBOARD_HOST
    case "$(env_get NEO_DASHBOARD_URL)" in "https://$DNS:$HTTPS_PORT") ;; *) env_unset NEO_DASHBOARD_URL ;; esac
    env_set_default NEO_DASHBOARD_URL "https://$DNS:$HTTPS_PORT"
    echo "  https://$DNS:$HTTPS_PORT  (Tailscale logins allowed: $(env_get COS_TAILSCALE_USERS); COS_TOKEN still works)"
  else
    echo "  tailscale serve failed; dashboard stays on http://127.0.0.1:$PORT"
  fi
else
  echo "  tailscale not found: dashboard stays on http://127.0.0.1:$PORT"
fi

say "systemd unit $UNIT.service"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$LOG_DIR"
cat > "/etc/systemd/system/$UNIT.service" <<UNIT_EOF
[Unit]
Description=neo Slack bot ($SVC_USER)
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$SVC_USER
Group=$SVC_USER
WorkingDirectory=$ROOT
Environment=HOME=$SVC_HOME
Environment=PATH=$SVC_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=NEO_ACTIVITY_LOG=$LOG
ExecStart=$PY app.py
Restart=always
RestartSec=10
# herdr and live agents are started by the bot; a restart must not end them.
KillMode=process
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
if [ "$START" = 1 ]; then
  systemctl enable -q "$UNIT"
  systemctl restart "$UNIT"
  sleep 5
  if systemctl is-active -q "$UNIT"; then
    echo "  running; log: $LOG"
    grep "Starting Neo bot" "$LOG" | tail -1 || true
  else
    echo "  NOT running; last log lines:"; tail -20 "$LOG"; exit 1
  fi
else
  echo "  written, not started. Stop every other copy of the bot first, then: sudo $0 --start"
fi
