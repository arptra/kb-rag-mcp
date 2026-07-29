# Локальная корпоративная база знаний для Qwen Code

Это локальный MVP корпоративного RAG: документы индексируются Python-процессом, embeddings
сохраняются в проверяемый файловый кэш, а при поиске целиком находятся в RAM. Qwen Code остаётся
единственной генеративной моделью и получает найденные фрагменты через read-only MCP tools.
MCP-сервер не формулирует финальные ответы, не исполняет shell-команды и не изменяет документы.

Проект рассчитан на Python 3.12, `uv` и официальный MCP Python SDK v2. Зафиксированная версия SDK
указана в `uv.lock`; сторонний пакет `fastmcp` не используется.

## Архитектура

```text
Confluence export
       ↓
knowledge/*.md, *.html, *.txt
       ↓
loader + normalizer + structural chunker
       ↓
Qwen3 Embedding (или тестовый hash provider)
       ↓
NumPy matrix in RAM
       ↓
MCP stdio
       ↓
Qwen Code CLI
```

`DocumentLoader` безопасно обходит только `KB_KNOWLEDGE_DIR`, нормализует Markdown/TXT и переводит
экспортированный HTML в Markdown-подобный текст. `StructuralChunker` сохраняет путь заголовков,
списки, таблицы и code fences. `KnowledgeService` координирует кэш и работает только через
интерфейс `KnowledgeStore`; MCP-слой не знает о NumPy.

В RAM находятся документы, чанки, отображение `chunk_id -> index` и нормализованная NumPy-матрица
`[chunk_count, embedding_dimension]`. Cosine similarity считается как `matrix @ query_vector`.

На диске в `.cache/kb/` находятся только:

- `manifest.json` — версии схемы, идентичность модели, chunking config и knowledge hash;
- `documents.json` — нормализованные документы и metadata;
- `chunks.json` — чанки без отдельной копии embedding;
- `embeddings.npy` — матрица без pickle.

Это не Vector DB: нет отдельного сервиса, индекса ANN, SQL или сетевого API. Диск используется для
ускорения старта, но поиск выполняется полным cosine scan по NumPy-матрице в памяти.

## Первый запуск

Убедитесь, что доступен Python 3.12. Глобальный `uv` не нужен: setup-скрипт создаст `.venv`,
установит `uv` непосредственно в него и синхронизирует зависимости из lock-файла:

```bash
./scripts/setup-venv.sh
source ./scripts/activate-venv.sh
```

После активации `command -v uv` должен указывать на `.venv/bin/uv`. Скрипт удаляет действующие
shell alias/function с именем `uv`, ставит `.venv/bin` первым в `PATH` и экспортирует `UV_BIN`:

```bash
command -v python
command -v uv
echo "$UV_BIN"
```

Сначала проверьте всю инфраструктуру без сети и без скачивания Qwen-модели:

```bash
KB_EMBEDDING_PROVIDER=hash \
  uv run kb index --force

KB_EMBEDDING_PROVIDER=hash \
  uv run kb search "Какой сервис владеет дневными лимитами?"
```

Hash provider — только детерминированная проверка цепочки загрузка → chunking → cache → cosine
search → MCP. Это не production semantic embedding.

После smoke test постройте реальный индекс:

```bash
uv run kb index --force
uv run kb search "Какой сервис владеет дневными лимитами?"
```

Первый реальный запуск скачает `Qwen/Qwen3-Embedding-0.6B` с Hugging Face. По умолчанию выбирается
CUDA, затем MPS, затем CPU. Устройство и размер батча можно задать через `.env` или `KB_*` variables.

## CLI

```bash
uv run kb index
uv run kb index --force
uv run kb search "Как рассчитывается дневной лимит?" --top-k 5
uv run kb search "Как рассчитывается дневной лимит?" --service limits-service
uv run kb search "Как рассчитывается дневной лимит?" --document-type business_rule
uv run kb documents
uv run kb stats
uv run kb eval
uv run kb eval --top-k 5
```

У `search`, `documents`, `stats` и `eval` есть `--json`. В этом режиме stdout содержит только JSON,
а логи остаются в stderr.

Если кэша нет или он несовместим, обычный поиск при `KB_AUTO_INDEX=false` завершится практичным
сообщением `Run: uv run kb index`. Это предотвращает неожиданное скачивание большой модели во время
MCP discovery.

## Подключение к Qwen Code

Скопируйте `examples/qwen-settings.example.json` в `.qwen/settings.json` проекта и замените все
`/ABSOLUTE/PATH/...` реальными абсолютными путями. Не рассчитывайте на раскрытие `${PROJECT_ROOT}`
в JSON. Пример запускает `scripts/start-mcp.sh`: этот wrapper сам активирует локальный `.venv` и
использует только `.venv/bin/uv`, поэтому глобальный `PATH` Qwen-процесса не имеет значения.

Альтернатива через CLI (выполняйте из корня этого репозитория, подставив абсолютные пути):

```bash
qwen mcp add \
  --scope project \
  --timeout 120000 \
  --include-tools kb_search,kb_get_document,kb_list_documents,kb_stats \
  -e KB_KNOWLEDGE_DIR=/absolute/path/to/repository/knowledge \
  -e KB_CACHE_DIR=/absolute/path/to/repository/.cache/kb \
  -e KB_EMBEDDING_PROVIDER=sentence_transformers \
  -e KB_AUTO_INDEX=false \
  local-corporate-kb \
  /absolute/path/to/repository/scripts/start-mcp.sh
```

