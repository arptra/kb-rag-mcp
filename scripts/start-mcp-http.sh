#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET

export KB_EMBEDDING_PROVIDER="${KB_EMBEDDING_PROVIDER:-hash}"
export KB_EMBEDDING_LOCAL_FILES_ONLY="${KB_EMBEDDING_LOCAL_FILES_ONLY:-true}"
export KB_AUTO_INDEX="${KB_AUTO_INDEX:-false}"
export KB_MCP_HTTP_HOST="${KB_MCP_HTTP_HOST:-0.0.0.0}"
export KB_MCP_HTTP_PORT="${KB_MCP_HTTP_PORT:-8000}"
export KB_MCP_HTTP_PATH="${KB_MCP_HTTP_PATH:-/mcp}"
export KB_MCP_TLS_ENABLED="${KB_MCP_TLS_ENABLED:-true}"
export KB_MCP_TLS_CERT_FILE="${KB_MCP_TLS_CERT_FILE:-${project_root}/certs/server.crt}"
export KB_MCP_TLS_KEY_FILE="${KB_MCP_TLS_KEY_FILE:-${project_root}/certs/server.key}"
# This deployment is intentionally open: TLS encrypts traffic, but there is no Bearer/admin auth.
export KB_MCP_HTTP_BEARER_TOKEN=""
export KB_ADMIN_PASSWORD=""

case "${KB_MCP_TLS_ENABLED}" in
  1|true|TRUE|yes|YES|on|ON)
    if [[ ! -f "${KB_MCP_TLS_CERT_FILE}" || ! -f "${KB_MCP_TLS_KEY_FILE}" ]]; then
      "${script_dir}/generate-dev-certs.sh"
    fi
    server_scheme="https"
    ;;
  0|false|FALSE|no|NO|off|OFF)
    server_scheme="http"
    ;;
  *)
    echo "KB_MCP_TLS_ENABLED must be a boolean value." >&2
    exit 2
    ;;
esac

runtime_dir="${project_root}/.cache/kb/runtime"
pid_file="${runtime_dir}/mcp-http.pid"
log_file="${runtime_dir}/mcp-http.log"
server_command=("${VIRTUAL_ENV}/bin/python" -m corporate_kb.mcp.http_server)

read_server_pid() {
  if [[ ! -f "${pid_file}" ]]; then
    return 1
  fi
  local pid
  pid="$(<"${pid_file}")"
  if [[ ! "${pid}" =~ ^[0-9]+$ ]]; then
    rm -f "${pid_file}"
    return 1
  fi
  local command
  command="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  if [[ "${command}" != *"corporate_kb.mcp.http_server"* ]]; then
    rm -f "${pid_file}"
    return 1
  fi
  printf '%s\n' "${pid}"
}

descendant_pids() {
  local parent_pid="$1"
  local child_pid
  for child_pid in $(pgrep -P "${parent_pid}" 2>/dev/null || true); do
    descendant_pids "${child_pid}"
    printf '%s\n' "${child_pid}"
  done
}

