# Подключение Qwen к корпоративной базе знаний

Эта инструкция предназначена для сотрудника, который подключает Qwen Code к уже работающему
удалённому RAG-серверу.

## Как это работает

```text
Qwen Code
    ↓ stdio
локальный corporate_kb_stdio_proxy.py
    ↓ Streamable HTTP MCP
общий RAG endpoint /mcp
    ↓
корпоративные документы
```

Qwen сам запускает локальный MCP-процесс. Отдельно запускать его перед каждым сеансом не нужно.
На клиент не копируются документы, индекс, embeddings или исходный код серверной части.

## Что должен выдать администратор

1. Один файл `corporate_kb_stdio_proxy.py`.
2. Адрес MCP endpoint, например `http://10.20.30.40:8000/mcp`.
3. Bearer-токен — только если администратор включил защиту на сервере.

На клиентском компьютере должны быть установлены Qwen Code и `uv`. Node.js, `npx`, `mcp-remote`,
Nginx и отдельное Python-окружение не нужны.

## 1. Сохранить клиентский файл

Сохраните `corporate_kb_stdio_proxy.py` в постоянном месте. После настройки файл нельзя перемещать,
потому что Qwen будет запускать его по абсолютному пути.

Примеры:

```text
/home/user/corporate_kb_stdio_proxy.py
C:/Users/User/corporate_kb_stdio_proxy.py
```

## 2. Найти абсолютный путь к uv

Linux или macOS:

```bash
command -v uv
```

Windows PowerShell:

```powershell
(Get-Command uv).Source
```

Windows CMD:

```bat
where uv
```

Используйте полученный абсолютный путь в Qwen settings. Это важно: Qwen может запускаться с другим
`PATH` и не находить просто команду `uv`.

## 3. Проверить доступ к удалённой базе

Проверка статистики:

```bash
curl -i \
  'http://SERVER_IP:8000/api/v1/stats'
```

Проверка поиска:

```bash
curl -G \
  'http://SERVER_IP:8000/api/v1/search' \
  --data-urlencode 'query=какой сервис владеет дневными лимитами' \
  --data-urlencode 'top_k=3'
```

Оба запроса должны вернуть `HTTP 200` и JSON. Если администратор всё же включил Bearer-защиту,
добавьте к командам `-H 'Authorization: Bearer SERVER_TOKEN'`.

## 4. Добавить MCP в Qwen

Пользовательский файл настроек:

- Linux/macOS: `~/.qwen/settings.json`;
- Windows: `%USERPROFILE%\.qwen\settings.json`.

Если файл уже содержит настройки модели или другие MCP-серверы, добавьте объект `corporate-kb` в
существующий `mcpServers`, не удаляя остальные поля.

### Linux или macOS

```json
{
  "mcpServers": {
    "corporate-kb": {
      "command": "/home/user/.local/bin/uv",
      "args": [
        "run",
        "/home/user/corporate_kb_stdio_proxy.py"
      ],
      "env": {
        "CORPORATE_KB_MCP_URL": "http://SERVER_IP:8000/mcp",
        "CORPORATE_KB_MCP_TIMEOUT": "120"
      },
      "timeout": 120000,
      "trust": false
    }
  }
}
```

### Windows

В JSON удобно использовать прямые `/`, чтобы не экранировать обратные слеши:

```json
{
  "mcpServers": {
    "corporate-kb": {
      "command": "C:/Users/User/.local/bin/uv.exe",
      "args": [
        "run",
        "C:/Users/User/corporate_kb_stdio_proxy.py"
      ],
      "env": {
        "CORPORATE_KB_MCP_URL": "http://SERVER_IP:8000/mcp",
        "CORPORATE_KB_MCP_TIMEOUT": "120"
      },
      "timeout": 120000,
      "trust": false
    }
  }
}
```

`CORPORATE_KB_MCP_URL` должен указывать на общий сервер, а не на компьютер сотрудника. `localhost`
подходит только если сервер запущен на той же машине или к нему настроен SSH/VPN tunnel.
Старая переменная `CORPORATE_KB_API_URL=http://SERVER_IP:8000` продолжает работать: proxy сам
добавит `/mcp`.

## 5. Запустить Qwen

Полностью закройте ранее запущенный Qwen, затем запустите снова:

