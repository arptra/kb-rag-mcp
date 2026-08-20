#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
dashboard_dir="${project_root}/apps/dashboard"

cd "${project_root}"

if [[ ! -d "${dashboard_dir}/node_modules" ]]; then
  echo "Frontend dependencies are missing; running npm ci..."
  npm --prefix "${dashboard_dir}" ci
fi

exec npm --prefix "${dashboard_dir}" run dev -- "$@"
