# Подключение GigaCode к корпоративной базе знаний

Эта инструкция предназначена для сотрудника, который подключает GigaCode к уже работающему
удалённому RAG-серверу.

## Как это работает

```text
GigaCode
    ↓ stdio
локальный corporate_kb_stdio_proxy.py
    ├── временные SSOT-файлы на клиенте
    ↓ Streamable HTTP MCP
общий RAG endpoint /mcp
    ↓
корпоративные документы
```

GigaCode сам запускает локальный MCP-процесс. Отдельно запускать его перед каждым сеансом не нужно.
На клиент не копируются индекс и embeddings. При генерации SSOT сервер отдаёт вызывающей модели
только выбранный source analysis и запрошенные ею файлы; готовый Markdown остаётся во временном
каталоге клиента и одновременно загружается в выбранный RAG-индекс.

## Что должен выдать администратор

1. Файлы `corporate_kb_stdio_proxy.py` и `requirements.txt` из каталога `clients/`.
2. Адрес MCP endpoint, например `http://10.20.30.40:8000/mcp`.
3. Bearer-токен — только если администратор включил защиту на сервере.

На клиентском компьютере должны быть установлены GigaCode, Python 3.12 и `pip`. `uv`, Node.js,
`npx`, `mcp-remote` и Nginx не нужны. Используется отдельное клиентское окружение `venv` с
зафиксированным `FastMCP==3.4.4`. Она не использует Python-окружение RAG-сервера или других
проектов и устраняет конфликты импортов.

## 1. Сохранить клиентский файл

Сохраните `corporate_kb_stdio_proxy.py` в постоянном месте. После настройки файл нельзя перемещать,
потому что GigaCode будет запускать его по абсолютному пути.

Примеры:

```text
/home/user/corporate_kb_stdio_proxy.py
C:/Users/User/corporate_kb_stdio_proxy.py
```

## 2. Создать чистое клиентское Python-окружение

Положите рядом два файла из repository:

```text
corporate-kb-client/
├── corporate_kb_stdio_proxy.py
└── requirements.txt
```

`requirements.txt` находится в `clients/requirements.txt`. Старое окружение не используйте.

### Linux или macOS

Перейдите в созданный каталог и выполните:

```bash
cd /ABSOLUTE/PATH/corporate-kb-client

python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt

python -c 'import fastmcp; from fastmcp import Client, FastMCP; from fastmcp.server import create_proxy; print(f"FastMCP {fastmcp.__version__}: OK")'
```

Последняя команда должна вывести `FastMCP 3.4.4: OK`.

Если в этом каталоге уже есть сломанная `venv`, сначала сохраните её под другим именем и повторите
команды:

```bash
deactivate 2>/dev/null || true
mv venv "venv-old-$(date +%Y%m%d-%H%M%S)"
```

### Windows PowerShell

```powershell
Set-Location "C:/ABSOLUTE/PATH/corporate-kb-client"

py -3.12 -m venv venv
& "venv/Scripts/Activate.ps1"
python -m pip install --upgrade pip
python -m pip install --requirement requirements.txt

python -c 'import fastmcp; from fastmcp import Client, FastMCP; from fastmcp.server import create_proxy; print(f"FastMCP {fastmcp.__version__}: OK")'
```

Старое окружение в PowerShell можно сохранить так:

```powershell
deactivate
Rename-Item venv ("venv-old-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
```

## 3. Проверить клиент вручную

Linux или macOS:

```bash
cd /ABSOLUTE/PATH/corporate-kb-client
source venv/bin/activate
CORPORATE_KB_MCP_URL=http://SERVER_IP:8000/mcp \
  python corporate_kb_stdio_proxy.py
```

Windows PowerShell:

```powershell
Set-Location "C:/ABSOLUTE/PATH/corporate-kb-client"
& "venv/Scripts/Activate.ps1"
$env:CORPORATE_KB_MCP_URL = "http://SERVER_IP:8000/mcp"
python "corporate_kb_stdio_proxy.py"
```

При успешном запуске процесс будет молча ожидать MCP-сообщения в stdin. Это нормально; остановите
его через `Ctrl+C`. `ModuleNotFoundError` или ошибка импорта означает, что окружение не активировано
либо зависимости установлены не в этот `venv`.

