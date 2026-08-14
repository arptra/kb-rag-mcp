#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"

export EMBEDDING_PROVIDER=LocalTransformer
export LOCAL_EMBEDDING_MODEL_PATH="${project_root}/models/multilingual-e5-small"
export LOCAL_EMBEDDING_DIMENSION=384
export LOCAL_EMBEDDING_DTYPE=q8
export LOCAL_EMBEDDING_MAX_TOKENS=2048
export LOCAL_EMBEDDING_QUERY_PREFIX="query: "
export LOCAL_EMBEDDING_DOCUMENT_PREFIX="passage: "
export EMBEDDING_BATCH_SIZE=32
export MILVUS_ADDRESS=127.0.0.1:19530
export MILVUS_LITE_COMMAND="${project_root}/.venv/bin/milvus-lite"
export MILVUS_LITE_DATA_DIR="${project_root}/.runtime/milvus-lite"
export GIGACODE_CONTEXT_HOME="${project_root}/.runtime"
export GIGACODE_CONTEXT_BACKGROUND_SYNC=true

exec node "${project_root}/packages/mcp/dist/index.js"
