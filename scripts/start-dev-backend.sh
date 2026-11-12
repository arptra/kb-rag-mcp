#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
cd "${project_root}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "Usage: ./scripts/start-dev-backend.sh"
  echo "HTTPS backend on 127.0.0.1:8000, using the project's configured data and access rules."
  echo "Optional: KB_DEV_BACKEND_PORT, KB_MCP_TLS_CERT_FILE, KB_MCP_TLS_KEY_FILE."
  echo "Creates a missing local certificate pair; browser trust is configured manually."
  exit 0
fi

export KB_MCP_TLS_ENABLED=true
export KB_MCP_TLS_CERT_FILE="${KB_MCP_TLS_CERT_FILE:-${project_root}/certs/server.crt}"
export KB_MCP_TLS_KEY_FILE="${KB_MCP_TLS_KEY_FILE:-${project_root}/certs/server.key}"
export KB_MCP_HTTP_HOST=127.0.0.1
export KB_MCP_HTTP_PORT="${KB_DEV_BACKEND_PORT:-8000}"

"${script_dir}/generate-dev-certs.sh" localhost

echo "Dev backend: https://127.0.0.1:${KB_MCP_HTTP_PORT}"
echo "Certificate login / MCP access: https://127.0.0.1:${KB_MCP_HTTP_PORT}/connect"
echo "Server certificate to trust in your browser: ${KB_MCP_TLS_CERT_FILE}"
exec "${script_dir}/start-backend.sh" "$@"
