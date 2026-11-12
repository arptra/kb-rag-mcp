#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
cd "${project_root}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: ./scripts/start-dev-frontend.sh [Vite options]"
  echo "HTTPS Vite dashboard on 127.0.0.1:5173, with live updates when UI files change."
  echo "Optional: KB_DEV_BACKEND_PORT, KB_DEV_FRONTEND_PORT, KB_MCP_TLS_CERT_FILE, KB_MCP_TLS_KEY_FILE."
  echo "Use the same backend port and certificate settings in both terminals."
  exit 0
fi

export KB_MCP_TLS_ENABLED=true
export VITE_TLS_ENABLED=true
export KB_MCP_TLS_CERT_FILE="${KB_MCP_TLS_CERT_FILE:-${project_root}/certs/server.crt}"
export KB_MCP_TLS_KEY_FILE="${KB_MCP_TLS_KEY_FILE:-${project_root}/certs/server.key}"
export VITE_BACKEND_URL="https://127.0.0.1:${KB_DEV_BACKEND_PORT:-8000}"
frontend_port="${KB_DEV_FRONTEND_PORT:-5173}"

"${script_dir}/generate-dev-certs.sh" localhost

echo "Dev dashboard: https://127.0.0.1:${frontend_port}/admin/"
echo "Backend: ${VITE_BACKEND_URL} (start it separately with ./scripts/start-dev-backend.sh)"
echo "Server certificate to trust in your browser: ${KB_MCP_TLS_CERT_FILE}"
exec "${script_dir}/start-frontend.sh" --host 127.0.0.1 --port "${frontend_port}" --strictPort "$@"
