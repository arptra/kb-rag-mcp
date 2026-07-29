#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"
python_bin="${PYTHON_BIN:-python3.12}"

cd "${project_root}"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  if ! command -v "${python_bin}" >/dev/null 2>&1; then
    echo "Python 3.12 executable not found: ${python_bin}" >&2
    echo "Install Python 3.12 or run with PYTHON_BIN=/absolute/path/to/python3.12" >&2
    exit 1
  fi
  "${python_bin}" -m venv "${venv_dir}"
fi

python_version="$("${venv_dir}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != "3.12" ]]; then
  echo "Existing .venv uses Python ${python_version}; Python 3.12 is required." >&2
  echo "Move the existing .venv aside and run this script again." >&2
  exit 1
fi

if [[ ! -x "${venv_dir}/bin/uv" ]]; then
  if ! "${venv_dir}/bin/python" -m pip --version >/dev/null 2>&1; then
    "${venv_dir}/bin/python" -m ensurepip --upgrade
  fi
  "${venv_dir}/bin/python" -m pip install --upgrade pip
  "${venv_dir}/bin/python" -m pip install uv
fi

export VIRTUAL_ENV="${venv_dir}"
export UV_CACHE_DIR="${project_root}/.uv-cache"
export UV_PYTHON_INSTALL_DIR="${project_root}/.uv-python"
export PATH="${venv_dir}/bin:${PATH}"
export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"

"${venv_dir}/bin/uv" sync --active --locked --inexact "$@"

echo
echo "Local environment is ready: ${venv_dir}"
echo "Activate it in the current shell:"
echo "  source ${project_root}/scripts/activate-venv.sh"
