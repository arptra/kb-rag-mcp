#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  echo "This script must be sourced: source ./scripts/activate-venv.sh" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"

if [[ ! -x "${venv_dir}/bin/python" || ! -x "${venv_dir}/bin/uv" ]]; then
  echo "Local environment is not initialized." >&2
  echo "Run: ${project_root}/scripts/setup-venv.sh" >&2
  return 1
fi

# An alias or shell function has priority over PATH, so remove only uv overrides.
unalias uv 2>/dev/null || true
unset -f uv 2>/dev/null || true

source "${venv_dir}/bin/activate"

export KB_PROJECT_ROOT="${project_root}"
export UV_BIN="${venv_dir}/bin/uv"
export UV_CACHE_DIR="${project_root}/.uv-cache"
export UV_PYTHON_INSTALL_DIR="${project_root}/.uv-python"
export PATH="${venv_dir}/bin:${PATH}"
export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"

# Bash caches executable locations; clear that cache after changing PATH.
hash -r 2>/dev/null || true

if [[ "${KB_ACTIVATE_QUIET:-false}" != "true" ]]; then
  echo "Activated: ${VIRTUAL_ENV}"
  echo "Python:    $(command -v python)"
  echo "uv:        ${UV_BIN}"
  echo "uv cache:  ${UV_CACHE_DIR}"
fi
