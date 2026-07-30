#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

show_help() {
  printf '%s\n' \
    "Usage: ./scripts/install-mcp-server.sh <mcp-servers-directory> [--copy-only|--pip]" \
    "" \
    "Installs this server into:" \
    "  <mcp-servers-directory>/corporate-kb" \
    "" \
    "Options:" \
    "  --copy-only  Copy runtime files without creating .venv or building the index" \
    "  --pip        Install with pip without using uv"
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  show_help >&2
  exit 2
fi

if [[ "${1}" == "-h" || "${1}" == "--help" ]]; then
  show_help
  exit 0
fi

target_root_input="${1}"
install_mode=uv

if [[ $# -eq 2 ]]; then
  if [[ "${2}" != "--copy-only" ]]; then
    if [[ "${2}" != "--pip" ]]; then
      printf 'Unknown option: %s\n' "${2}" >&2
      show_help >&2
      exit 2
    fi
    install_mode=pip
  else
    install_mode=copy
  fi
fi

mkdir -p -- "${target_root_input}"
target_root="$(cd -- "${target_root_input}" && pwd)"
install_dir="${target_root}/corporate-kb"

case "${install_dir}/" in
  "${project_root}/"*)
    printf 'Target must be outside the source repository: %s\n' "${install_dir}" >&2
    exit 2
    ;;
esac

printf 'Installing corporate-kb into %s\n' "${install_dir}"

mkdir -p -- \
  "${install_dir}/src" \
  "${install_dir}/scripts" \
  "${install_dir}/knowledge"

cp -R "${project_root}/src/." "${install_dir}/src/"
cp -R "${project_root}/knowledge/." "${install_dir}/knowledge/"
cp "${project_root}/pyproject.toml" "${install_dir}/pyproject.toml"
cp "${project_root}/uv.lock" "${install_dir}/uv.lock"
cp "${project_root}/README.md" "${install_dir}/README.md"
cp "${project_root}/.env.example" "${install_dir}/.env.example"
cp "${project_root}/scripts/setup-venv.sh" "${install_dir}/scripts/setup-venv.sh"
cp "${project_root}/scripts/setup-pip.sh" "${install_dir}/scripts/setup-pip.sh"
cp "${project_root}/scripts/activate-venv.sh" "${install_dir}/scripts/activate-venv.sh"
cp "${project_root}/scripts/dev.sh" "${install_dir}/scripts/dev.sh"
cp "${project_root}/scripts/start-mcp.sh" "${install_dir}/scripts/start-mcp.sh"
cp "${project_root}/scripts/start-mcp-http.sh" "${install_dir}/scripts/start-mcp-http.sh"
chmod +x "${install_dir}/scripts/"*.sh

if [[ "${install_mode}" == uv ]]; then
  "${install_dir}/scripts/setup-venv.sh" --no-dev
  "${install_dir}/scripts/dev.sh" index-hash
elif [[ "${install_mode}" == pip ]]; then
  "${install_dir}/scripts/setup-pip.sh" --no-dev
  "${install_dir}/scripts/dev.sh" index-hash
fi

if [[ ! -x "${install_dir}/.venv/bin/python" ]]; then
  printf '\nRuntime files copied. Complete installation with:\n'
  printf '  %q/scripts/setup-pip.sh --no-dev\n' "${install_dir}"
  printf '  %q/scripts/dev.sh index-hash\n' "${install_dir}"
  exit 0
fi

printf '\nInstallation complete. Add this entry to Qwen settings.json:\n\n'
"${install_dir}/.venv/bin/python" -c '
import json
import sys

root = sys.argv[1]
entry = {
    "command": f"{root}/.venv/bin/python",
    "args": ["-m", "corporate_kb.mcp.server"],
    "cwd": root,
    "env": {
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": f"{root}/src",
        "KB_KNOWLEDGE_DIR": f"{root}/knowledge",
        "KB_CACHE_DIR": f"{root}/.cache/kb",
        "KB_EMBEDDING_PROVIDER": "hash",
        "KB_EMBEDDING_LOCAL_FILES_ONLY": "true",
        "KB_AUTO_INDEX": "false",
        "HF_HUB_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "DO_NOT_TRACK": "1",
        "KB_LOG_LEVEL": "INFO",
    },
    "includeTools": [
        "kb_search",
        "kb_get_document",
        "kb_list_documents",
        "kb_stats",
    ],
    "discoveryTimeoutMs": 30000,
    "timeout": 120000,
    "trust": False,
}
print(json.dumps({"local-corporate-kb": entry}, ensure_ascii=False, indent=2))
' "${install_dir}"