`stdio` — транспорт по умолчанию, поэтому `--transport http` здесь не нужен. Синтаксис команды
сверен с [официальной документацией Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/),
но в среде разработки этого репозитория `qwen` не был установлен, и команда локально не выполнялась.
JSON-конфигурация также задаёт `cwd`, `trust: false` и allowlist из четырёх tools.

Проверка подключения:

```text
qwen
/mcp
```

Тестовый запрос:

```text
Используй corporate knowledge MCP.
Найди, какой сервис владеет дневными лимитами,
объясни правило и обязательно укажи использованные источники.
```

Сервер предоставляет только:

- `kb_search` — поиск с `top_k`, `min_score` и metadata filters;
- `kb_get_document` — полный нормализованный документ по `document_id`;
- `kb_list_documents` — metadata документов без embeddings;
- `kb_stats` — состояние индекса и абсолютные пути.

Ручной запуск stdio server:

```bash
uv run kb-mcp
```

stdout зарезервирован для MCP-протокола; все application logs направляются в stderr.

## Добавление Confluence-страницы

Экспортируйте страницу в HTML либо сохраните её как Markdown и положите внутрь `knowledge/`.
Поддерживаются `.md`, `.markdown`, `.html`, `.htm`, `.txt`. Скрытые каталоги, `.git`, `.cache`,
`__pycache__`, `node_modules`, бинарные и неподдерживаемые файлы игнорируются. После изменения
перестройте индекс; при обычном запуске несовпадение `knowledge_hash` также инвалидирует кэш.

Пример front matter:

```yaml
---
document_type: service
service: limits-service
domain: payments
status: current
authority: confluence
authority_priority: 80
owner: limits-team
source_id: "confluence-12345"
source_url: "https://confluence.example.com/pages/12345"
last_reviewed: "2026-07-20"
custom_field: "неизвестные поля тоже сохраняются"
---

# Limits Service
```

Без front matter заголовок берётся из первого H1 или имени файла, `source_id` — из относительного
пути, `status=current`, `authority=local_file`, `authority_priority=50`.

## Кэш и конфигурация

Пересобрать кэш:

```bash
uv run kb index --force
```

Полностью удалить его можно командой `rm -rf .cache/kb`, после чего снова выполнить `kb index`.
Запись каждого файла атомарна, а `manifest.json` заменяется последним. Повреждение JSON/NumPy,
несовпадение схемы, модели, dimension, query instruction, chunking config или knowledge hash приводит
к понятной invalidation, а не к неясной NumPy-ошибке.

Все параметры перечислены в `.env.example`. Основные:

- `KB_EMBEDDING_PROVIDER=sentence_transformers|hash`;
- `KB_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B`;
- `KB_EMBEDDING_DEVICE=auto|cpu|mps|cuda`;
- `KB_EMBEDDING_DIMENSION=1024`;
- `KB_CHUNK_SIZE_TOKENS=700`, `KB_CHUNK_HARD_MAX_TOKENS=900`,
  `KB_CHUNK_OVERLAP_TOKENS=80`;
- `KB_AUTO_INDEX=false`.

Относительные пути разрешаются относительно текущего project working directory; `kb stats`
показывает итоговые абсолютные пути.

## Проверки

```bash
uv run ruff check .
uv run mypy src
KB_EMBEDDING_PROVIDER=hash uv run pytest -q
./scripts/dev.sh check
```

Обычный shell-скрипт `scripts/dev.sh` также объединяет повседневные команды:

```bash
./scripts/dev.sh install
./scripts/dev.sh test
./scripts/dev.sh lint
./scripts/dev.sh typecheck
./scripts/dev.sh index-hash
./scripts/dev.sh search-hash
./scripts/dev.sh index
./scripts/dev.sh eval
./scripts/dev.sh serve
```

Тесты всегда инжектируют hash provider и не требуют интернета, Hugging Face, GPU, Qwen Code, Docker
или внешней БД. MCP integration test использует официальный v2 `Client` напрямую с объектом
`MCPServer` и in-memory transport — сетевой порт не поднимается.

## Ограничения MVP и развитие

- Полный brute-force cosine scan подходит для небольшой локальной базы, но не для миллионов чанков.
- Любое изменение документа полностью перестраивает индекс; per-document incremental rebuild нет.
- Нет Confluence REST API, OAuth, фоновой синхронизации и HTML-адаптеров под каждый вариант экспорта.
- Нет reranker, hybrid/BM25 retrieval и отдельной оценки authority при ранжировании.
- Точный token counter реальной модели не используется для предварительного chunking: интерфейс
  `TokenCounter` отделён, поэтому его можно подключить без связи chunker с SentenceTransformer.
- MCP работает только через локальный stdio subprocess.

Для перехода на настоящую Vector DB нужно реализовать `PostgresKnowledgeStore` или
`QdrantKnowledgeStore` с тем же контрактом `KnowledgeStore`, выбрать реализацию при сборке
`KnowledgeService` и сохранить API сервиса/MCP без изменений. Следующим этапом стоит добавить
инкрементальный cache manifest, batch upsert, hybrid retrieval и production evaluation corpus.
