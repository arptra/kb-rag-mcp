# Общий SSOT-индекс сервисов

Этот режим решает одну задачу: Qwen делает один вызов `ssot_context`, а RAG сам находит все
относящиеся к вопросу сервисы и возвращает один компактный межсервисный контекст.

```text
SSOT всех сервисов в ssot/
        ↓
один отдельный индекс .cache/ssot
        ↓
один MCP tool ssot_context
        ↓
один сгруппированный ответ для Qwen
```

Обычные документы и Confluence продолжают использовать `knowledge/` и `.cache/kb`. Они не
смешиваются с SSOT-индексом.

## 1. Положить SSOT сервисов в одну папку

Структура вложенных папок не важна. Например:

```text
ssot/
└── services/
    ├── payments-service.md
    ├── limits-service.md
    └── customer-service.md
```

В начало каждого существующего SSOT добавьте короткий YAML-заголовок:

```markdown
---
document_type: ssot
service: payments-service
domain: payments
status: current
commit_sha: REPLACE_WITH_GIT_COMMIT
---

# Payments Service

Существующий текст SSOT без переписывания...
```

Обязательны четыре поля:

- `document_type: ssot` отделяет SSOT от остальных документов;
- `service` содержит стабильное техническое имя сервиса;
- `status: current` исключает старые версии из актуальных ответов;
- `commit_sha` показывает, из какой Git-версии получены сведения.

Если один SSOT упоминает техническое имя другого сервиса, например `limits-service`, RAG использует
это как связь и сам добавляет второй сервис в контекст.

## 2. Построить отдельный индекс

```bash
./scripts/dev.sh index-ssot
```

Команда читает только `ssot/` и сохраняет индекс в `.cache/ssot`. При повторном запуске embeddings
неизменившихся разделов переиспользуются.

## 3. Включить SSOT на сервере

```bash
export KB_SSOT_ENABLED=true
export KB_SSOT_KNOWLEDGE_DIR=/opt/corporate-kb/ssot
export KB_SSOT_CACHE_DIR=/opt/corporate-kb/.cache/ssot

./scripts/start-mcp-http.sh
```

При старте в логе должна появиться строка `Preloaded global SSOT index`.

## 4. Проверить до подключения Qwen

```bash
KB_SSOT_ENABLED=true ./scripts/dev.sh ssot \
  'Как реализовать платёж с повторной проверкой дневного лимита?' \
  --mode implementation
```

Для бизнес-вопроса используйте:

```bash
KB_SSOT_ENABLED=true ./scripts/dev.sh ssot \
  'Какие сервисы участвуют в расчёте дневного лимита?' \
  --mode business
```

HTTP-проверка удалённого сервера:

```bash
curl -G 'http://SERVER_IP:8000/api/v1/ssot/context' \
  -H 'Authorization: Bearer SERVER_TOKEN' \
  --data-urlencode 'question=Как реализовать платёж с повторной проверкой лимита?' \
  --data-urlencode 'mode=implementation'
```

## Что получает Qwen

Один ответ содержит:

- затронутые сервисы;
- несколько релевантных фактов по каждому сервису;
- найденные упоминания связей между сервисами;
- `evidence_id`, путь и Git revision для проверки;
- `missing_information`, если актуальные SSOT не дают ответа.

Размер всего ответа ограничивает `KB_SSOT_CONTEXT_TOKENS`, по умолчанию 1000 условных токенов.
Qwen не должен повторять обычный `kb_search`: описание `ssot_context` прямо сообщает, что расширение
по связанным сервисам уже выполнено внутри RAG.

## Автоматическое обновление из Git

Первая версия не хранит Git credentials и не клонирует репозитории внутри HTTP-сервера. Надёжнее,
чтобы ваш Git CI после merge выполнял три действия:

1. Копировал актуальный SSOT в `ssot/services/<service>.md`.
2. Записывал commit merge в поле `commit_sha`.
3. Запускал `./scripts/dev.sh index-ssot`, после чего перезапускал сервис.

Так Git остаётся источником истины и истории, а RAG хранит только готовый актуальный поисковый
индекс. Следующий этап разработки — webhook/registry репозиториев, который автоматизирует эти три
операции без изменения формата SSOT и MCP tool.
