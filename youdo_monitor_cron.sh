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

# Hybrid Mark-voice pitch generator (spec node "гибридный генератор").
# Enabling it does NOT make the LLM mandatory: on timeout / bad voice / missing
# key the generator falls back to the deterministic pitch inside timeout_s=12.
export YOUDO_HYBRID_LLM=1
export YOUDO_OMNIROUTE_URL="${YOUDO_OMNIROUTE_URL:-http://127.0.0.1:20128/v1/chat/completions}"
export YOUDO_HYBRID_MODEL="${YOUDO_HYBRID_MODEL:-omni/pro}"
# API key is never hardcoded here: use the inherited environment, or the
# optional key file if the operator installed one. No key -> deterministic path.
if [ -z "${OMNIROUTE_KEY_GEMINI:-}${OMNIROUTE_API_KEY:-}" ] && [ -r /root/.hermes/youdo_hybrid.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /root/.hermes/youdo_hybrid.env
    set +a
fi

start=$(date +%s)
out=$(timeout 540 "$PY" -m youdo_monitor.runner --once --no-auto-offer --json 2>> logs/run.err)
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
