# Подключение Qwen к корпоративной базе знаний

Эта инструкция предназначена для сотрудника, который подключает Qwen Code к уже работающему
удалённому RAG-серверу.

## Как это работает

```text
Qwen Code
    ↓ stdio
локальный corporate_kb_stdio_proxy.py
    ↓ обычные авторизованные HTTP GET-запросы
удалённый RAG API
    ↓
корпоративные документы
```

Qwen сам запускает локальный MCP-процесс. Отдельно запускать его перед каждым сеансом не нужно.
На клиент не копируются документы, индекс, embeddings или исходный код серверной части.

## Что должен выдать администратор

1. Один файл `corporate_kb_stdio_proxy.py`.
2. Адрес сервера без `/mcp`, например `http://10.20.30.40:8000`.
3. Bearer-токен доступа.

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
  'http://SERVER_IP:8000/api/v1/stats' \
  -H 'Authorization: Bearer SERVER_TOKEN'
```

Проверка поиска:

```bash
curl -G \
  'http://SERVER_IP:8000/api/v1/search' \
  -H 'Authorization: Bearer SERVER_TOKEN' \
  --data-urlencode 'query=какой сервис владеет дневными лимитами' \
  --data-urlencode 'top_k=3'
```

Оба запроса должны вернуть `HTTP 200` и JSON. Если получен `401`, проверьте токен.

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
        "CORPORATE_KB_API_URL": "http://SERVER_IP:8000",
        "CORPORATE_KB_API_TOKEN": "SERVER_TOKEN",
        "CORPORATE_KB_API_TIMEOUT": "30"
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
        "CORPORATE_KB_API_URL": "http://SERVER_IP:8000",
        "CORPORATE_KB_API_TOKEN": "SERVER_TOKEN",
        "CORPORATE_KB_API_TIMEOUT": "30"
      },
      "timeout": 120000,
      "trust": false
    }
  }
}
```

В `CORPORATE_KB_API_URL` не добавляйте `/mcp` или `/api/v1`: нужен только адрес сервера.

## 5. Запустить Qwen

Полностью закройте ранее запущенный Qwen, затем запустите снова:

```bash
qwen
```

Откройте:

```text
/mcp
```

Сервер `corporate-kb` должен показать семь встроенных инструментов:

```text
kb_search
kb_get_document
kb_get_chunk
kb_run_context_benchmark
kb_list_documents
kb_stats
ssot_context
```

`kb_search` возвращает короткие выдержки, а не полные страницы Confluence. Если Qwen нужен текст
конкретного результата, он сам вызывает `kb_get_chunk` с `chunk_id` — это сохраняет контекст диалога.

`kb_run_context_benchmark` — отдельный административный прогон. При его вызове Qwen должен сначала
спросить отдельный benchmark-пароль. Не сохраняйте этот пароль в Qwen settings и не используйте
вместо него обычный `CORPORATE_KB_API_TOKEN`.

Администратор может создавать дополнительные search-tools через защищённый серверный API.
Однофайловый proxy получает их имена, описания и JSON Schema при старте. Чтобы увидеть новый tool,
полностью перезапустите Qwen. Не добавляйте `includeTools` в settings — статический список скроет
новые tools.

Кнопка OAuth-аутентификации не нужна: Bearer-токен уже передаётся локальному Python-процессу через
Qwen settings.

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

### Configuration error: URL must not include /mcp

Используйте:

```text
http://SERVER_IP:8000
```

а не:

```text
http://SERVER_IP:8000/mcp
```

### Remote RAG returned HTTP 401

Токен отсутствует, устарел или скопирован с лишними символами. Запросите новый токен у
администратора.

### uv не может скачать FastMCP

Проверьте настройки корпоративного Python package index. Это единственная загрузка зависимости на
клиенте; документы и индекс при этом не скачиваются.

### Поиск работает через curl, но не через Qwen

Запустите файл вручную той же командой из settings:

```bash
/absolute/path/to/uv run /absolute/path/to/corporate_kb_stdio_proxy.py
```

Процесс должен ожидать MCP-сообщения в stdin. Ошибки конфигурации выводятся в stderr. Остановить
ручной запуск можно через `Ctrl+C`.

## Обновление и удаление

Для обновления замените `corporate_kb_stdio_proxy.py` новой версией по тому же пути и перезапустите
Qwen.

Для удаления удалите объект `corporate-kb` из `mcpServers`, затем удалите единственный `.py`-файл.
