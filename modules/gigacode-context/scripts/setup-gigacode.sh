#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
settings_path="${1:-${GIGACODE_SETTINGS_PATH:-${HOME}/.gigacode/settings.json}}"
model_path="${project_root}/models/multilingual-e5-small"

for required_command in node npm; do
  if ! command -v "${required_command}" >/dev/null 2>&1; then
    echo "Required command is not installed: ${required_command}" >&2
    exit 1
  fi
done

python_command="${GIGACODE_PYTHON:-}"
if [[ -z "${python_command}" ]]; then
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "${candidate}" >/dev/null 2>&1; then
      python_command="${candidate}"
      break
    fi
  done
fi
if [[ -z "${python_command}" ]]; then
  echo "Python 3.10+ is required for Milvus Lite" >&2
  exit 1
fi
if ! "${python_command}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10+ is required; found $("${python_command}" --version)" >&2
  exit 1
fi

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if (( node_major < 20 || node_major >= 24 )); then
  echo "Node.js 20 or 22 is required; found $(node --version)" >&2
  exit 1
fi

if [[ ! -f "${model_path}/onnx/model_quantized.onnx" ]]; then
  echo "Repository ONNX model is missing: ${model_path}" >&2
  echo "Download it first with: ./scripts/download-model.sh" >&2
  exit 1
fi

cd "${project_root}"
npm ci
if [[ ! -x "${project_root}/.venv/bin/python" ]]; then
  "${python_command}" -m venv "${project_root}/.venv"
fi
"${project_root}/.venv/bin/python" -m pip install -r requirements-milvus-lite.txt
npm run build
node scripts/smoke-local-embedding.mjs
node scripts/smoke-mcp.mjs
node scripts/configure-gigacode.mjs \
  --settings "${settings_path}" \
  --model "${model_path}" \
  --dimension 384 \
  --dtype q8 \
  --query-prefix "query: " \
  --document-prefix "passage: "

echo "GigaCode Context is ready. Restart GigaCode CLI to load the MCP server."