```bash
qwen
```

Откройте:

```text
/mcp
```

Сервер `corporate-kb` должен показать все инструменты, которые сейчас отдаёт общий сервер. В
текущей версии среди них есть:

```text
kb_feature_context
kb_generate_system_ssot
kb_search
kb_get_document
kb_get_chunk
kb_run_context_benchmark
kb_list_documents
kb_stats
ssot_context
```

Перед реализацией межсервисной фичи вызывайте `kb_feature_context`. Минимальный аргумент — текст
фичи; если исходный сервис известен, передайте `start_service`. Tool сам проходит входящие и
исходящие связи (по умолчанию два hop), показывает протокол и операцию вызова и ищет документацию
именно в индексе репозитория каждого найденного сервиса. Поле `invocation_contexts` означает
статически восстановленный trigger/handler, а не наблюдённый runtime trace.

`kb_search` возвращает короткие выдержки, а не полные страницы Confluence. Если Qwen нужен текст
конкретного результата, он сам вызывает `kb_get_chunk` с `chunk_id` — это сохраняет контекст диалога.

`kb_run_context_benchmark` — отдельный административный прогон. При его вызове Qwen должен сначала
спросить отдельный benchmark-пароль. Не сохраняйте этот пароль в Qwen settings и не используйте
вместо него обычный `CORPORATE_KB_MCP_TOKEN`.

Администратор может добавлять built-in и управляемые tools на общем сервере. Proxy не содержит их
список: при старте он делает настоящий MCP `tools/list`, переносит имена, описания, JSON Schema и
annotations, а `tools/call` прозрачно отправляет обратно на сервер. Чтобы клиент перечитал новый
набор, полностью перезапустите Qwen. Заменять `corporate_kb_stdio_proxy.py` при добавлении tools не
нужно. Не добавляйте `includeTools` в settings — статический allowlist скроет новые tools.

Кнопка OAuth-аутентификации не нужна. В стандартном режиме токена нет; при включённой серверной
защите Bearer-токен передаётся локальному Python-процессу через Qwen settings.

Тестовый запрос:

```text
Используй corporate-kb. Найди информацию о дневных лимитах и укажи использованные источники.
```

## Что происходит при первом запуске

В начале `uv` читает inline dependency из единственного `.py`-файла и устанавливает
`FastMCP==3.4.4` в собственный кэш. Последующие запуски используют кэш и происходят быстрее.
Проектная папка и локальная `.venv` не создаются.

Если корпоративный компьютер использует внутренний Python package index, он должен содержать
FastMCP и его зависимости либо быть настроен в `uv` администратором.

## Диагностика

### Qwen не видит MCP

- Проверьте абсолютные пути к `uv` и `.py`-файлу.
- Проверьте JSON на лишние запятые.
- Полностью перезапустите Qwen после изменения settings.

### Configuration error: URL must be a server root or end with /mcp

Предпочтительный вариант:

```text
http://SERVER_IP:8000/mcp
```

Для совместимости разрешена старая переменная:

```text
CORPORATE_KB_API_URL=http://SERVER_IP:8000
```

### Remote RAG returned HTTP 401

На сервере включена опциональная Bearer-защита. Запросите токен у администратора и добавьте
`CORPORATE_KB_MCP_TOKEN` в `env` MCP-настройки.

### uv не может скачать FastMCP

Проверьте настройки корпоративного Python package index. Это единственная загрузка зависимости на
клиенте; документы и индекс при этом не скачиваются.

### Поиск работает через curl, но не через Qwen

Запустите файл вручную той же командой из settings:

```bash
CORPORATE_KB_MCP_URL=http://SERVER_IP:8000/mcp \
  /absolute/path/to/uv run /absolute/path/to/corporate_kb_stdio_proxy.py
```

Процесс должен ожидать MCP-сообщения в stdin. Ошибки конфигурации выводятся в stderr. Остановить
ручной запуск можно через `Ctrl+C`.

## Обновление и удаление

При появлении новых server tools обновление файла не требуется — достаточно перезапустить Qwen.
Сам `corporate_kb_stdio_proxy.py` заменяйте только при выпуске новой версии proxy transport.

Для удаления удалите объект `corporate-kb` из `mcpServers`, затем удалите единственный `.py`-файл.