stop_pid() {
  local pid="$1"
  local descendants
  descendants="$(descendant_pids "${pid}")"
  local descendant
  for descendant in ${descendants}; do
    kill -TERM "${descendant}" 2>/dev/null || true
  done
  kill -TERM "${pid}" 2>/dev/null || true
  for _attempt in {1..50}; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      for descendant in ${descendants}; do
        kill -KILL "${descendant}" 2>/dev/null || true
      done
      return 0
    fi
    sleep 0.1
  done
  echo "Server did not stop in 5 seconds; sending SIGKILL." >&2
  for descendant in ${descendants}; do
    kill -KILL "${descendant}" 2>/dev/null || true
  done
  kill -KILL "${pid}" 2>/dev/null || true
  for _attempt in {1..20}; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

run_server() {
  local existing
  if existing="$(read_server_pid)"; then
    echo "RAG/MCP server is already running (PID ${existing})." >&2
    return 1
  fi
  mkdir -p "${runtime_dir}"
  local child_pid=""
  cleanup_pid() {
    if [[ -n "${child_pid}" && -f "${pid_file}" && "$(<"${pid_file}")" == "${child_pid}" ]]; then
      rm -f "${pid_file}"
    fi
  }
  forward_signal() {
    if [[ -n "${child_pid}" ]]; then
      kill -TERM "${child_pid}" 2>/dev/null || true
    fi
  }
  trap forward_signal INT TERM
  trap cleanup_pid EXIT
  "${server_command[@]}" "$@" &
  child_pid="$!"
  printf '%s\n' "${child_pid}" > "${pid_file}"
  set +e
  wait "${child_pid}"
  local exit_code="$?"
  if kill -0 "${child_pid}" 2>/dev/null; then
    stop_pid "${child_pid}" || true
    wait "${child_pid}" 2>/dev/null || true
  fi
  set -e
  cleanup_pid
  trap - EXIT INT TERM
  if [[ "${exit_code}" -eq 130 || "${exit_code}" -eq 143 ]]; then
    exit_code=0
  fi
  return "${exit_code}"
}

start_server() {
  local existing
  if existing="$(read_server_pid)"; then
    echo "RAG/MCP server is already running (PID ${existing})."
    return 0
  fi
  mkdir -p "${runtime_dir}"
  nohup "${BASH_SOURCE[0]}" __daemon "$@" >>"${log_file}" 2>&1 &
  local launcher_pid="$!"
  for _attempt in {1..50}; do
    if existing="$(read_server_pid)"; then
      local stable=true
      local current=""
      for _stability_check in {1..10}; do
        sleep 0.1
        if ! kill -0 "${launcher_pid}" 2>/dev/null; then
          stable=false
          break
        fi
        if ! current="$(read_server_pid)" || [[ "${current}" != "${existing}" ]]; then
          stable=false
          break
        fi
      done
      if [[ "${stable}" == true ]]; then
        echo "RAG/MCP server started (PID ${existing})."
        echo "Admin: ${server_scheme}://${KB_MCP_HTTP_HOST}:${KB_MCP_HTTP_PORT}/admin"
        echo "MCP:   ${server_scheme}://${KB_MCP_HTTP_HOST}:${KB_MCP_HTTP_PORT}${KB_MCP_HTTP_PATH}"
        echo "Log:   ${log_file}"
        return 0
      fi
      break
    fi
    if ! kill -0 "${launcher_pid}" 2>/dev/null; then
      break
    fi
    sleep 0.1
  done
  echo "RAG/MCP server failed to start. Last log lines:" >&2
  tail -n 30 "${log_file}" >&2 || true
  return 1
}

stop_server() {
  local pid
  if ! pid="$(read_server_pid)"; then
    echo "RAG/MCP server is not running."
    return 0
  fi
  echo "Stopping RAG/MCP server (PID ${pid})..."
  if ! stop_pid "${pid}"; then
    echo "Could not stop RAG/MCP server PID ${pid}." >&2
    return 1
  fi
  rm -f "${pid_file}"
  echo "RAG/MCP server stopped."
}

action="${1:-run}"
if [[ $# -gt 0 ]]; then
  shift
fi
case "${action}" in
  run)
    run_server "$@"
    ;;
  __daemon)
    run_server "$@"
    ;;
  start)
    start_server "$@"
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server
    start_server "$@"
    ;;
  status)
    if pid="$(read_server_pid)"; then
      echo "RAG/MCP server is running (PID ${pid})."
    else
      echo "RAG/MCP server is not running."
      exit 1
    fi
    ;;
  logs)
    touch "${log_file}"
    tail -n 100 -f "${log_file}"
    ;;
  *)
    echo "Usage: $0 {run|start|stop|restart|status|logs}" >&2
    exit 2
    ;;
esac
