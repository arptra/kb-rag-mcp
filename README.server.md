# Развёртывание корпоративной RAG-базы на сервере

Эта инструкция предназначена для администратора удалённого сервера. Сервер хранит документы,
строит индекс один раз, загружает его в память и отдаёт результаты поиска авторизованным клиентам.

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
GET /api/v1/* с Bearer-токеном
        ↓
локальные stdio MCP-клиенты сотрудников
```

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

## 4. Создать токен

```bash
openssl rand -hex 32
```

Сохраните значение как секрет. Один и тот же токен должен быть установлен на сервере и передан
авторизованным клиентам. Не добавляйте реальный токен в Git.

## 5. Запустить сервер вручную

```bash
cd /opt/corporate-kb

export KB_MCP_HTTP_HOST='0.0.0.0'
export KB_MCP_HTTP_PORT='8000'
export KB_MCP_HTTP_BEARER_TOKEN='REPLACE_WITH_GENERATED_TOKEN'
export KB_AUTO_INDEX='false'

./scripts/start-mcp-http.sh
```

Allowlist адресов не используется. Сервер принимает запросы с любых адресов, которые доступны по
сети, но защищённые endpoints требуют правильный Bearer-токен.

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

Проверка авторизации и индекса:

```bash
curl -i \
  'http://127.0.0.1:8000/api/v1/stats' \
  -H 'Authorization: Bearer REPLACE_WITH_GENERATED_TOKEN'
```

Проверка поиска:

```bash
curl -G \
  'http://127.0.0.1:8000/api/v1/search' \
  -H 'Authorization: Bearer REPLACE_WITH_GENERATED_TOKEN' \
  --data-urlencode 'query=какой сервис владеет дневными лимитами' \
  --data-urlencode 'top_k=5'
```

Ожидается `HTTP 200` и JSON. Затем повторите команды с клиентского компьютера, используя сетевой IP
сервера.

## Read-only JSON API

Все endpoints используют `GET` и один Bearer-токен:

| Endpoint | Назначение | Основные параметры |
| --- | --- | --- |
| `/api/v1/search` | Поиск релевантных фрагментов | `query`, `top_k`, filters |
| `/api/v1/document` | Полный документ | `document_id` |
| `/api/v1/documents` | Список metadata | filters, `limit` |
| `/api/v1/stats` | Состояние индекса | нет |

API не изменяет документы и не запускает команды. Поисковый запрос передаётся в URL и может
попадать в access logs; ограничьте доступ к журналам сервера.

## 7. Запустить как systemd service

Создайте `/etc/corporate-kb.env`:

```dotenv
KB_MCP_HTTP_HOST=0.0.0.0
KB_MCP_HTTP_PORT=8000
KB_MCP_HTTP_BEARER_TOKEN=REPLACE_WITH_GENERATED_TOKEN
KB_AUTO_INDEX=false
KB_EMBEDDING_PROVIDER=hash
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

Проверьте точное совпадение Bearer-токена на сервере и клиенте.

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
