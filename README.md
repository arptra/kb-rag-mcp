# Локальная корпоративная база знаний для Qwen Code

Это локальный MVP корпоративного RAG: документы индексируются Python-процессом, embeddings
сохраняются в проверяемый файловый кэш, а при поиске целиком находятся в RAM. Qwen Code остаётся
единственной генеративной моделью и получает найденные фрагменты через read-only MCP tools по
локальному `stdio` или удалённому Streamable HTTP. MCP-сервер не формулирует финальные ответы,
не исполняет shell-команды и не изменяет документы.

Проект рассчитан на Python 3.12 и standalone `FastMCP==3.4.4`. Запуск после установки не зависит
от `uv`: все runtime-скрипты вызывают Python из `.venv` напрямую. Для разработки доступна
воспроизводимая установка через `uv.lock`, а для корпоративных машин — отдельная установка через
обычный `pip`.

Отдельные пошаговые инструкции:

- [текущее устройство module-aware анализа repository, lifecycle jobs и SSOT workflow](README.repository-analysis.md);
- [evidence-backed граф Java/Spring-репозиториев для GigaCode](README.gigacode-graph.md);
- [подключение Qwen на клиентском компьютере](README.client.md);
- [развёртывание базы и API на удалённом сервере](README.server.md);
- [быстрый запуск RAG с уменьшенным контекстом](README.low-context.md);
- [отдельный общий SSOT-индекс всех сервисов](README.ssot.md).

## Архитектура

```text
Confluence export
       ↓
knowledge/*.md, *.html, *.txt
       ↓
loader + normalizer + structural chunker
       ↓
local feature hashing (по умолчанию) или локальная embedding-модель
       ↓
NumPy matrix in RAM
       ↓
MCP stdio / Streamable HTTP
       ↓
Qwen Code CLI
```

В удалённом режиме тот же индекс один раз загружается в память серверного процесса, после чего к
нему одновременно подключаются Qwen Code CLI с разных машин:

```text
Qwen CLI ─┐
Qwen CLI ─┼─ HTTP(S), Bearer optional ─ MCP Streamable HTTP ─ in-memory index
Qwen CLI ─┘
```

`DocumentLoader` безопасно обходит только `KB_KNOWLEDGE_DIR`, нормализует Markdown/TXT и переводит
экспортированный HTML в Markdown-подобный текст. `StructuralChunker` сохраняет путь заголовков,
списки, таблицы и code fences. `KnowledgeService` координирует кэш и работает только через
интерфейс `KnowledgeStore`; MCP-слой не знает о NumPy.

В RAM находятся документы, чанки, отображение `chunk_id -> index` и нормализованная NumPy-матрица
`[chunk_count, embedding_dimension]`. Cosine similarity считается как `matrix @ query_vector`.

### Экономия контекста Qwen

Поиск не передаёт модели все найденные тексты. Сервер сначала находит до 12 кандидатов внутри
индекса, затем отдаёт максимум 3 наиболее релевантные выдержки из разных документов: до 260
условных токенов на выдержку и до 1000 на один ответ инструмента. Выдержка выбирается по словам
вопроса и сохраняет ссылку на источник. Это ограничивает расход контекста, даже если в базе десятки
тысяч страниц.

Если выбранный результат требует деталей, Qwen вызывает `kb_get_chunk` с `chunk_id`; полный текст
не загружается автоматически. `kb_get_document` также возвращает ограниченный извлекаемый фрагмент.
Лимиты настраиваются через `KB_SEARCH_*` и `KB_DOCUMENT_CONTEXT_TOKENS` в `.env.example`.

Защищённый `kb_run_context_benchmark` сравнивает прежние `top-5` полных чанков с текущими `top-3`
выдержками: Hit@K, оценку токенов, точные JSON bytes, процент сжатия и latency. Инструмент требует
отдельный `KB_BENCHMARK_PASSWORD`, который Qwen запрашивает перед каждым вызовом.

