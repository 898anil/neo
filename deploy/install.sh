#!/usr/bin/env bash
# Install or update neo on this Mac (meant for the always-on Mac mini).
#
#   deploy/install.sh
#
# Idempotent. It:
#   1. installs herdr (Homebrew) if missing, and the Python dependencies (Poetry)
#   2. publishes the dashboard on your tailnet with `tailscale serve` (HTTPS, tailnet only)
#   3. adds dashboard settings to .env if they are missing (never touches the Slack tokens)
#   4. writes and (re)loads the launchd agent so the bot survives logout and reboots
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
say() { printf '\n==> %s\n' "$*"; }

[ -f .env ] || { echo "Missing $ROOT/.env (copy .env.example and add the Slack tokens)"; exit 1; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
# launchd label: NEO_LAUNCHD_LABEL from the environment or .env (keep it stable: a new label means a second agent).
LABEL="${NEO_LAUNCHD_LABEL:-$(env_get NEO_LAUNCHD_LABEL)}"
LABEL="${LABEL:-com.neo.slack-bot}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
env_set_default() {  # add KEY=VALUE only when KEY is not already set
  if ! grep -qE "^$1=" .env; then printf '%s=%s\n' "$1" "$2" >> .env; echo "  .env: added $1"; fi
}
env_unset() {  # remove KEY (used when switching from the token fallback to Tailscale Serve)
  if grep -qE "^$1=" .env; then grep -vE "^$1=" .env > .env.tmp && mv .env.tmp .env; echo "  .env: removed $1"; fi
}

say "Tools"
command -v claude >/dev/null && echo "  claude: $(claude --version 2>/dev/null)" || echo "  claude: missing"
command -v codex >/dev/null && echo "  codex:  $(codex --version 2>/dev/null)" || echo "  codex:  missing"
if ! command -v herdr >/dev/null; then
  say "Installing herdr (live agents you can attach to)"
  brew install herdr
fi
echo "  herdr:  $(herdr --version)"
command -v tmux >/dev/null || brew install tmux

say "Python dependencies"
poetry install -q
PY="$(poetry env info -p)/bin/python"
echo "  $PY"

say "Tests"
"$PY" -m unittest discover -s tests 2>&1 | tail -1

# Prefer a plain CLI build: the Tailscale.app binary tries to open its GUI and hangs over SSH.
# TAILSCALE_BIN (environment or .env) points at a specific tailscale binary.
TS=""
for candidate in "${TAILSCALE_BIN:-$(env_get TAILSCALE_BIN)}" "$(command -v tailscale || true)" /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then TS="$candidate"; break; fi
done
PORT="$(env_get NEO_DASHBOARD_PORT)"; PORT="${PORT:-8788}"
env_set_default NEO_DASHBOARD_PORT "$PORT"
run_limited() {  # run "$@" for at most $1 seconds (macOS has no `timeout`)
  local secs="$1"; shift
  "$@" > /tmp/neo-install.out 2>&1 & local pid=$!
  for _ in $(seq 1 "$secs"); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill "$pid" 2>/dev/null || true
  cat /tmp/neo-install.out
}
if [ "$(env_get NEO_TAILSCALE_SERVE)" = "0" ]; then
  # Second user on a shared Mac: the tailnet node and its `serve` config belong to the owner.
  echo "  NEO_TAILSCALE_SERVE=0: dashboard on $(env_get NEO_DASHBOARD_URL) (host/token from .env)"
elif [ -n "$TS" ]; then
  say "Dashboard on your tailnet"
  read -r DNS IP LOGIN < <(run_limited 10 "$TS" status --json | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
me = d["Self"]
user = (d.get("User") or {}).get(str(me.get("UserID")), {})
print(me["DNSName"].rstrip("."), me["TailscaleIPs"][0], user.get("LoginName", "-"))' 2>/dev/null || true) || true
  if [ -z "${DNS:-}" ] || [ -z "${IP:-}" ]; then
    echo "  Tailscale is not logged in or not responding; dashboard stays on http://127.0.0.1:$PORT"
    SERVE_OUT="skipped"
  else
    SERVE_OUT="$(run_limited 12 "$TS" serve --bg "http://127.0.0.1:$PORT")"
  fi
  if [ "$SERVE_OUT" != "skipped" ] && ! echo "$SERVE_OUT" | grep -qi "not enabled\|error\|failed"; then
    # Serve can accept the config yet fail every request (no HTTPS certificate on the tailnet).
    # Prove it end to end before relying on it.
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --resolve "$DNS:443:$IP" "https://$DNS/" || true)"
    if [ "$CODE" != "200" ] && [ "$CODE" != "403" ]; then
      run_limited 10 "$TS" serve reset >/dev/null
      SERVE_OUT="Serve is configured but HTTPS does not work (enable HTTPS Certificates and MagicDNS for the tailnet) - error"
    fi
  fi
  if [ "$SERVE_OUT" = "skipped" ]; then
    :
  elif echo "$SERVE_OUT" | grep -qi "not enabled\|error\|failed"; then
    # Tailscale Serve needs a one-time admin approval. Until then: listen on the tailnet
    # address only (never the LAN), with a token that the phone keeps as a cookie.
    echo "  Tailscale Serve is not usable yet:"
    echo "$SERVE_OUT" | grep -o 'https://login.tailscale.com[^ ]*\|HTTPS does not work.*' | sed 's/^/    /'
    echo "  Falling back to http://$DNS:$PORT with a dashboard token."
    case "$(env_get NEO_DASHBOARD_URL)" in https://*) env_unset NEO_DASHBOARD_URL ;; esac
    env_set_default NEO_DASHBOARD_HOST "$IP"
    env_set_default NEO_DASHBOARD_URL "http://$IP:$PORT"
    # Your own tailnet devices are recognised with `tailscale whois`; the token is for anything else.
    [ "$LOGIN" != "-" ] && env_set_default COS_TAILSCALE_USERS "$LOGIN"
    env_set_default NEO_TAILSCALE_CLI "$TS"
    env_set_default COS_TOKEN "$(openssl rand -hex 24)"
    echo "  Your devices ($(env_get COS_TAILSCALE_USERS)): http://$IP:$PORT  (no token needed)"
    echo "  Anything else: http://$IP:$PORT/?token=$(env_get COS_TOKEN)"
  else
    # Serve works: undo the token fallback so the bot listens on 127.0.0.1 behind `tailscale serve`.
    env_unset NEO_DASHBOARD_HOST
    case "$(env_get NEO_DASHBOARD_URL)" in https://*) ;; *) env_unset NEO_DASHBOARD_URL ;; esac
    env_set_default NEO_DASHBOARD_URL "https://$DNS"
    [ "$LOGIN" != "-" ] && env_set_default COS_TAILSCALE_USERS "$LOGIN"
    echo "  https://$DNS  (Tailscale login allowed: $(env_get COS_TAILSCALE_USERS); COS_TOKEN still works)"
  fi
else
  echo "  tailscale not found: dashboard stays on http://127.0.0.1:$PORT"
fi

say "launchd agent"
TOOL_PATH="$(dirname "$(command -v claude || echo /usr/bin/true)"):$(dirname "$(command -v codex || echo /usr/bin/true)"):$(dirname "$(command -v herdr)"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$PY</string><string>app.py</string></array>
    <key>WorkingDirectory</key><string>$ROOT</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$HOME</string>
        <key>PATH</key><string>$TOOL_PATH</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>StandardOutPath</key><string>$HOME/Library/Logs/neo-slack-bot.log</string>
    <key>StandardErrorPath</key><string>$HOME/Library/Logs/neo-slack-bot.log</string>
</dict>
</plist>
PLIST_EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 5
STATE="$(launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null || true)"
if echo "$STATE" | grep -q "state = running"; then
  echo "  running; log: ~/Library/Logs/neo-slack-bot.log"
  grep "Starting Neo bot" "$HOME/Library/Logs/neo-slack-bot.log" | tail -1
else
  echo "  NOT running; last log lines:"; tail -20 "$HOME/Library/Logs/neo-slack-bot.log"; exit 1
fi
