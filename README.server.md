# Развёртывание корпоративной RAG-базы на сервере

Эта инструкция предназначена для администратора удалённого сервера. Сервер хранит документы,
строит индекс один раз, загружает его в память и отдаёт результаты поиска HTTP/MCP-клиентам.

## Архитектура

```text
knowledge/*.md, *.html, *.txt
        ↓
нормализация и структурные чанки
        ↓
embeddings + файловый кэш .cache/kb
        ↓
прогретый индекс в RAM
        ↓
GET /api/v1/* (Bearer опционален)
        ↓
локальные stdio MCP-клиенты сотрудников
```

Поиск ограничивает ответ для LLM: внутри индекс ищет до 12 кандидатов, но наружу возвращаются до
3 разных документов, выдержки до 260 условных токенов и суммарно до 1000. Для деталей клиент
запрашивает только выбранный `/api/v1/chunk`; все лимиты можно изменить через `KB_SEARCH_*` и
`KB_DOCUMENT_CONTEXT_TOKENS`.

На сервере также остаётся `/mcp` для новых клиентов с нативным Streamable HTTP. Старые Qwen и
корпоративные сети могут использовать только JSON API через однофайловый stdio proxy.

## Требования

- Linux-сервер или VM;
- Python 3.12;
- доступный TCP-порт, по умолчанию `8000`;
- репозиторий проекта;
- документы в форматах `.md`, `.markdown`, `.html`, `.htm` или `.txt`.

Версия системного `uv` не важна: рекомендуемая серверная установка использует обычный `pip` внутри
локальной `.venv`.

## 1. Разместить проект и документы

Пример расположения:

```bash
cd /opt
git clone https://github.com/arptra/kb-rag-mcp.git corporate-kb
cd /opt/corporate-kb
```

Скопируйте документы в `knowledge/`. Подкаталоги разрешены:

```text
knowledge/
├── architecture/
│   └── payments.md
├── runbooks/
│   └── limits.html
└── business-rules.txt
```

Скрытые каталоги, symlink-каталоги, symlink-файлы и неподдерживаемые форматы не индексируются.
Повреждённый или нечитаемый документ пропускается с предупреждением в логе; остальные документы
продолжают индексироваться.

## 2. Установить серверное окружение

```bash
cd /opt/corporate-kb
./scripts/setup-pip.sh --no-dev
```

Скрипт создаёт `/opt/corporate-kb/.venv` на Python 3.12 и устанавливает FastMCP и runtime
зависимости. Глобальные Python-пакеты не изменяются.

## 3. Построить индекс

Для первого запуска без внешней модели используйте hash provider:

```bash
cd /opt/corporate-kb
./scripts/dev.sh index-hash
```

В `.cache/kb/` появятся:

```text
manifest.json
documents.json
chunks.json
embeddings.npy
```

Эти файлы являются серверным кэшем. На клиентские компьютеры их копировать не нужно.

Проверка индекса:

```bash
./scripts/dev.sh search-hash 'дневные лимиты'
```

Для 10 000 документов первый semantic-индекс может строиться долго: embedding-модель должна
обработать каждый новый chunk. На CPU это нормально; для ускорения используйте GPU и увеличьте
batch size, если хватает памяти:

```bash
export KB_EMBEDDING_PROVIDER=sentence_transformers
export KB_EMBEDDING_DEVICE=cuda
export KB_EMBEDDING_BATCH_SIZE=32
./scripts/dev.sh index
```

При последующих обновлениях неизменившиеся chunks берутся из предыдущего кэша, поэтому модель
обрабатывает только новые или изменённые chunks. Во время обхода, chunking и embedding сервер пишет
прогресс в stderr; для systemd смотрите его так:

```bash
sudo journalctl -u corporate-kb -f
```

## 4. Авторизация необязательна

Для локального или доверенного контура ничего создавать не нужно: оставьте
`KB_MCP_HTTP_BEARER_TOKEN` и `KB_ADMIN_PASSWORD` пустыми. `/mcp`, JSON API и `/admin` будут
доступны без экрана входа и дополнительных заголовков.

Если сервер публикуется в недоверенную сеть, защиту можно включить отдельно. Создайте токен:

```bash
openssl rand -hex 32
```

Сохраните его в `KB_MCP_HTTP_BEARER_TOKEN`. Для защиты web-панели можно отдельно задать
`KB_ADMIN_PASSWORD`; оба значения включают соответствующую проверку только когда они непустые.
Не добавляйте реальные секреты в Git.

Для административного benchmark создайте второй, отдельный пароль:

```bash
openssl rand -hex 32
```

Запишите его в `KB_BENCHMARK_PASSWORD`. Этот пароль не заменяет Bearer-токен и не должен храниться
в клиентском settings: Qwen запрашивает его у администратора непосредственно перед запуском
`kb_run_context_benchmark`.

## 5. Запустить сервер вручную

