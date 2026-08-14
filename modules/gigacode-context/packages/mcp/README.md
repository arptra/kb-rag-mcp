# GigaCode Context MCP

MCP transport для локального индексатора GigaCode Context.

Сервер запускается GigaCode CLI через `stdio`. Он не обращается к API GigaCode
и не требует ключа от GigaCode или OpenAI. Эмбеддинги строятся локальной
Transformers.js/ONNX-моделью, а векторный индекс хранится в PyPI Milvus Lite.
MCP сам запускает локальный gRPC-процесс базы; Docker не используется.

Рекомендуемая установка и готовый settings-блок находятся в корневом
[`README.md`](../../README.md). Самостоятельный запуск после сборки:

```bash
npm run start:offline --workspace=@zilliz/claude-context-mcp
```

Обязательные переменные runtime:

- `EMBEDDING_PROVIDER=LocalTransformer`;
- `LOCAL_EMBEDDING_MODEL_PATH`;
- `LOCAL_EMBEDDING_DIMENSION`;
- `MILVUS_ADDRESS` с loopback-адресом.
- `MILVUS_LITE_COMMAND` и `MILVUS_LITE_DATA_DIR`.

Любой другой embedding provider или внешний адрес Milvus отклоняется до запуска
MCP-сервера.
