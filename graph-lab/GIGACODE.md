# Как алгоритм использует GigaCode CLI

Для verification сервер запускает исполняемый файл из `KB_GIGACODE_COMMAND` (по умолчанию
`gigacode`) с аргументами:

```text
gigacode --output-format stream-json \
  --exclude-tools shell,write,edit,agent,web_fetch,web_search \
  --max-session-turns <KB_GIGACODE_MAX_SESSION_TURNS>
```

Рабочая директория процесса — checkout одного репозитория. Prompt передаётся через stdin.
В него входят каталог сервисов и максимум 25 dependency candidates текущего batch-а, а также
JSON Schema ответа. Модель имеет только read-only инструменты. Supervisor ограничивает wall
time, умеет ждать browser authentication, убивает process group при cancel/timeout и читает
stdout/stderr параллельно, чтобы pipe не заблокировался.

Путь данных:

1. Static scanner создаёт EntryPoint, ExitPoint и `DEPENDS_ON` с evidence/confidence.
2. Verifier выбирает unresolved/LOW кандидаты; `verify_all` дополнительно включает discovery.
3. GigaCode читает только нужные source files и возвращает строгий JSON.
4. Pydantic проверяет форму JSON.
5. Verifier отдельно проверяет каждый путь, номер строки, source ownership, target id и
   совместимость HTTP/Kafka contract.
6. Валидное решение меняет confidence/status/origin; невалидное пишется в warnings, static
   graph сохраняется.
7. Graph Lab сохраняет prompt/schema/raw stream/result по каждому batch-у.

Repair-mode — другая команда и другие права. Она запускается только после явного
`debug repair --allow-write`, разрешает local shell/write/edit в изолированном worktree,
проверяет изменённые пути и возвращает patch. Verification никогда не получает эти права.
