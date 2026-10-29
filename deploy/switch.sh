#!/usr/bin/env bash
# Move the live neo between the Mac mini and spark, from the laptop (ssh hosts "mini" and "spark").
#
#   deploy/switch.sh to-spark [--with-state]   stop the mini's bot, start neo on spark
#   deploy/switch.sh to-mini  [--with-state]   stop neo on spark, start the mini's bot again
#   deploy/switch.sh status                    which one is running
#
# Only one may run at a time: Slack splits events between every connected copy. Each side keeps its own
# state unless --with-state copies it across: neo's files in the checkout (watches, findings, loops,
# cards, Slack thread map, runs) and its memory and people settings. Each side's .env stays its own.
# The mini's other launchd jobs are never touched.
set -euo pipefail

MINI=${NEO_MINI_HOST:-mini}
SPARK=${NEO_SPARK_HOST:-spark}
MINI_DIR=${NEO_MINI_DIR:-neo/slack-claude-bot}          # relative to the mini user's home
MINI_LABEL=${NEO_MINI_LABEL:-com.anil.neo-slack-bot}
SPARK_DIR=${NEO_SPARK_DIR:-/home/neo/slack-claude-bot}
SPARK_HOME=${NEO_SPARK_HOME:-/home/neo}
STATE="auto_rca.json cards.json chief.json findings.json loops.json mentions.json seen.json sessions.json
settings.json titles.json urgent.json usage.json watches.json broker.json personal.json onboarding.json
undo.json sign_in_threads.json digests runs"

say() { printf '==> %s\n' "$*"; }

mini_running() { ssh "$MINI" "launchctl list | grep -q '$MINI_LABEL\$'"; }
spark_running() { ssh "$SPARK" "systemctl is-active --quiet neo"; }

wait_for_mini_runs() {
  say "waiting for runs in progress on the mini (up to 10 min)"
  for _ in $(seq 60); do
    busy=$(ssh "$MINI" "cd $MINI_DIR && python3 - <<'EOF'
import glob, json
n = 0
for p in sorted(glob.glob('runs/*/meta.json'))[-80:]:
    try:
        m = json.load(open(p))
    except Exception:
        continue
    if m.get('status') in ('queued', 'running') and m.get('runtime') != 'herdr':
        n += 1
print(n)
EOF")
    [ "$busy" = "0" ] && return 0
    echo "    $busy still running"
    sleep 10
  done
  echo "    some runs are still going; their replies may not reach Slack"
}

copy_mini_to_spark() {
  say "copying state mini -> spark"
  # shellcheck disable=SC2086
  ssh "$MINI" "cd $MINI_DIR && ls -d $(echo $STATE) 2>/dev/null | xargs tar -cf -" \
    | ssh "$SPARK" "sudo tar -xf - -C $SPARK_DIR && sudo chown -R neo:neo $SPARK_DIR"
  ssh "$MINI" "cd ~/.neo && tar -cf - memory people" \
    | ssh "$SPARK" "sudo tar -xf - -C $SPARK_HOME/.neo && sudo chown -R neo:neo $SPARK_HOME/.neo && sudo chmod -R go-rwx $SPARK_HOME/.neo"
}

copy_spark_to_mini() {
  say "copying state spark -> mini"
  # shellcheck disable=SC2086
  ssh "$SPARK" "sudo bash -c 'cd $SPARK_DIR && ls -d $(echo $STATE) 2>/dev/null | xargs tar -cf -'" \
    | ssh "$MINI" "cd $MINI_DIR && tar -xf -"
  ssh "$SPARK" "sudo tar -C $SPARK_HOME/.neo -cf - memory people" | ssh "$MINI" "cd ~/.neo && tar -xf -"
}

WITH_STATE=0
[ "${2:-}" = "--with-state" ] && WITH_STATE=1

case "${1:-}" in
  to-spark)
    spark_running && { say "neo already runs on spark"; exit 0; }
    if mini_running; then
      wait_for_mini_runs
      say "stopping the mini's bot"
      ssh "$MINI" "launchctl bootout gui/\$(id -u)/$MINI_LABEL 2>/dev/null; launchctl disable gui/\$(id -u)/$MINI_LABEL"
      mini_running && { echo "the mini's bot is still running; stopping here"; exit 1; }
    fi
    [ "$WITH_STATE" = 1 ] && copy_mini_to_spark
    say "starting neo on spark"
    ssh "$SPARK" "sudo systemctl disable --now neo-web 2>/dev/null || true; cd /home && sudo bash -c 'cd $SPARK_DIR && deploy/install-linux.sh --start' 2>&1 | tail -6"
    spark_running && say "neo runs on spark" || { echo "neo didn't start on spark: run 'deploy/switch.sh to-mini'"; exit 1; }
    ;;
  to-mini)
    if spark_running; then
      say "stopping neo on spark"
      ssh "$SPARK" "sudo systemctl disable --now neo"
    fi
    [ "$WITH_STATE" = 1 ] && copy_spark_to_mini
    say "starting the mini's bot"
    ssh "$MINI" "launchctl enable gui/\$(id -u)/$MINI_LABEL && launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$MINI_LABEL.plist 2>/dev/null || launchctl kickstart -k gui/\$(id -u)/$MINI_LABEL"
    sleep 5
    mini_running && say "neo runs on the mini again" || { echo "the mini's bot didn't start; check ~/Library/Logs/neo-slack-bot.log"; exit 1; }
    ;;
  status)
    mini_running && echo "mini: running" || echo "mini: stopped"
    spark_running && echo "spark: running" || echo "spark: stopped"
    ;;
  *) echo "usage: deploy/switch.sh to-spark | to-mini | status"; exit 2 ;;
esac
