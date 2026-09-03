#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
dashboard_dir="${project_root}/apps/dashboard"

cd "${project_root}"

export VITE_TLS_ENABLED="${VITE_TLS_ENABLED:-${KB_MCP_TLS_ENABLED:-true}}"
case "${VITE_TLS_ENABLED}" in
  1|true|TRUE|yes|YES|on|ON)
    export KB_MCP_TLS_CERT_FILE="${KB_MCP_TLS_CERT_FILE:-${project_root}/certs/server.crt}"
    export KB_MCP_TLS_KEY_FILE="${KB_MCP_TLS_KEY_FILE:-${project_root}/certs/server.key}"
    if [[ ! -f "${KB_MCP_TLS_CERT_FILE}" || ! -f "${KB_MCP_TLS_KEY_FILE}" ]]; then
      "${script_dir}/generate-dev-certs.sh"
    fi
    export VITE_TLS_CERT_FILE="${KB_MCP_TLS_CERT_FILE}"
    export VITE_TLS_KEY_FILE="${KB_MCP_TLS_KEY_FILE}"
    export VITE_BACKEND_URL="${VITE_BACKEND_URL:-https://127.0.0.1:8000}"
    ;;
  0|false|FALSE|no|NO|off|OFF)
    export VITE_BACKEND_URL="${VITE_BACKEND_URL:-http://127.0.0.1:8000}"
    ;;
  *)
    echo "VITE_TLS_ENABLED must be a boolean value." >&2
    exit 2
    ;;
esac

if [[ ! -d "${dashboard_dir}/node_modules" ]]; then
  echo "Frontend dependencies are missing; running npm ci..."
  npm --prefix "${dashboard_dir}" ci
fi

exec npm --prefix "${dashboard_dir}" run dev -- "$@"
