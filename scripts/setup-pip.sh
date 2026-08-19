#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_dir="${project_root}/.venv"
python_bin="${PYTHON_BIN:-python3.12}"
with_dev=true
with_semantic=false

show_help() {
  cat <<'EOF'
Usage: ./scripts/setup-pip.sh [--no-dev] [--semantic]

Create/update .venv using pip only. The global or corporate uv installation is not used.

Options:
  --no-dev    Install runtime dependencies only
  --semantic  Also install the optional sentence-transformers dependencies
EOF
}

while [[ $# -gt 0 ]]; do
  case "${1}" in
    --no-dev)
      with_dev=false
      ;;
    --semantic)
      with_semantic=true
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "${1}" >&2
      show_help >&2
      exit 2
      ;;
  esac
  shift
done

cd "${project_root}"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  if ! command -v "${python_bin}" >/dev/null 2>&1; then
    printf 'Python 3.12 executable not found: %s\n' "${python_bin}" >&2
    printf 'Set PYTHON_BIN=/absolute/path/to/python3.12 and retry.\n' >&2
    exit 1
  fi
  "${python_bin}" -m venv "${venv_dir}"
fi

python_version="$("${venv_dir}/bin/python" -c \
  'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${python_version}" != "3.12" ]]; then
  printf 'Existing .venv uses Python %s; Python 3.12 is required.\n' "${python_version}" >&2
  printf 'Move the existing .venv aside and run this script again.\n' >&2
  exit 1
fi

if ! "${venv_dir}/bin/python" -m pip --version >/dev/null 2>&1; then
  "${venv_dir}/bin/python" -m ensurepip --upgrade
fi

install_target="${project_root}"
if [[ "${with_semantic}" == true ]]; then
  install_target="${project_root}[semantic]"
fi

"${venv_dir}/bin/python" -m pip install --upgrade "${install_target}"

if [[ "${with_dev}" == true ]]; then
  "${venv_dir}/bin/python" -m pip install \
    'mypy>=1.17,<2' \
    'pytest>=8.4,<10' \
    'pytest-asyncio>=1.1,<2' \
    'ruff>=0.12,<1'
fi

printf '\nLocal pip environment is ready: %s\n' "${venv_dir}"
printf 'Activate it with: source %s/scripts/activate-venv.sh\n' "${project_root}"
