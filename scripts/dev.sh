#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${project_root}"

command_name="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

show_help() {
  cat <<'EOF'
Usage: ./scripts/dev.sh <command> [arguments]

Commands:
  install       Create/update the local venv and install locked dependencies
  install-semantic
                Install optional local semantic embedding dependencies
  test          Run offline tests with the hash embedding provider
  lint          Run Ruff
  typecheck     Run mypy
  check         Run lint, type checking, and tests
  index-hash    Rebuild the offline hash index
  search-hash   Search with hash embeddings; accepts an optional query
  index         Rebuild using the configured provider (hash by default)
  search        Search using the configured provider; accepts an optional query
  index-semantic
                Rebuild with an explicitly configured local semantic model
  eval          Run retrieval evaluation
  serve         Start the stdio MCP server
EOF
}

case "${command_name}" in
  install)
    exec "${script_dir}/setup-venv.sh" "$@"
    ;;
  install-semantic)
    exec "${script_dir}/setup-venv.sh" --extra semantic "$@"
    ;;
  help|-h|--help)
    show_help
    exit 0
    ;;
esac

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET
uv_runtime=("${UV_BIN}" run --offline --no-sync)

case "${command_name}" in
  test)
    KB_EMBEDDING_PROVIDER=hash "${uv_runtime[@]}" pytest -q "$@"
    ;;
  lint)
    "${uv_runtime[@]}" ruff check . "$@"
    ;;
  typecheck)
    "${uv_runtime[@]}" mypy src "$@"
    ;;
  check)
    "${uv_runtime[@]}" ruff check .
    "${uv_runtime[@]}" mypy src
    KB_EMBEDDING_PROVIDER=hash "${uv_runtime[@]}" pytest -q
    ;;
  index-hash)
    KB_EMBEDDING_PROVIDER=hash "${uv_runtime[@]}" kb index --force "$@"
    ;;
  search-hash)
    query="${1:-Какой сервис владеет дневными лимитами?}"
    if [[ $# -gt 0 ]]; then
      shift
    fi
    KB_EMBEDDING_PROVIDER=hash "${uv_runtime[@]}" kb search "${query}" --top-k 5 "$@"
    ;;
  index)
    "${uv_runtime[@]}" kb index --force "$@"
    ;;
  search)
    query="${1:-Какой сервис владеет дневными лимитами?}"
    if [[ $# -gt 0 ]]; then
      shift
    fi
    "${uv_runtime[@]}" kb search "${query}" --top-k 5 "$@"
    ;;
  index-semantic)
    "${uv_runtime[@]}" kb index --force "$@"
    ;;
  eval)
    "${uv_runtime[@]}" kb eval "$@"
    ;;
  serve)
    exec "${script_dir}/start-mcp.sh" "$@"
    ;;
  *)
    echo "Unknown command: ${command_name}" >&2
    echo "Run ./scripts/dev.sh help for usage." >&2
    exit 2
    ;;
esac
