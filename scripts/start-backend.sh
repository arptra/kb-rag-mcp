#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

if [[ ! -x "${project_root}/.venv/bin/python" ]]; then
  echo "Python environment is missing; installing backend dependencies..."
  "${script_dir}/setup-pip.sh"
fi

export KB_MCP_HTTP_HOST="${KB_MCP_HTTP_HOST:-127.0.0.1}"
export KB_MCP_HTTP_PORT="${KB_MCP_HTTP_PORT:-8000}"
export KB_AUTO_INDEX="${KB_AUTO_INDEX:-false}"

exec "${script_dir}/start-mcp-http.sh" run "$@"
