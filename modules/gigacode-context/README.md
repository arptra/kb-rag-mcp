# GigaCode Context

Локальный MCP-сервер семантической индексации кода на базе официального
`zilliztech/claude-context`.

Рабочий runtime не использует OpenAI, Ollama, VoyageAI, Gemini или другие
внешние embedding API. GigaCode CLI вызывает MCP по `stdio`, локальная
Transformers.js/ONNX-модель строит эмбеддинги внутри Node.js-процесса, а векторы
хранятся в локальном Milvus.

## Что требуется

- Node.js 20 или 22;
- npm 9+;
- Python 3.10+ с `venv`;
- доступ из корпоративного контура к разрешённым NPM и PyPI-репозиториям;
- локально скачанная ONNX-модель в каталоге `models/multilingual-e5-small`.

Никакой `OPENAI_API_KEY` и никакой API-ключ GigaCode для MCP не нужны.
Авторизация самого GigaCode CLI остаётся его внутренней настройкой и не
передаётся индексатору.

## Установка из репозиториев зависимостей

```bash
git clone <internal-git-url> gigacode-context
cd gigacode-context

GIGACODE_MODEL_BASE_URL=https://models.company.local/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78 \
  ./scripts/download-model.sh

NPM_CONFIG_REGISTRY=https://npm.company.local/repository/npm/ \
PIP_INDEX_URL=https://pypi.company.local/simple/ \
  ./scripts/setup-gigacode.sh
```

`setup-gigacode.sh` выполняет `npm ci` по lockfile, собирает только
`packages/core` и `packages/mcp`, создаёт локальную `.venv`, устанавливает
`milvus-lite` из PyPI, прогоняет реальные embedding/index/search/MCP проверки и
добавляет `gigacode-context` в `~/.gigacode/settings.json`.

Docker, etcd и MinIO не используются. При запуске MCP сам поднимает
`milvus-lite server` на `127.0.0.1`, а при завершении останавливает принадлежащий
ему процесс. База сохраняется в `.runtime/milvus-lite`.

Можно передать путь к настройкам GigaCode первым аргументом:

```bash
./scripts/setup-gigacode.sh /absolute/path/to/settings.json
```

## Настройка GigaCode

Установщик генерирует блок следующего вида:

```json
{
  "mcpServers": {
    "gigacode-context": {
      "command": "/absolute/path/to/node",
      "args": ["/absolute/path/to/packages/mcp/dist/index.js"],
      "env": {
        "EMBEDDING_PROVIDER": "LocalTransformer",
        "LOCAL_EMBEDDING_MODEL_PATH": "/absolute/path/to/models/multilingual-e5-small",
        "LOCAL_EMBEDDING_DIMENSION": "384",
        "LOCAL_EMBEDDING_DTYPE": "q8",
        "LOCAL_EMBEDDING_QUERY_PREFIX": "query: ",
        "LOCAL_EMBEDDING_DOCUMENT_PREFIX": "passage: ",
        "MILVUS_ADDRESS": "127.0.0.1:19530",
        "MILVUS_LITE_COMMAND": "/absolute/path/to/.venv/bin/milvus-lite",
        "MILVUS_LITE_DATA_DIR": "/absolute/path/to/.runtime/milvus-lite"
      }
    }
  }
}
```

Существующий settings-файл сохраняется как backup, остальные настройки и MCP
серверы не удаляются.

## MCP tools

- `index_codebase` — индексирует абсолютный локальный путь к репозиторию;
- `search_code` — выполняет семантический поиск;
- `get_indexing_status` — возвращает статус и статистику индекса;
- `clear_index` — удаляет индекс выбранного репозитория.

Подробности runtime и сетевых ограничений: [docs/gigacode-offline.md](docs/gigacode-offline.md).
