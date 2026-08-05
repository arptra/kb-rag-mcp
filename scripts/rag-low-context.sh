#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"

# Balanced low-context profile. Values supplied by the administrator take priority.
export KB_AUTO_INDEX="${KB_AUTO_INDEX:-false}"
export KB_DEFAULT_TOP_K="${KB_DEFAULT_TOP_K:-3}"
export KB_SEARCH_CANDIDATE_K="${KB_SEARCH_CANDIDATE_K:-12}"
export KB_SEARCH_MAX_RESULTS="${KB_SEARCH_MAX_RESULTS:-2}"
export KB_SEARCH_EXCERPT_TOKENS="${KB_SEARCH_EXCERPT_TOKENS:-160}"
export KB_SEARCH_CONTEXT_TOKENS="${KB_SEARCH_CONTEXT_TOKENS:-500}"
export KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT="${KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT:-1}"
export KB_DOCUMENT_CONTEXT_TOKENS="${KB_DOCUMENT_CONTEXT_TOKENS:-350}"
export KB_MCP_MINIMAL_TOOLS="${KB_MCP_MINIMAL_TOOLS:-true}"
export KB_LOG_LEVEL="${KB_LOG_LEVEL:-INFO}"

show_help() {
  printf '%s\n' \
    "Usage: ./scripts/rag-low-context.sh <command>" \
    "" \
    "Commands:" \
    "  config    Print the active non-secret low-context settings" \
    "  reindex   Explicitly rebuild the index and reuse unchanged embeddings" \
    "  serve     Load the prepared cache and start the authenticated HTTP server"
}

show_config() {
  printf '%s\n' \
    "KB_AUTO_INDEX=${KB_AUTO_INDEX}" \
    "KB_DEFAULT_TOP_K=${KB_DEFAULT_TOP_K}" \
    "KB_SEARCH_CANDIDATE_K=${KB_SEARCH_CANDIDATE_K}" \
    "KB_SEARCH_MAX_RESULTS=${KB_SEARCH_MAX_RESULTS}" \
    "KB_SEARCH_EXCERPT_TOKENS=${KB_SEARCH_EXCERPT_TOKENS}" \
    "KB_SEARCH_CONTEXT_TOKENS=${KB_SEARCH_CONTEXT_TOKENS}" \
    "KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT=${KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT}" \
    "KB_DOCUMENT_CONTEXT_TOKENS=${KB_DOCUMENT_CONTEXT_TOKENS}" \
    "KB_MCP_MINIMAL_TOOLS=${KB_MCP_MINIMAL_TOOLS}" \
    "KB_LOG_LEVEL=${KB_LOG_LEVEL}"
}

command_name="${1:-help}"
case "${command_name}" in
  config)
    show_config
    ;;
  reindex)
    printf '%s\n' \
      "Building the prepared index with incremental embedding reuse..." \
      "The HTTP server can keep serving the old cache until this job finishes."
    exec "${script_dir}/dev.sh" index
    ;;
  serve)
    if [[ "${KB_AUTO_INDEX}" != "false" ]]; then
      printf '%s\n' "KB_AUTO_INDEX must be false in the low-context production profile." >&2
      exit 2
    fi
    exec "${script_dir}/start-mcp-http.sh"
    ;;
  help|-h|--help)
    show_help
    ;;
  *)
    printf 'Unknown command: %s\n' "${command_name}" >&2
    show_help >&2
    exit 2
    ;;
esac
