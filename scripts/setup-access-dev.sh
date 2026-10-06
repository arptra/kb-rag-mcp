#!/usr/bin/env bash
# Opt-in local runtime setup: never changes system Python or installs with sudo.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"
bootstrap_dir="${project_root}/.cache/access-bootstrap"
cd "${project_root}"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  printf '%s\n' 'Usage: bash scripts/setup-access-dev.sh' \
    'Installs a private Python 3.12 runtime and project dependencies; may download packages.'
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo 'Unknown argument; use --help.' >&2
  exit 2
fi

fail_existing() {
  echo 'Existing .venv is broken, linked or not Python 3.12; it was not overwritten.' >&2
  echo 'Move it aside manually and rerun setup. Do not copy virtualenvs between operating systems.' >&2
  exit 1
}

python_version() {
  "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

export UV_CACHE_DIR="${project_root}/.uv-cache"
export UV_PYTHON_INSTALL_DIR="${project_root}/.uv-python"
export PIP_CACHE_DIR="${project_root}/.cache/pip"
for local_path in "${project_root}/.cache" "${bootstrap_dir}" \
  "${UV_CACHE_DIR}" "${UV_PYTHON_INSTALL_DIR}" "${PIP_CACHE_DIR}"; do
  if [[ -L "${local_path}" ]]; then
    echo "Refusing a symlink setup directory: ${local_path}" >&2
    exit 1
  fi
done

if [[ -e "${venv_dir}" || -L "${venv_dir}" ]]; then
  [[ ! -L "${venv_dir}" && -d "${venv_dir}" && -x "${venv_dir}/bin/python" ]] || fail_existing
  version="$(python_version "${venv_dir}/bin/python")" || fail_existing
  [[ "${version}" == "3.12" ]] || fail_existing
  bash "${script_dir}/setup-pip.sh" --no-dev
  exit 0
fi

python_candidate="${PYTHON_BIN:-python3.12}"
if command -v "${python_candidate}" >/dev/null 2>&1; then
  version="$(python_version "${python_candidate}")" || version=""
  if [[ "${version}" == "3.12" ]]; then
    PYTHON_BIN="${python_candidate}" bash "${script_dir}/setup-pip.sh" --no-dev
    exit 0
  fi
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  echo 'PYTHON_BIN must name a working Python 3.12 executable. Unset it to use automatic setup.' >&2
  exit 1
fi

uv_command=()
if command -v uv >/dev/null 2>&1; then
  uv_command=(uv)
elif [[ -x "${bootstrap_dir}/bin/python" ]] && \
  "${bootstrap_dir}/bin/python" -m uv --version >/dev/null 2>&1; then
  uv_command=("${bootstrap_dir}/bin/python" -m uv)
elif command -v python3 >/dev/null 2>&1 && python3 -m uv --version >/dev/null 2>&1; then
  uv_command=(python3 -m uv)
else
  if ! command -v python3 >/dev/null 2>&1; then
    echo 'Python 3 or uv is required. On Debian: sudo apt install python3 python3-venv' >&2
    exit 1
  fi
  if [[ -e "${bootstrap_dir}" ]]; then
    if [[ ! -d "${bootstrap_dir}" || ! -x "${bootstrap_dir}/bin/python" ]]; then
      echo 'Existing .cache/access-bootstrap is incomplete; move it aside manually and retry.' >&2
      exit 1
    fi
  elif ! python3 -m venv "${bootstrap_dir}"; then
    echo 'Cannot create the isolated uv bootstrap. On Debian: sudo apt install python3-venv' >&2
    echo 'Then move any incomplete .cache/access-bootstrap aside and rerun setup.' >&2
    exit 1
  fi
  "${bootstrap_dir}/bin/python" -m pip install --upgrade uv
  uv_command=("${bootstrap_dir}/bin/python" -m uv)
fi

"${uv_command[@]}" venv --python 3.12 "${venv_dir}"
version="$(python_version "${venv_dir}/bin/python")" || fail_existing
[[ "${version}" == "3.12" ]] || fail_existing
"${uv_command[@]}" pip install --python "${venv_dir}/bin/python" "${project_root}"
printf 'Local Python 3.12 runtime is ready: %s\n' "${venv_dir}"
