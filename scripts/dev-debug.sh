#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
python_runtime="${project_root}/.venv/bin/python"

if [[ ! -x "${python_runtime}" ]]; then
  echo "Нет .venv/bin/python. Установите dev-зависимости через ./scripts/dev.sh install." >&2
  exit 2
fi

# No dependency installation, config mutation or background daemon startup here.
export PYTHONPATH="${project_root}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PYTHONDONTWRITEBYTECODE=1
cd "${project_root}"
exec "${python_runtime}" -m corporate_kb.dev_debug "$@"
