#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

log_dir="${AI_CASE_LOG_DIR:-data/runtime/logs}"
mkdir -p "$log_dir"
python_bin="${PYTHON_BIN:-.venv/bin/python}"
# Retry only explicit rejections; uncertain delivery outcomes are never resent.
"$python_bin" scripts/notify_collection.py --retry-pending --send --quiet >> "$log_dir/notifications.log" 2>&1 || true
timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
api_status="$(curl -fsS --max-time 8 http://127.0.0.1:8000/api/readyz 2>&1 || true)"
ui_status="$(curl -fsS --max-time 8 http://127.0.0.1:5173/ 2>&1 | head -c 80 || true)"

if [[ "$api_status" == *'"status":"ready"'* && "$ui_status" == *'<!doctype html>'* ]]; then
  printf '%s ready api=%s ui=ok\n' "$timestamp" "$api_status" >> "$log_dir/health.log"
  exit 0
fi

printf '%s unhealthy api=%s ui=%s\n' "$timestamp" "$api_status" "$ui_status" >> "$log_dir/health.log"
exit 1
