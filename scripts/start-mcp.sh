#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET

export KB_EMBEDDING_PROVIDER="${KB_EMBEDDING_PROVIDER:-hash}"
export KB_EMBEDDING_LOCAL_FILES_ONLY="${KB_EMBEDDING_LOCAL_FILES_ONLY:-true}"

exec "${UV_BIN}" run --offline --no-sync --project "${KB_PROJECT_ROOT}" kb-mcp "$@"