## 4. Добавить MCP в GigaCode

Сначала удалите прежнюю запись с тем же именем, чтобы GigaCode не запускал старый Python:

```bash
gigacode mcp remove corporate-kb
```

Пользовательский файл настроек:

- Linux/macOS: `~/.gigacode/settings.json`;
- Windows: `%USERPROFILE%\.gigacode\settings.json`.

Если файл уже содержит настройки модели или другие MCP-серверы, добавьте объект `corporate-kb` в
существующий `mcpServers`, не удаляя остальные поля. В `command` должен быть абсолютный путь именно
к Python из нового клиентского `venv`, а не глобальный `python` или Python другого проекта.

Готовый шаблон находится в
`examples/gigacode-venv-stdio-settings.example.json`.

### Linux или macOS

```json
{
  "mcpServers": {
    "corporate-kb": {
      "command": "/ABSOLUTE/PATH/corporate-kb-client/venv/bin/python",
      "args": [
        "/ABSOLUTE/PATH/corporate-kb-client/corporate_kb_stdio_proxy.py"
      ],
      "env": {
        "CORPORATE_KB_MCP_URL": "http://SERVER_IP:8000/mcp",
        "CORPORATE_KB_MCP_TOKEN": "",
        "CORPORATE_KB_MCP_TIMEOUT": "120",
        "CORPORATE_KB_SSOT_TEMP_DIR": "/ABSOLUTE/PATH/corporate-kb-ssot"
      },
      "timeout": 120000,
      "trust": false
    }
  }
}
```

### Windows

Используйте прямые `/` в JSON:

```json
{
  "mcpServers": {
    "corporate-kb": {
      "command": "C:/ABSOLUTE/PATH/corporate-kb-client/venv/Scripts/python.exe",
      "args": [
        "C:/ABSOLUTE/PATH/corporate-kb-client/corporate_kb_stdio_proxy.py"
      ],
      "env": {
        "CORPORATE_KB_MCP_URL": "http://SERVER_IP:8000/mcp",
        "CORPORATE_KB_MCP_TOKEN": "",
        "CORPORATE_KB_MCP_TIMEOUT": "120",
        "CORPORATE_KB_SSOT_TEMP_DIR": "C:/ABSOLUTE/PATH/corporate-kb-ssot"
      },
      "timeout": 120000,
      "trust": false
    }
  }
}
```

После изменения полностью закройте GigaCode, запустите его заново и выполните `/mcp`.

`CORPORATE_KB_MCP_URL` должен указывать на общий сервер, а не на компьютер сотрудника. `localhost`
подходит только если сервер запущен на той же машине или к нему настроен SSH/VPN tunnel.
`CORPORATE_KB_SSOT_TEMP_DIR` необязателен: без него proxy использует системный temp-каталог
`corporate-kb-ssot/<session-id>/<service-id>.md`.

## Проверить доступ к удалённой базе

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

## 5. Запустить GigaCode

Полностью закройте ранее запущенный GigaCode, затем запустите снова:

```bash
gigacode
```

Откройте:

```text
/mcp
```

Сервер `corporate-kb` должен показать все инструменты, которые сейчас отдаёт общий сервер. В
текущей версии среди них есть:

```text
kb_feature_context
kb_system_graph
kb_generate_system_ssot
kb_save_and_upload_ssot
kb_search
kb_get_document
kb_get_chunk
kb_run_context_benchmark
kb_list_documents
kb_stats
ssot_context
```

Перед реализацией межсервисной фичи вызывайте `kb_system_graph`. Минимальный аргумент — текст
фичи; если исходный сервис известен, передайте `start_service`. Tool проходит входящие и исходящие
связи (по умолчанию два hop), показывает протокол, операцию и evidence, но не обращается к RAG.
Затем клиент выполняет возвращённые `next_calls` к `kb_search_index` только для выбранных сервисов.
Поле `invocation_contexts` означает статически восстановленный trigger/handler, а не наблюдённый
runtime trace.

`kb_search` возвращает короткие выдержки, а не полные страницы Confluence. Если GigaCode нужен текст
конкретного результата, он сам вызывает `kb_get_chunk` с `chunk_id` — это сохраняет контекст диалога.

