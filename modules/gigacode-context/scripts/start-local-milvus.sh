#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"

milvus_lite="${project_root}/.venv/bin/milvus-lite"
data_dir="${GIGACODE_MILVUS_LITE_DATA_DIR:-${project_root}/.runtime/milvus-lite}"

if [[ ! -x "${milvus_lite}" ]]; then
  echo "Milvus Lite is not installed in ${project_root}/.venv. Run scripts/setup-gigacode.sh first." >&2
  exit 1
fi

mkdir -p "${data_dir}"
exec "${milvus_lite}" server --data-dir "${data_dir}" --host 127.0.0.1 --port 19530
