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
  install-pip   Create/update the local venv with pip; does not use uv
  install-semantic
                Install optional local semantic embedding dependencies
  install-pip-semantic
                Install runtime, dev, and semantic dependencies with pip
  test          Run offline tests with the hash embedding provider
  lint          Run Ruff
  typecheck     Run mypy
  check         Run lint, type checking, and tests
  dashboard-install
                Install React dashboard dependencies from package-lock.json
  dashboard-dev Start the React/TypeScript dashboard development server
  dashboard-build
                Type-check and build dashboard assets served at /admin
  index-hash    Rebuild the offline hash index
  search-hash   Search with hash embeddings; accepts an optional query
  index         Rebuild using the configured provider (hash by default)
  index-ssot    Rebuild the separate global SSOT index incrementally
  search        Search using the configured provider; accepts an optional query
  ssot          Build one cross-service SSOT context; accepts a question
  index-semantic
                Rebuild with an explicitly configured local semantic model
  eval          Run retrieval evaluation
  serve         Start the stdio MCP server
  serve-http    Start the authenticated Streamable HTTP MCP server
EOF
}

case "${command_name}" in
  install)
    exec "${script_dir}/setup-venv.sh" "$@"
    ;;
  install-pip)
    exec "${script_dir}/setup-pip.sh" "$@"
    ;;
  install-semantic)
    exec "${script_dir}/setup-venv.sh" --extra semantic "$@"
    ;;
  install-pip-semantic)
    exec "${script_dir}/setup-pip.sh" --semantic "$@"
    ;;
  help|-h|--help)
    show_help
    exit 0
    ;;
esac

export KB_ACTIVATE_QUIET=true
source "${script_dir}/activate-venv.sh"
unset KB_ACTIVATE_QUIET
python_runtime=("${VIRTUAL_ENV}/bin/python")

case "${command_name}" in
  test)
    KB_EMBEDDING_PROVIDER=hash "${python_runtime[@]}" -m pytest -q "$@"
    ;;
  lint)
    "${python_runtime[@]}" -m ruff check . "$@"
    ;;
  typecheck)
    "${python_runtime[@]}" -m mypy src "$@"
    ;;
  check)
    "${python_runtime[@]}" -m ruff check .
    "${python_runtime[@]}" -m mypy src
    KB_EMBEDDING_PROVIDER=hash "${python_runtime[@]}" -m pytest -q
    npm --prefix apps/dashboard run build
    ;;
  dashboard-install)
    exec npm --prefix apps/dashboard ci "$@"
    ;;
  dashboard-dev)
    exec npm --prefix apps/dashboard run dev -- "$@"
    ;;
  dashboard-build)
    exec npm --prefix apps/dashboard run build -- "$@"
    ;;
  index-hash)
    KB_EMBEDDING_PROVIDER=hash "${python_runtime[@]}" -m corporate_kb.cli index --force --incremental "$@"
    ;;
  search-hash)
    query="${1:-Какой сервис владеет дневными лимитами?}"
    if [[ $# -gt 0 ]]; then
      shift
    fi
    KB_EMBEDDING_PROVIDER=hash "${python_runtime[@]}" -m corporate_kb.cli \
      search "${query}" --top-k 5 "$@"
    ;;
  index)
    "${python_runtime[@]}" -m corporate_kb.cli index --force --incremental "$@"
    ;;
  index-ssot)
    "${python_runtime[@]}" -m corporate_kb.cli index-ssot "$@"
    ;;
  search)
    query="${1:-Какой сервис владеет дневными лимитами?}"
    if [[ $# -gt 0 ]]; then
      shift
    fi
    "${python_runtime[@]}" -m corporate_kb.cli search "${query}" --top-k 5 "$@"
    ;;
  ssot)
    question="${1:-Какие сервисы участвуют в проверке дневного лимита?}"
    if [[ $# -gt 0 ]]; then
      shift
    fi
    "${python_runtime[@]}" -m corporate_kb.cli ssot "${question}" "$@"
    ;;
  index-semantic)
    "${python_runtime[@]}" -m corporate_kb.cli index --force --incremental "$@"
    ;;
  eval)
    "${python_runtime[@]}" -m corporate_kb.cli eval "$@"
    ;;
  serve)
    exec "${script_dir}/start-mcp.sh" "$@"
    ;;
  serve-http)
    exec "${script_dir}/start-mcp-http.sh" "$@"
    ;;
  *)
    echo "Unknown command: ${command_name}" >&2
    echo "Run ./scripts/dev.sh help for usage." >&2
    exit 2
    ;;
esac
