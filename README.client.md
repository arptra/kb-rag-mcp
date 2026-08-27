# Подключение GigaCode к общему RAG/MCP-серверу

На клиентской машине нужен только установленный GigaCode. Python, `venv`, FastMCP, `npx`,
`mcp-remote` и локальный stdio-процесс больше не используются.

```text
GigaCode ── Streamable HTTPS ──> https://RAG-SERVER:8000/mcp
                                      ├── живой tools/list
                                      ├── RAG-индексы
                                      └── граф и SSOT workflow
```

Сервер использует обычный one-way TLS: он показывает свой сертификат, но не запрашивает клиентский
сертификат, Bearer-токен или пароль. Все новые MCP tools появляются после повторного discovery в
GigaCode; клиентские файлы обновлять не требуется.

## Настройка GigaCode

Откройте пользовательский файл:

- Linux/macOS: `~/.gigacode/settings.json`;
- Windows: `%USERPROFILE%\.gigacode\settings.json`.

Добавьте сервер в существующий объект `mcpServers`, не удаляя остальные настройки:

```json
{
  "mcpServers": {
    "corporate-kb": {
      "httpUrl": "https://RAG-SERVER.EXAMPLE.COM:8000/mcp"
    }
  }
}
```

Готовый шаблон: `examples/gigacode-settings.example.json`. После сохранения полностью перезапустите
GigaCode и выполните `/mcp`.

`localhost` подходит только тогда, когда RAG-сервер запущен на той же машине. Обычно здесь должно
быть DNS-имя или IP общей серверной машины.

## Требование к сертификату

Чтобы в `settings.json` действительно оставался только URL, сертификат сервера должен быть выпущен
центром сертификации, которому доверяет клиентская ОС, и содержать DNS/IP из URL. Администратор
кладёт цепочку и ключ на RAG-сервер:

```text
certs/server.crt
certs/server.key
```

Локальный self-signed сертификат, автоматически созданный скриптом запуска, предназначен только для
разработки. Для проверки через `curl` можно временно использовать `-k`, но GigaCode-клиентам лучше
установить `server.crt` в системное доверенное хранилище или заменить его корпоративным сертификатом.
Параметр `trust` в MCP-конфигурациях не следует считать отключением TLS-проверки.

## Проверка сервера

С клиентской машины:

```bash
curl https://RAG-SERVER.EXAMPLE.COM:8000/health
```

Настоящий MCP initialize:

```bash
curl -i --max-time 15 \
  'https://RAG-SERVER.EXAMPLE.COM:8000/mcp' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"curl-test","version":"1.0"}}}'
```

Ожидается `HTTP 200`. Ошибка сертификата означает, что имя в URL отсутствует в SAN сертификата или
его CA ещё не установлен на клиентской машине.

## SSOT через прямой HTTP MCP

`kb_generate_system_ssot` полностью работает без stdio-proxy:

1. `action=options` — выбрать индекс и сервисы;
2. `action=prepare` — подготовить анализ;
3. `action=context` и `action=read_file` — получить необходимые исходники;
4. вызывающая модель формирует Markdown;
5. `action=submit` — напрямую загружает SSOT на сервер и обновляет индекс.

Временный файл на клиентской машине больше не создаётся: итоговый SSOT сразу сохраняется на общем
RAG-сервере.
