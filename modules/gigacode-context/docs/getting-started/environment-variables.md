# Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `EMBEDDING_PROVIDER` | Must be `LocalTransformer` | `LocalTransformer` |
| `LOCAL_EMBEDDING_MODEL_PATH` | Absolute local model directory | required |
| `LOCAL_EMBEDDING_DIMENSION` | Embedding dimension | required (`384` for the included model) |
| `LOCAL_EMBEDDING_DTYPE` | Local ONNX dtype | `q8` |
| `LOCAL_EMBEDDING_QUERY_PREFIX` | Query prefix | empty |
| `LOCAL_EMBEDDING_DOCUMENT_PREFIX` | Indexed-document prefix | empty |
| `MILVUS_ADDRESS` | Local Milvus endpoint | required |
| `MILVUS_LITE_COMMAND` | Absolute `.venv` executable path | required |
| `MILVUS_LITE_DATA_DIR` | Persistent local database directory | required |
| `GIGACODE_CONTEXT_HOME` | Snapshot/runtime directory | project `.runtime` in generated settings |

The MCP configuration intentionally has no OpenAI or GigaCode API key.