```bash
cd /opt/corporate-kb

export KB_MCP_HTTP_HOST='127.0.0.1'
export KB_MCP_HTTP_PORT='8000'
export KB_AUTO_INDEX='false'

./scripts/start-mcp-http.sh
```

Для фонового режима с предсказуемой остановкой:

```bash
./scripts/start-mcp-http.sh start
./scripts/start-mcp-http.sh status
./scripts/start-mcp-http.sh logs
./scripts/start-mcp-http.sh stop
```

Скрипт хранит PID и лог в `.cache/kb/runtime/`; `stop` принудительно завершает процесс после
пяти секунд, если штатный shutdown завис. Запускайте скрипт из любого каталога — рабочей
директорией всегда становится корень проекта, поэтому jobs и индексы не расползаются по разным
`.cache`.

Это открытый локальный запуск без паролей. Для сетевого режима явно задайте `0.0.0.0`; если сеть
недоверенная, одновременно установите `KB_MCP_HTTP_BEARER_TOKEN` и `KB_ADMIN_PASSWORD`.

При старте готовый индекс загружается в RAM до открытия порта. При
`KB_AUTO_INDEX=false` сервер **не обходит `knowledge/`** и не пересчитывает hash 11 000 документов:
он читает только готовый `.cache/kb/`. Embeddings не перестраиваются на каждый клиентский запрос.
Оставляйте `KB_AUTO_INDEX=false` для production; `true` нужно только для разовой отладки, когда
разрешено строить индекс автоматически.

## 6. Проверить сервер

Публичная проверка процесса:

```bash
curl -i 'http://127.0.0.1:8000/health'
```

Проверка индекса:

```bash
curl -i \
  'http://127.0.0.1:8000/api/v1/stats'
```

Проверка поиска:

```bash
curl -G \
  'http://127.0.0.1:8000/api/v1/search' \
  --data-urlencode 'query=какой сервис владеет дневными лимитами' \
  --data-urlencode 'top_k=3'
```

Ожидается `HTTP 200` и JSON. Затем повторите команды с клиентского компьютера, используя сетевой IP
сервера.

## Client JSON API

В открытом режиме endpoints не требуют Bearer-токен. Если задан
`KB_MCP_HTTP_BEARER_TOKEN`, тот же API автоматически начинает требовать его; чтение использует
`GET`, вызов управляемого tool и benchmark — `POST`:

| Endpoint | Назначение | Основные параметры |
| --- | --- | --- |
| `/api/v1/search` | Поиск релевантных фрагментов | `query`, `top_k`, filters |
| `/api/v1/document` | Ограниченная выдержка документа | `document_id`, `max_tokens` |
| `/api/v1/chunk` | Ограниченная выдержка найденного чанка | `chunk_id`, `max_tokens` |
| `/api/v1/tools` | Каталог управляемых MCP schemas | нет |
| `/api/v1/tools/call` | Выполнение управляемого search-tool | POST JSON |
| `/api/v1/admin/context-benchmark` | Парольный замер качества и сжатия | POST + password header |
| `/api/v1/documents` | Список metadata | filters, `limit` |
| `/api/v1/stats` | Состояние индекса | нет |

Client API не изменяет документы и не запускает команды. Поисковый запрос передаётся в URL и может
попадать в access logs; ограничьте доступ к журналам сервера.

Ручная проверка benchmark:

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/admin/context-benchmark' \
  -H 'X-KB-Benchmark-Password: REPLACE_WITH_SEPARATE_BENCHMARK_PASSWORD'
