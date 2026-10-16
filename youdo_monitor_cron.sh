#!/bin/bash
# YouDo monitor — cron wrapper (no_agent job).
# Runs one monitoring cycle: task feed + account notifications -> Telegram.
set -uo pipefail

cd /root/youdo-monitor || exit 1

PY=/root/.hermes/hermes-agent/venv/bin/python
LOCK=/tmp/youdo_monitor.lock
LOG=logs/cron.log
mkdir -p logs

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date '+%F %T') previous cycle still running, skipping" >> "$LOG"
    exit 0
fi

export PYTHONPATH=/root/youdo-monitor
start=$(date +%s)
out=$(timeout 540 "$PY" -m youdo_monitor.runner --once --json 2>> logs/run.err)
rc=$?
elapsed=$(( $(date +%s) - start ))

if [ $rc -ne 0 ]; then
    echo "$(date '+%F %T') FAILED rc=$rc after ${elapsed}s" >> "$LOG"
    tail -20 logs/run.err >> "$LOG"
    exit $rc
fi

echo "$(date '+%F %T') ok ${elapsed}s $out" >> "$LOG"

# no_agent jobs deliver stdout verbatim, so stay silent unless something
# notable happened — the per-run stats line lives in cron.log instead.
notable=$(echo "$out" | grep -Eo '"(new|matched|notified|errors|send_failures)": [1-9][0-9]*' | head -5)
auth_false=$(echo "$out" | grep -Eo '"authenticated": false')
if [ -n "$notable" ] || [ -n "$auth_false" ]; then
    echo "YouDo: $out"
fi

# Keep the error log from growing forever.
if [ -f logs/run.err ] && [ "$(stat -c%s logs/run.err)" -gt 524288 ]; then
    tail -200 logs/run.err > logs/run.err.tmp && mv logs/run.err.tmp logs/run.err
fi
exit 0