В HTTP-режиме по `/admin` доступна React/TypeScript-панель управления. Через неё создаются
отдельные RAG-индексы и MCP search-tools, tools привязываются к одному или нескольким индексам,
а Git-репозитории подключаются по URL и ref. Сервер сам обновляет управляемый checkout, находит все
каталоги `openspec`, переносит поддерживаемые документы в выбранный индекс, перестраивает embeddings
и обновляет evidence-backed граф системы. Module-aware слой читает Maven/Gradle descriptors,
сохраняет пустые modules, а `tree-sitter-java` и `tree-sitter-kotlin` индексируют Java/Kotlin без
запуска build. Layout обходит checkout один раз, библиотечные подмодули прикрепляет к service
boundary, а неизменившиеся services берёт из `.cache/kb/module-analysis/`.
Отдельный модуль `service_map` без LLM и SSOT извлекает точки входа, исходящие HTTP/Kafka-интерфейсы
и предполагаемые межсервисные зависимости. Карта хранится в `.cache/kb/service_map.json` и доступна через
`GET /admin/api/service-map`; краткая статистика — через `GET /admin/api/service-map/overview`.
Если `openspec` отсутствует, импорт всё равно завершается: сервис попадает в карту, а в RAG для
этого источника добавляется 0 документов.
Git и source-analysis выполняются вне HTTP request: активную или ожидающую операцию можно отменить
кнопкой в очереди, Git завершается вместе с дочерними процессами, а анализ исходников работает в
отдельном disposable-процессе с жёстким лимитом `KB_REPOSITORY_ANALYSIS_TIMEOUT_SECONDS` (600 секунд
по умолчанию). После layout-прохода найденные repository/service nodes сразу публикуются в dashboard,
а длительный source scan обновляет checkpoint примерно раз в пять секунд. Если лимит достигнут или
worker аварийно завершился, последний checkpoint остаётся доступен как частичный граф вместо сброса
всего результата. Dashboard опрашивает состояние последовательно раз в секунду, пока есть активная
задача, и не создаёт зависающих параллельных запросов. Пересборка RAG также запускается в отдельном
процессе, публикует cache только после успеха и ограничена `KB_INDEX_BUILD_TIMEOUT_SECONDS` (10 минут
по умолчанию), поэтому её можно немедленно отменить без повреждения текущего serving index.
Карточка каждого индекса открывает отдельный экран содержимого. Список документов читается из
активного serving-index постранично по 50 записей и поддерживает поиск по названию и пути, поэтому
10 000+ файлов не отправляются в браузер одним ответом. Клик по документу открывает его полный
нормализованный serving-текст, источник, размер и metadata без доступа к произвольному пути на
диске. На этом же экране можно выбрать или
перетащить до 50 Markdown, TXT, HTML, JSON, YAML, CSV, XML, properties и других текстовых файлов.
Они безопасно сохраняются в `uploads/` только выбранного индекса, после чего одна фоновая
переиндексация запускается автоматически. Суммарный размер одной загрузки ограничен
`KB_ADMIN_MAX_UPLOAD_BYTES`; бинарные данные, скрытые и выходящие за knowledge-root пути отклоняются.
Отдельная страница графа показывает найденные сервисы, вызовы, события, таблицы и бизнес-правила.
Repository и производные сервисы можно удалять из dashboard, а любой сервис — повторно
анализировать. Каждая job сохраняет полный журнал и traceback в `.cache/kb/job-logs/`. Каждый
успешный source analysis архивируется в `.cache/kb/analysis/runs/`; с карточки сервиса можно
скачать пакет с analysis JSON и [`build-service-ssot`](skills/build-service-ssot/SKILL.md), затем
загрузить проверенный Markdown SSOT в выбранный RAG-индекс.
Для агентского режима кнопка **«Подготовить SSOT-контекст»** или MCP-tool
`kb_generate_system_ssot` запускает свежий source analysis и открывает сессию чтения исходников.
Сервер не содержит LLM и не требует URL модели: SSOT пишет нейронка, которая вызвала MCP с
клиентского компьютера. Локальный proxy сначала сохраняет Markdown во временный каталог клиента,
затем загружает его в `ssot/generated/<service-id>.md` выбранного индекса и запускает обновление
RAG. Полный action-flow описан в
[документации анализа](README.repository-analysis.md#агентский-системный-ssot-без-llm-на-сервере).
Во вкладке dashboard **«Операции и логи»** открытый журнал активной job обновляется раз в секунду:
он показывает layout каждого repository, найденные modules, число Java/Kotlin-файлов, cache
hit/miss, размер и отдельные фазы чтения layout-cache (`stat`, `read`, `JSON parse`, `hydrate`),
длительность этапов и текущий этап парсинга, линковку зависимостей и полный traceback или системный
signal при аварии worker. Supervisor пишет heartbeat каждые 5 секунд с `worker_pid`, общим временем,
`silent_for`, размером progress-log и последней операцией. Если worker не сообщил о прогрессе 10
секунд, на Linux/macOS supervisor автоматически запрашивает stack dump: строки `Worker stack | ...`
показывают конкретный Python-файл, функцию и строку, на которой остановился процесс. Повторный dump
запрашивается раз в 30 секунд, пока worker молчит.

На диске в `.cache/kb/` находятся только:

- `manifest.json` — версии схемы, идентичность модели, chunking config и knowledge hash;
- `documents.json` — нормализованные документы и metadata;
- `chunks.json` — чанки без отдельной копии embedding;
- `embeddings.npy` — матрица без pickle.

Это не Vector DB: нет отдельного сервиса хранения, индекса ANN или SQL. Почти все MCP-tools
read-only; `kb_generate_system_ssot` управляет source-сессией, а локальный
`kb_save_and_upload_ssot` сохраняет созданный клиентской моделью документ и инициирует перестроение
выбранного индекса. Поиск выполняется полным cosine scan по NumPy-матрице в памяти, а диск
используется для ускорения старта.

## Первый запуск

### Локальный dashboard: две отдельные команды

Backend и frontend запускаются независимо из корня repository:

```bash
# Терминал 1 — Python API, RAG и MCP на 127.0.0.1:8000
./scripts/start-backend.sh

# Терминал 2 — React/Vite на 127.0.0.1:5173
./scripts/start-frontend.sh
```

Откройте `http://127.0.0.1:5173/admin/`. При первом запуске каждый скрипт сам установит недостающие
dependencies своей части. Подробности и production-режим описаны в разделе
[«Разработка React-панели»](#разработка-react-панели).

### Подключение сотрудника к удалённой базе

RAG, индекс и документы находятся только на сервере. Для старых версий Qwen сотруднику
передаются [`clients/corporate_kb_stdio_proxy.py`](clients/corporate_kb_stdio_proxy.py) и
[`clients/requirements.txt`](clients/requirements.txt). В отдельном клиентском `venv` через обычный
`pip` устанавливается `FastMCP==3.4.4`. Qwen запускает Python из этого `venv` как stdio MCP, а процесс ходит к удалённому
серверу как прозрачный Streamable HTTP MCP-клиент. На клиенте нет прошитого списка tools: при
каждом запуске он выполняет удалённый `tools/list` и зеркалирует новые имена, схемы, annotations и
вызовы. `uv`, Node.js, `npx`, `mcp-remote`, Nginx, копия серверного проекта, документы и индекс на
клиенте не нужны.

Готовый settings находится в
[`examples/qwen-venv-stdio-settings.example.json`](examples/qwen-venv-stdio-settings.example.json), а полная
инструкция для сотрудника — в [`README.client.md`](README.client.md).

Новые версии Qwen также могут подключаться к `/mcp` напряму через Streamable HTTP; скрипт
`install.sh` оставлен как опциональный способ для таких клиентов.
Скопируйте из неё `mcpServers` в `~/.qwen/settings.json` и замените placeholder:

- `REPLACE_WITH_CLIENT_DIR` — каталог с клиентским `venv` и `.py`-файлом;
- `REPLACE_WITH_SERVER_IP_OR_DOMAIN` — адрес удалённого сервера;

Qwen запускает этот файл как локальный MCP по `stdio` через абсолютный путь к `venv/bin/python`.
Локальный MCP обращается к общему RAG-серверу через Streamable HTTP endpoint `/mcp`.

### Установка серверной части

Убедитесь, что доступен Python 3.12. На корпоративной машине рекомендуется pip-вариант: он не
читает `uv.lock`, не запускает `uv` и не зависит от установленной в системе версии `uv`.
Hugging Face, PyTorch и `sentence-transformers` в базовую установку не входят:

```bash
./scripts/setup-pip.sh
source ./scripts/activate-venv.sh
```

Для разработки с точными версиями из lock-файла остаётся вариант:

```bash
./scripts/setup-venv.sh
source ./scripts/activate-venv.sh
```

Оба варианта создают одинаковую `.venv`; runtime-команды используют только `.venv/bin/python`.

Полностью локальный режим по умолчанию использует hash provider и не требует модели или сети:

```bash
./scripts/dev.sh index-hash
./scripts/dev.sh search-hash
```

Hash provider строит локальные lexical vectors из слов и символьных триграмм. Он пригоден для
полностью автономного поиска по совпадающей терминологии, но не понимает смысл и синонимы так же
хорошо, как semantic embedding model.

Для качественного semantic search сначала положите заранее полученные и одобренные model files в
локальный каталог. Этот проект не скачивает их. Например:

```bash
models/Qwen3-Embedding-0.6B/
```

После этого активируйте окружение, укажите только локальный путь и постройте индекс:

```bash
./scripts/dev.sh install-pip-semantic
source ./scripts/activate-venv.sh
export KB_EMBEDDING_PROVIDER=sentence_transformers
export KB_EMBEDDING_MODEL="$KB_PROJECT_ROOT/models/Qwen3-Embedding-0.6B"
export KB_EMBEDDING_LOCAL_FILES_ONLY=true

./scripts/dev.sh index-semantic
./scripts/dev.sh search "Какой сервис владеет дневными лимитами?"
```

`local_files_only=true`, `HF_HUB_OFFLINE=1` и `TRANSFORMERS_OFFLINE=1` запрещают обращения к
Hugging Face. Если model files отсутствуют, индексирование завершится понятной ошибкой без попытки
скачивания. По умолчанию выбирается CUDA, затем MPS, затем CPU.

`scripts/start-mcp.sh` по умолчанию запускает MCP с `KB_EMBEDDING_PROVIDER=hash`, поэтому обычное
подключение Qwen полностью offline. Для локальной semantic-модели явно передайте provider и путь в
environment Qwen-конфигурации. Все runtime wrappers вызывают Python из готовой `.venv` напрямую:
после установки они не обращаются к package registry и не меняют окружение.

## CLI

```bash
./.venv/bin/python -m corporate_kb.cli index
./.venv/bin/python -m corporate_kb.cli index --force
./.venv/bin/python -m corporate_kb.cli search "Как рассчитывается дневной лимит?" --top-k 5
./.venv/bin/python -m corporate_kb.cli search "Как рассчитывается дневной лимит?" --service limits-service
./.venv/bin/python -m corporate_kb.cli documents
./.venv/bin/python -m corporate_kb.cli stats
./.venv/bin/python -m corporate_kb.cli eval --top-k 5
```

У `search`, `documents`, `stats` и `eval` есть `--json`. В этом режиме stdout содержит только JSON,
а логи остаются в stderr.

Если кэша нет или он несовместим, обычный поиск при `KB_AUTO_INDEX=false` завершится практичным
сообщением `Run: ./scripts/dev.sh index`. Это предотвращает неожиданную сетевую активность во время
MCP discovery.

## Подключение к Qwen Code

Если MCP-серверы хранятся в отдельном каталоге, установите туда автономную runtime-копию. Скрипт
создаёт подкаталог `corporate-kb`, копирует только необходимые файлы, создаёт собственный `.venv`,
ставит locked runtime dependencies без dev-пакетов, строит hash-индекс и печатает готовый server
entry для Qwen:

```bash
./scripts/install-mcp-server.sh /absolute/path/to/mcp-servers
```

Если версия `uv` на целевой машине отличается или `uv` запрещён политиками, установите ту же
runtime-копию через pip:

```bash
./scripts/install-mcp-server.sh /absolute/path/to/mcp-servers --pip
```

Для закрытого окружения можно сначала только скопировать файлы, затем настроить корпоративный Python
package registry и завершить установку командами, которые напечатает скрипт:

```bash
./scripts/install-mcp-server.sh /absolute/path/to/mcp-servers --copy-only
```

Скопируйте `examples/qwen-settings.example.json` в `.qwen/settings.json` проекта и замените все
`/ABSOLUTE/PATH/...` реальными абсолютными путями. Не рассчитывайте на раскрытие `${PROJECT_ROOT}`
в JSON. В `command` указан абсолютный путь к `.venv/bin/python`, а в `args` — запуск модуля
`corporate_kb.mcp.server`. Поэтому Qwen не зависит от глобальных `python`, `uv`, `PATH`, shell
activation или wrapper-скрипта.

Минимальная форма server entry:

```json
{
  "command": "/absolute/path/to/repository/.venv/bin/python",
  "args": ["-m", "corporate_kb.mcp.server"],
  "cwd": "/absolute/path/to/repository",
  "env": {
    "PYTHONPATH": "/absolute/path/to/repository/src"
  }
}
```

Альтернатива через CLI (выполняйте из корня этого репозитория, подставив абсолютные пути):

```bash
qwen mcp add \
  --scope project \
  --timeout 120000 \
  -e KB_KNOWLEDGE_DIR=/absolute/path/to/repository/knowledge \
  -e KB_CACHE_DIR=/absolute/path/to/repository/.cache/kb \
  -e KB_EMBEDDING_PROVIDER=hash \
  -e KB_EMBEDDING_LOCAL_FILES_ONLY=true \
  -e HF_HUB_OFFLINE=1 \
  -e TRANSFORMERS_OFFLINE=1 \
  -e PYTHONUNBUFFERED=1 \
  -e PYTHONNOUSERSITE=1 \
  -e PYTHONPATH=/absolute/path/to/repository/src \
  -e KB_AUTO_INDEX=false \
  local-corporate-kb \
  /absolute/path/to/repository/.venv/bin/python \
  -m corporate_kb.mcp.server
```

`stdio` — транспорт по умолчанию, поэтому `--transport http` здесь не нужен. Синтаксис команды
сверен с [официальной документацией Qwen Code](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/),
но в среде разработки этого репозитория `qwen` не был установлен, и команда локально не выполнялась.
JSON-конфигурация также задаёт `cwd` и `trust: false`; статического фильтра tools в ней нет.

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

Встроенные tools сервера:

- `ssot_context` — собирает единый текущий SSOT-контекст по вопросу;
- `kb_feature_context` — связывает service graph с RAG-индексом каждого затронутого сервиса:
  возвращает `caller → API/event → callee`, статический trigger/handler, evidence и компактные
  выдержки документации для планирования фичи;
- `kb_search` — поиск с `top_k`, `min_score`, metadata filters и компактными выдержками;
- `kb_get_chunk` — лениво загружает один ограниченный фрагмент по `chunk_id`;
- `kb_run_context_benchmark` — защищённый паролем read-only замер качества и сжатия;
- `kb_get_document` — ограниченный извлекаемый фрагмент документа по `document_id`;
- `kb_list_documents` — metadata документов без embeddings;
- `kb_generate_system_ssot` — выбирает индекс и repositories, клонирует недостающий Git source,
  запускает/опрашивает analysis и порционно отдаёт исходники клиентской нейронке для SSOT;
- `kb_save_and_upload_ssot` — локальный tool распределённого stdio-proxy: пишет готовый Markdown во
  временный каталог клиентского компьютера и загружает его в выбранный server index;
- `kb_stats` — состояние индекса и абсолютные пути.

Во вкладке dashboard **«MCP tools»** отображается живой каталог FastMCP: все встроенные tools и
созданные через UI search-tools с теми же описаниями и JSON Schema, которые получает нейросеть в
`tools/list`. Кнопка **«Проверить»** строит форму по входной схеме, выполняет настоящий вызов tool и
показывает полный результат, ошибку и latency. У встроенного tool можно безопасно менять описание
для LLM; код и схема остаются read-only, а изменения сохраняются в
`.cache/kb/builtin_tool_overrides.json` и применяются без перезапуска сервера. У управляемого
search-tool по-прежнему редактируются описание, индексы, фильтры и лимиты.

Пример вызова нового feature-tool через JSON API (тот же обработчик использует MCP):

```bash
curl -sS http://127.0.0.1:8000/api/v1/feature-context \
  -H 'Content-Type: application/json' \
  -d '{"feature":"Добавить резервирование товара при создании заказа","start_service":"orders"}'
```

Если `start_service` не передан, сервис ищется по узлам графа, интерфейсам и затем по документам
RAG. В неоднозначном случае ответ имеет `status: needs_service` и содержит допустимые
`candidate_services` вместо придуманного маршрута.

Не задавайте статический `includeTools` в Qwen settings, если используете управляемые tools из UI:
клиентский allowlist скроет новые схемы от LLM. Прямой HTTP-клиент обновляет `/mcp` discovery, а
однофайловый stdio proxy выполняет тот же удалённый `tools/list` после перезапуска Qwen. Обновлять
сам proxy-файл при добавлении server tools больше не требуется.

### Разработка React-панели

Production assets уже входят в Python-пакет и отдаются основным HTTP-процессом по `/admin`.
Для изменения интерфейса frontend и backend можно запускать независимо в двух терминалах.

#### Backend отдельно — терминал 1

Из корня repository выполните одну команду:

```bash
./scripts/start-backend.sh
```

Она запускает только FastAPI/FastMCP backend на `http://127.0.0.1:8000` и MCP endpoint на
`http://127.0.0.1:8000/mcp`. Если `.venv` ещё нет, скрипт сначала сам установит Python
dependencies. Процесс работает в foreground и останавливается через `Ctrl+C`.

Порт и адрес при необходимости переопределяются environment variables:

```bash
KB_MCP_HTTP_HOST=127.0.0.1 \
KB_MCP_HTTP_PORT=8000 \
KB_AUTO_INDEX=false \
./scripts/start-backend.sh
```

#### Frontend отдельно — терминал 2

Из корня repository выполните вторую команду:

```bash
./scripts/start-frontend.sh
```

Она запускает только React/Vite frontend. Если `node_modules` ещё нет, скрипт сначала сам выполнит
`npm ci`. Открывайте `http://127.0.0.1:5173/admin/`. Vite автоматически проксирует `/admin/api` на
backend `http://127.0.0.1:8000`, поэтому CORS и отдельная настройка API URL не нужны.

Эквивалентный ручной запуск из каталога frontend:

```bash
cd apps/dashboard
npm ci
npm run dev
```

#### Production: один процесс

Для production отдельный frontend-процесс не нужен. Соберите React assets и запустите backend:

```bash
./scripts/dev.sh dashboard-build
KB_MCP_HTTP_HOST=127.0.0.1 ./scripts/start-mcp-http.sh run
```

После этого вся панель доступна через backend по `http://127.0.0.1:8000/admin`. Команда
`dashboard-build` выполняет TypeScript-проверку и складывает готовые assets внутрь
`corporate_kb/mcp/admin_dist`, поэтому Node.js на production-сервере не требуется.

Ручной запуск stdio server:

```bash
KB_LOG_LEVEL=DEBUG ./.venv/bin/python -m corporate_kb.mcp.server
```

stdout зарезервирован для MCP-протокола; все application logs направляются в stderr.

## Удалённый MCP по HTTP

Удалённый режим заранее загружает готовый индекс и только после этого открывает порт. Поэтому все
подключённые Qwen CLI используют один прогретый процесс и не строят embeddings при каждом запросе.
Endpoint реализует рекомендованный для удалённых MCP-серверов Streamable HTTP, а не устаревший SSE.

### 1. Подготовить сервер

Скопируйте репозиторий и документы на сервер, установите runtime и один раз постройте индекс:

```bash
cd /opt/corporate-kb
./scripts/setup-pip.sh --no-dev
./scripts/dev.sh index-hash
```

Для локального запуска пароль и токен не нужны:

```bash
export KB_MCP_HTTP_HOST='127.0.0.1'
export KB_MCP_HTTP_PORT='8000'
export KB_AUTO_INDEX='false'

./scripts/start-mcp-http.sh
```

Без аргументов сервер работает в foreground и корректно завершает дочерние Git-процессы по
`Ctrl+C`. Для управляемого фонового запуска используйте PID-файл и команды lifecycle:

```bash
./scripts/start-mcp-http.sh start
./scripts/start-mcp-http.sh status
./scripts/start-mcp-http.sh logs
./scripts/start-mcp-http.sh stop
./scripts/start-mcp-http.sh restart
```

PID и лог находятся в `.cache/kb/runtime/`. `stop` сначала отправляет `SIGTERM`, а через пять
секунд при необходимости — `SIGKILL`, поэтому зависший HTTP shutdown не оставляет занятый порт.
Git fetch/clone запускаются в отдельной process group: timeout или остановка сервера завершают
также credential helper, SSH и остальные дочерние процессы. По умолчанию Git timeout равен 60
секундам и настраивается через `KB_REPOSITORY_GIT_TIMEOUT_SECONDS`.

Публичный health check не раскрывает тексты документов:

```bash
curl http://10.0.0.5:8000/health
```

Для кастомных HTTP-клиентов сервер также предоставляет read-only JSON API. В стандартном локальном
режиме поиск работает без токена:

```bash
curl -G 'http://10.0.0.5:8000/api/v1/search' \
  --data-urlencode 'query=какой сервис владеет дневными лимитами' \
  --data-urlencode 'top_k=3'
```

Доступны `/api/v1/search`, `/api/v1/document`, `/api/v1/chunk`, защищённый
`/api/v1/admin/context-benchmark`, `/api/v1/documents` и `/api/v1/stats`. Они используют
тот же прогретый индекс, что и MCP tools, не строят embeddings на клиенте и не изменяют документы.

Сам `/mcp` также работает без заголовка авторизации. Если задать
`KB_MCP_HTTP_BEARER_TOKEN`, Bearer-проверка включится одновременно для `/mcp` и JSON API.
`KB_AUTO_INDEX=false` гарантирует, что удалённый процесс не начнёт неожиданную переиндексацию.

Проверяйте с клиентской машины не только `/health`, но и настоящий MCP `initialize`:

```bash
curl -i --max-time 15 \
  'http://10.0.0.5:8000/mcp' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl-test","version":"1.0"}}}'
```

Ожидается `HTTP/1.1 200`. При включённой защите `401` означает неверный токен, `404` — неверный путь, а `421` — что
запущена старая сборка с Host allowlist. Прямой FastMCP listener, запущенный через Python или `uv`,
использует обычный HTTP. `https://` указывайте только при наличии TLS reverse proxy; иначе Qwen
обычно сообщает `TypeError: fetch failed`.

### 2. Подключить Qwen CLI

Перед раздачей впишите в корневой `install.sh` адрес сервера. Токен оставьте пустым, если защита
на сервере не включена:

```bash
default_mcp_url="https://kb.company.example/mcp"
default_mcp_token=""
```

Сотруднику передаётся только этот один файл. В любом каталоге он выполняет:

```bash
bash install.sh
```

Скрипт не скачивает репозиторий, документы или Python-зависимости и не создаёт каталог RAG. Он
только добавляет подключение `corporate-kb` в пользовательскую конфигурацию уже установленного
Qwen Code. После запуска сотрудник перезапускает `qwen` и проверяет соединение через `/mcp`.

Если опциональный Bearer-токен всё же задан, раздавайте файл через защищённый корпоративный канал.

### 3. Доступ через интернет

Не передавайте Bearer-токен по открытому интернету через обычный HTTP. Оставьте backend на
`127.0.0.1:8000`, а наружу опубликуйте его как HTTPS через Nginx, Caddy, ingress или корпоративный
API gateway:

```bash
export KB_MCP_HTTP_BEARER_TOKEN='PASTE_GENERATED_TOKEN'
export KB_MCP_HTTP_HOST='127.0.0.1'
./scripts/start-mcp-http.sh
```

Минимальные существенные параметры location для Nginx:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

После этого клиент подключается к `https://kb.example.com/mcp`. TLS-сертификат и сетевой доступ
настраиваются на reverse proxy; порт `8000` не должен быть открыт наружу.

Когда документы изменились, выполните `./scripts/dev.sh index-hash` и перезапустите HTTP-процесс.
Уже работающий процесс намеренно продолжает обслуживать согласованную старую версию индекса до
рестарта.

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
./scripts/dev.sh index
```

Полностью удалить его можно командой `rm -rf .cache/kb`, после чего снова выполнить `kb index`.
Запись каждого файла атомарна, а `manifest.json` заменяется последним. Повреждение JSON/NumPy,
несовпадение схемы, модели, dimension, query instruction, chunking config или knowledge hash приводит
к понятной invalidation, а не к неясной NumPy-ошибке.

Все параметры перечислены в `.env.example`. Основные:

- `KB_EMBEDDING_PROVIDER=sentence_transformers|hash`;
- `KB_EMBEDDING_MODEL=./models/Qwen3-Embedding-0.6B` — локальный каталог model files;
- `KB_EMBEDDING_LOCAL_FILES_ONLY=true` — fail-closed запрет сетевой загрузки модели;
- `KB_EMBEDDING_DEVICE=auto|cpu|mps|cuda`;
- `KB_EMBEDDING_DIMENSION=1024`;
- `KB_CHUNK_SIZE_TOKENS=700`, `KB_CHUNK_HARD_MAX_TOKENS=900`,
  `KB_CHUNK_OVERLAP_TOKENS=80`;
- `KB_AUTO_INDEX=false`.
- `KB_MCP_HTTP_HOST`, `KB_MCP_HTTP_PORT`, `KB_MCP_HTTP_PATH`;
- `KB_MCP_HTTP_BEARER_TOKEN` — опциональная защита HTTP/MCP; пустое значение включает открытый режим;

Относительные пути разрешаются относительно текущего project working directory; `kb stats`
показывает итоговые абсолютные пути.

## Проверки

```bash
./scripts/dev.sh lint
./scripts/dev.sh typecheck
./scripts/dev.sh test
./scripts/dev.sh check
```

Обычный shell-скрипт `scripts/dev.sh` также объединяет повседневные команды:

```bash
./scripts/dev.sh install
./scripts/dev.sh install-pip
./scripts/dev.sh install-semantic
./scripts/dev.sh install-pip-semantic
./scripts/dev.sh test
./scripts/dev.sh lint
./scripts/dev.sh typecheck
./scripts/dev.sh index-hash
./scripts/dev.sh search-hash
./scripts/dev.sh index
./scripts/dev.sh search
./scripts/dev.sh index-semantic
./scripts/dev.sh eval
./scripts/dev.sh serve
./scripts/dev.sh serve-http
```

Тесты всегда инжектируют hash provider и не требуют интернета, Hugging Face, GPU, Qwen Code, Docker
или внешней БД. Интеграционные тесты проверяют как in-memory MCP transport, так и HTTP handshake
через ASGI без открытия сетевого порта.

## Ограничения MVP и развитие

- Полный brute-force cosine scan подходит для небольшой локальной базы, но не для миллионов чанков.
- При изменении документов неизменившиеся chunks и их embeddings переиспользуются из предыдущего
  кэша; полный пересчёт нужен только для новых или изменившихся chunks.
- Нет Confluence REST API, OAuth, фоновой синхронизации и HTML-адаптеров под каждый вариант экспорта.
- Нет reranker, hybrid/BM25 retrieval и отдельной оценки authority при ранжировании.
- Точный token counter реальной модели не используется для предварительного chunking: интерфейс
  `TokenCounter` отделён, поэтому его можно подключить без связи chunker с SentenceTransformer.
- Статический Bearer-токен даёт всем клиентам одинаковые права; для персональных учётных записей,
  отзыва сессий и аудита нужен внешний gateway/IdP либо полноценный OAuth.

Для перехода на настоящую Vector DB нужно реализовать `PostgresKnowledgeStore` или
`QdrantKnowledgeStore` с тем же контрактом `KnowledgeStore`, выбрать реализацию при сборке
`KnowledgeService` и сохранить API сервиса/MCP без изменений. Следующим этапом стоит добавить
инкрементальный cache manifest, batch upsert, hybrid retrieval и production evaluation corpus.