```

Ответ содержит только агрегаты и вопросы, на которых сжатый `Hit@3` промахнулся; тексты документов
в результат benchmark не включаются.

## Admin UI

После запуска откройте:

```text
http://SERVER_IP:8000/admin
```

React/TypeScript-панель содержит три рабочих раздела:

- **Индексы** — создание изолированных RAG-индексов, ручная пересборка и фоновые jobs;
- **MCP tools** — создание безопасных search-tools, настройка фильтров и привязка к одному или
  нескольким индексам; tool можно временно отвязать от всех индексов;
- **Граф системы** — service-level или полный граф фактов, извлечённых из исходников.

При подключении репозитория укажите название, Git URL, необязательный branch/tag/commit и целевой
индекс. Сервер выполняет shallow fetch в управляемый `KB_REPOSITORY_CACHE_DIR`, не исполняет код
репозитория, ищет каталог `openspec` без обхода `.git`, `node_modules`, `vendor`, `build`, `dist` и
`target`, затем индексирует только Markdown/HTML/TXT. Повторное подключение того же URL/ref обновляет
checkout и заменяет его OpenSpec-снимок в индексе. После успешной RAG-сборки сервер обновляет общий
граф репозиториев.

Декларативные schemas сохраняются в `KB_MANAGED_TOOLS_PATH`, каталог индексов — в
`KB_INDEX_CATALOG_PATH`. Подключённому прямому MCP-клиенту после изменения tools нужно обновить
discovery; однофайловому proxy — перезапустить Qwen.

## 7. Запустить как systemd service

Создайте `/etc/corporate-kb.env`:

```dotenv
KB_MCP_HTTP_HOST=0.0.0.0
KB_MCP_HTTP_PORT=8000
KB_MCP_HTTP_BEARER_TOKEN=REPLACE_WITH_GENERATED_TOKEN
KB_AUTO_INDEX=false
KB_EMBEDDING_PROVIDER=hash
KB_SEARCH_CANDIDATE_K=12
KB_SEARCH_EXCERPT_TOKENS=260
KB_SEARCH_CONTEXT_TOKENS=1000
KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT=1
KB_DOCUMENT_CONTEXT_TOKENS=800
KB_BENCHMARK_QUESTIONS_PATH=/opt/corporate-kb/evaluation/questions.json
KB_BENCHMARK_PASSWORD=REPLACE_WITH_SEPARATE_BENCHMARK_PASSWORD
KB_BENCHMARK_MAX_QUESTIONS=100
KB_ADMIN_PASSWORD=REPLACE_WITH_SEPARATE_ADMIN_PASSWORD
KB_ADMIN_MAX_UPLOAD_BYTES=10000000
KB_MANAGED_TOOLS_PATH=/opt/corporate-kb/.cache/kb/managed_tools.json
KB_INDEX_CATALOG_PATH=/opt/corporate-kb/.cache/kb/index_catalog.json
KB_MANAGED_INDEXES_DIR=/opt/corporate-kb/.cache/kb/indexes
KB_REPOSITORY_CACHE_DIR=/opt/corporate-kb/.cache/kb/repositories
KB_GRAPH_STORE_PATH=/opt/corporate-kb/.cache/kb/system_graph.json
KB_REPOSITORY_MAX_FILES=10000
KB_LOG_LEVEL=INFO
```

Ограничьте доступ:

```bash
sudo chmod 600 /etc/corporate-kb.env
```

Создайте `/etc/systemd/system/corporate-kb.service`:

```ini
[Unit]
Description=Corporate Knowledge RAG API
After=network.target

[Service]
Type=simple
User=corporate-kb
Group=corporate-kb
WorkingDirectory=/opt/corporate-kb
EnvironmentFile=/etc/corporate-kb.env
ExecStart=/opt/corporate-kb/scripts/start-mcp-http.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Замените `User` и `Group` на существующую учётную запись сервиса. Она должна иметь право
читать проект, `knowledge/` и `.cache/kb/`. Индекс лучше перестраивать от имени этой же учётной
записи, чтобы не менять владельца файлов кэша.

Запуск:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now corporate-kb
sudo systemctl status corporate-kb
```

Логи:

```bash
sudo journalctl -u corporate-kb -f
```

## 8. Открыть сетевой доступ

В доверенной корпоративной сети или VPN разрешите клиентам TCP-доступ к порту `8000`. Клиентский
URL будет выглядеть так:

```text
http://SERVER_IP:8000
```

В клиентском `CORPORATE_KB_API_URL` не указывайте `/mcp` или `/api/v1`.

Для публикации вне доверенной сети используйте TLS и корпоративный gateway согласно правилам вашей
инфраструктуры. Это не требуется для работы внутри защищённой сети.

## Обновление документов

После добавления или изменения файлов:

```bash
cd /opt/corporate-kb
./scripts/dev.sh index-hash
sudo systemctl restart corporate-kb
```

Новый процесс загрузит обновлённый индекс до начала обслуживания клиентов.

На старте сервер не перестраивает embeddings при каждом запросе: он загружает совместимый кэш в
RAM. Если кэш несовместим, в логах будут отдельные этапы `Loaded knowledge documents`, `Chunked
knowledge documents`, `Embedded ...` и `Saved knowledge cache`, поэтому зависание можно отличить от
медленного semantic indexing.

## Обновление приложения

```bash
cd /opt/corporate-kb
git pull --ff-only
./scripts/setup-pip.sh --no-dev
./scripts/dev.sh index-hash
sudo systemctl restart corporate-kb
```

Перед обновлением production рекомендуется выполнить проверки в отдельном checkout:

```bash
./scripts/dev.sh check
```

## Диагностика

### `/health` работает, `/api/v1/*` возвращает `401`

На сервере задан `KB_MCP_HTTP_BEARER_TOKEN`. Проверьте его совпадение на сервере и клиенте либо
очистите переменную, чтобы вернуть открытый режим.

### Сервер не стартует: knowledge index is missing or incompatible

Перестройте индекс:

```bash
./scripts/dev.sh index-hash
```

### Сервер слушает только localhost

Установите:

```dotenv
KB_MCP_HTTP_HOST=0.0.0.0
```

и перезапустите процесс.

### Клиент не видит сервер

С клиентского компьютера проверьте:

```bash
curl -i 'http://SERVER_IP:8000/health'
```

Если ответа нет, проверьте firewall, маршрут/VPN и адрес bind. Если `/health` работает, проверьте
авторизованный `/api/v1/stats`.
