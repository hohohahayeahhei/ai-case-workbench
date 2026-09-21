#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
backend_pid=""
frontend_pid=""
cleanup() {
  [[ -n "$backend_pid" ]] && kill "$backend_pid" 2>/dev/null || true
  [[ -n "$frontend_pid" ]] && kill "$frontend_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python_bin="${PYTHON_BIN:-.venv/bin/python}"
api_port="${API_PORT:-8000}"
ui_port="${UI_PORT:-5173}"

# LaunchAgents and a manual terminal can overlap. Reuse a healthy instance
# instead of starting children that immediately fail with EADDRINUSE.
if ! curl -fsS --max-time 2 "http://127.0.0.1:${api_port}/api/readyz" >/dev/null; then
  "$python_bin" -m uvicorn backend.app.api:app --host 127.0.0.1 --port "$api_port" > /tmp/ai-case-workbench-api.log 2>&1 & backend_pid=$!
fi
if ! curl -fsS --max-time 2 "http://127.0.0.1:${ui_port}/" >/dev/null; then
  (cd frontend && API_PROXY_TARGET="http://127.0.0.1:${api_port}" npm run dev -- --host 127.0.0.1 --port "$ui_port") > /tmp/ai-case-workbench-ui.log 2>&1 & frontend_pid=$!
fi
for _ in {1..30}; do
  if curl -fsS "http://127.0.0.1:${api_port}/api/readyz" >/dev/null && curl -fsS "http://127.0.0.1:${ui_port}/" >/dev/null; then
    echo "AI 案例情报工作台已启动：http://127.0.0.1:${ui_port}"
    wait
    exit 0
  fi
  sleep 1
done
echo "后端启动失败，请查看 /tmp/ai-case-workbench-api.log" >&2
exit 1
