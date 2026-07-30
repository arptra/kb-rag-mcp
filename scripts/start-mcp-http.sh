#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET

export KB_EMBEDDING_PROVIDER="${KB_EMBEDDING_PROVIDER:-hash}"
export KB_EMBEDDING_LOCAL_FILES_ONLY="${KB_EMBEDDING_LOCAL_FILES_ONLY:-true}"
export KB_AUTO_INDEX="${KB_AUTO_INDEX:-false}"
export KB_MCP_HTTP_HOST="${KB_MCP_HTTP_HOST:-0.0.0.0}"
export KB_MCP_HTTP_PORT="${KB_MCP_HTTP_PORT:-8000}"
export KB_MCP_HTTP_PATH="${KB_MCP_HTTP_PATH:-/mcp}"

if [[ -z "${KB_MCP_HTTP_BEARER_TOKEN:-}" ]]; then
  printf '%s\n' \
    "KB_MCP_HTTP_BEARER_TOKEN is required." \
    "Generate one with: openssl rand -hex 32" >&2
  exit 2
fi

if [[ -z "${KB_MCP_HTTP_ALLOWED_HOSTS:-}" ]]; then
  printf '%s\n' \
    "KB_MCP_HTTP_ALLOWED_HOSTS is required for the default external bind." \
    "Example: export KB_MCP_HTTP_ALLOWED_HOSTS='kb.example.com,10.0.0.5:*'" >&2
  exit 2
fi

exec "${VIRTUAL_ENV}/bin/python" -m corporate_kb.mcp.http_server "$@"
