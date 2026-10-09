#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET

cd -- "${project_root}"
exec "${VIRTUAL_ENV}/bin/python" -m skill_registry.mcp_server "$@"