### Создать SSOT клиентской нейронкой

Попросите GigaCode/GigaCode: «Создай SSOT для выбранного сервиса через corporate-kb и загрузи его в
индекс». Модель должна выполнить protocol, который возвращает сам tool:

1. вызвать `kb_generate_system_ssot` с `action=options` и показать доступные индексы и repositories;
2. если repository отсутствует — запросить Git URL и вызвать `action=clone`, затем опрашивать
   `action=status`;
3. после выбора repository/service вызвать `action=prepare` и дождаться статуса `completed`;
4. для каждого target вызвать `action=context`, изучить analysis и manifest, а недостающие файлы
   получать через `action=read_file`;
5. самостоятельно создать evidence-backed Markdown и вызвать локальный
   `kb_save_and_upload_ssot`; для нескольких targets передавать `finalize=false`, а для последнего
   `finalize=true`.

Никакой URL нейронки на RAG-сервере не задаётся. `kb_save_and_upload_ssot` сначала атомарно пишет
файл в `CORPORATE_KB_SSOT_TEMP_DIR` (или системный temp), и лишь затем отправляет тот же Markdown в
server session. Если upload завершится ошибкой, локальная копия не удаляется.

`kb_run_context_benchmark` — отдельный административный прогон. При его вызове GigaCode должен сначала
спросить отдельный benchmark-пароль. Не сохраняйте этот пароль в GigaCode settings и не используйте
вместо него обычный `CORPORATE_KB_MCP_TOKEN`.

Администратор может добавлять built-in и управляемые tools на общем сервере. Proxy не содержит их
список: при старте он делает настоящий MCP `tools/list`, переносит имена, описания, JSON Schema и
annotations, а `tools/call` прозрачно отправляет обратно на сервер. Чтобы клиент перечитал новый
набор, полностью перезапустите GigaCode. Заменять `corporate_kb_stdio_proxy.py` при добавлении tools не
нужно. Не добавляйте `includeTools` в settings — статический allowlist скроет новые tools.

Кнопка OAuth-аутентификации не нужна. В стандартном режиме токена нет; при включённой серверной
защите Bearer-токен передаётся локальному Python-процессу через GigaCode settings.

Тестовый запрос:

```text
Используй corporate-kb. Найди информацию о дневных лимитах и укажи использованные источники.
```

## Что установлено на клиенте

Все Python-зависимости находятся только в каталоге `venv`. `requirements.txt` фиксирует
`FastMCP==3.4.4`, а обычный `pip` устанавливает совместимые зависимости. После установки GigaCode
запускает готовый `venv/bin/python` и ничего не скачивает при старте.

Если корпоративный компьютер использует внутренний Python package index, он должен содержать
FastMCP и его зависимости либо `pip` должен быть настроен администратором на этот index.

## Диагностика

### GigaCode не видит MCP

- Проверьте абсолютные пути к `venv/bin/python` или `venv/Scripts/python.exe` и к `.py`-файлу.
- Проверьте JSON на лишние запятые.
- Полностью перезапустите GigaCode после изменения settings.

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

### pip не может установить FastMCP

Проверьте настройки корпоративного Python package index. При необходимости администратор должен
выдать URL и сертификат внутреннего PyPI mirror. Документы и индекс при этом не скачиваются.

### Поиск работает через curl, но не через GigaCode

Запустите файл вручную той же командой из settings:

```bash
cd /ABSOLUTE/PATH/corporate-kb-client
source venv/bin/activate
CORPORATE_KB_MCP_URL=http://SERVER_IP:8000/mcp \
  python corporate_kb_stdio_proxy.py
```

Процесс должен ожидать MCP-сообщения в stdin. Ошибки конфигурации выводятся в stderr. Остановить
ручной запуск можно через `Ctrl+C`.

## Обновление и удаление

При появлении новых server tools обновление файла не требуется — достаточно перезапустить GigaCode.
Сам `corporate_kb_stdio_proxy.py` нужно заменить, если обновилась proxy-версия или добавился
client-local tool (как `kb_save_and_upload_ssot`), затем полностью перезапустить GigaCode.

Для удаления удалите объект `corporate-kb` из `mcpServers`, затем каталог клиента вместе с `venv`.
