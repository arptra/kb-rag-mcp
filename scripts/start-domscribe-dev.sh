#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
backend_pid=""
frontend_pid=""

cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  if [[ -n "${frontend_pid}" ]] && kill -0 "${frontend_pid}" 2>/dev/null; then
    kill "${frontend_pid}" 2>/dev/null || true
  fi
  if [[ -n "${backend_pid}" ]] && kill -0 "${backend_pid}" 2>/dev/null; then
    kill "${backend_pid}" 2>/dev/null || true
  fi
  wait "${frontend_pid}" 2>/dev/null || true
  wait "${backend_pid}" 2>/dev/null || true
  exit "${exit_status}"
}

trap cleanup EXIT INT TERM

cd "${project_root}"

export KB_DOMSCRIBE_ENABLED=true
export KB_MCP_TLS_ENABLED="${KB_MCP_TLS_ENABLED:-false}"
export VITE_TLS_ENABLED="${VITE_TLS_ENABLED:-${KB_MCP_TLS_ENABLED}}"

echo "Starting dev-only DomScribe/GigaCode mode"
echo "Dashboard: http://127.0.0.1:5173/admin/"
echo "Backend:   http://127.0.0.1:8000"

"${script_dir}/start-backend.sh" &
backend_pid=$!
"${script_dir}/start-frontend.sh" --mode domscribe --host 127.0.0.1 --port 5173 --strictPort &
frontend_pid=$!

while kill -0 "${backend_pid}" 2>/dev/null && kill -0 "${frontend_pid}" 2>/dev/null; do
  sleep 1
done

exit 1
