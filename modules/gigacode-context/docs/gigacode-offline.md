# GigaCode Context: закрытый локальный контур

Это форк официального `zilliztech/claude-context`. Сохранены его TypeScript-ядро,
AST/tree-sitter chunking, Merkle-инкрементальная индексация, hybrid search и MCP tools.
Изменён runtime-контур:

- эмбеддинги считаются внутри процесса через Transformers.js и локальную ONNX-модель;
- удалён выбор OpenAI, VoyageAI, Gemini, OpenRouter и Ollama из MCP;
- Transformers.js запускается с `allowRemoteModels=false` и `local_files_only=true`;
- Milvus обязан быть на loopback-адресе, token-to-cloud discovery отключён;
- локальная база — PyPI `milvus-lite`, запускаемая MCP как дочерний процесс;
- дополнительный runtime guard блокирует `fetch` и TCP за пределы loopback;
- MCP подключается к GigaCode локальным `node .../dist/index.js`, без `npx`.

## Локальная модель

Скрипт `scripts/download-model.sh` скачивает tokenizer и quantized ONNX
`Xenova/multilingual-e5-small` зафиксированной ревизии
`761b726dd34fb83930e26aab4e9ac3899aa1fa78`. Размерность — 384, dtype — q8.
SHA-256 ONNX проверяется скриптом. При запуске MCP никаких загрузок модели не
выполняется. Для закрытого контура задайте `GIGACODE_MODEL_BASE_URL` с адресом
внутреннего зеркала.

Для работы требуются Node.js 20/22, npm 9+, Python 3.10+ и доступ к
разрешённым внутренним NPM/PyPI-репозиториям. Docker не используется.

## Полностью автоматический локальный запуск

```bash
./scripts/setup-gigacode.sh
```

Скрипт собирает core/MCP, поднимает Milvus, прогоняет настоящий embedding,
индексирует тестовый Spring-код, выполняет semantic search напрямую и через MCP,
а затем добавляет сервер в `~/.gigacode/settings.json`. Можно передать другой
путь к настройкам первым аргументом.

Отдельный запуск MCP без ручного набора переменных:

```bash
./scripts/run-gigacode-mcp.sh
```

## Установка через внутренние репозитории

```bash
git clone <internal-git-url> gigacode-context
cd gigacode-context
GIGACODE_MODEL_BASE_URL=https://models.company.local/Xenova/multilingual-e5-small/resolve/761b726dd34fb83930e26aab4e9ac3899aa1fa78 \
  ./scripts/download-model.sh
NPM_CONFIG_REGISTRY=https://npm.company.local/repository/npm/ \
PIP_INDEX_URL=https://pypi.company.local/simple/ \
  ./scripts/setup-gigacode.sh
```

Установщик выполняет `npm ci` только для npm workspaces `core` и `mcp`, создаёт
`.venv` и ставит версии из `requirements-milvus-lite.txt`. Модель хранится
локально вне Git.
При старте MCP автоматически запускает `.venv/bin/milvus-lite server` только на
`127.0.0.1:19530`; данные сохраняются в `.runtime/milvus-lite`. Docker, etcd и
MinIO отсутствуют. MCP дополнительно блокирует внешний TCP/fetch на уровне
процесса.

Если нужен нестандартный путь к settings, MCP можно зарегистрировать отдельно:

```bash
node ./scripts/configure-gigacode.mjs \
  --model /absolute/path/to/gigacode-context/models/multilingual-e5-small \
  --dimension 384 \
  --dtype q8 \
  --query-prefix "query: " \
  --document-prefix "passage: "
```

Если `--settings` не указан, скрипт использует `~/.gigacode/settings.json`.
Обычный `setup-gigacode.sh` уже выполняет эту регистрацию. Существующий файл
сохраняется рядом как backup, другие настройки и MCP-серверы не удаляются.

После перезапуска GigaCode доступны официальные tools:

- `index_codebase` — индексировать локальный абсолютный путь;
- `search_code` — семантический поиск по индексу;
- `get_indexing_status` — состояние и статистика индекса;
- `clear_index` — удалить индекс выбранной кодовой базы.

Пример готового блока настроек находится в `examples/gigacode/settings.json`.

## Быстрая проверка политики

```bash
node ./scripts/smoke-local-embedding.mjs
node ./scripts/smoke-index-search.mjs
node ./scripts/smoke-mcp.mjs
npm run test:core
npm run test:mcp
```

MCP завершится с ошибкой до запуска, если модель отсутствует, размерность не
задана, выбран внешний provider или `MILVUS_ADDRESS` не является loopback.
Даже если зависимость попробует сделать внешний `fetch`/TCP-вызов, runtime guard
заблокирует его.
