#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
model_dir="${1:-${project_root}/modules/gigacode-context/models/multilingual-e5-small}"
revision="761b726dd34fb83930e26aab4e9ac3899aa1fa78"
base_url="${GIGACODE_MODEL_BASE_URL:-https://huggingface.co/Xenova/multilingual-e5-small/resolve/${revision}}"
onnx_sha256="f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193"
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required" >&2
  exit 1
fi

mkdir -p "${model_dir}/onnx"

for file in \
  config.json \
  quant_config.json \
  sentencepiece.bpe.model \
  special_tokens_map.json \
  tokenizer.json \
  tokenizer_config.json
do
  curl -fL --retry 3 \
    "${base_url}/${file}" \
    -o "${model_dir}/${file}"
done

curl -fL --retry 3 \
  "${base_url}/onnx/model_quantized.onnx" \
  -o "${model_dir}/onnx/model_quantized.onnx"

if command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "${model_dir}/onnx/model_quantized.onnx" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "${model_dir}/onnx/model_quantized.onnx" | awk '{print $1}')"
else
  echo "Install shasum or sha256sum to verify the downloaded ONNX model" >&2
  exit 1
fi

if [[ "${actual_sha256}" != "${onnx_sha256}" ]]; then
  echo "ONNX SHA-256 mismatch: expected ${onnx_sha256}, got ${actual_sha256}" >&2
  exit 1
fi

echo "Model installed and verified: ${model_dir}"
