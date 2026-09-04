# Graph Lab

`graph-lab` — это испытательный контур алгоритма графа, а не второй production-граф.
Он фиксирует входные репозитории и commit-ы, запускает выбранную реализацию, проверяет
явные ожидания и сохраняет всё необходимое для разбора ошибки или повторного запуска.

## Быстрый цикл

1. Скопируйте `cases/CASE.example.yaml` в `cases/<имя>/CASE.yaml`.
2. Укажите репозитории, сервисы и связи, существование которых известно заранее.
3. Запустите только статику:

   ```bash
   gigacode-graph debug run graph-lab/cases/<имя>/CASE.yaml --mode static
   ```

4. Посмотрите `run.json`, `validation.json`, `report.md` и `static-graph.json` в
   напечатанной директории `graph-lab/runs/<run-id>`.
5. Если статика оставила кандидаты неразрешёнными, запустите тот же case с моделью:

   ```bash
   gigacode-graph debug run graph-lab/cases/<имя>/CASE.yaml --mode gigacode
   ```

6. Для потерянной связи спросите, на каком шаге она исчезла:

   ```bash
   gigacode-graph debug explain-missing graph-lab/runs/<run-id>/final-graph.json \
     --source order-orchestrator --target payment-service --protocol HTTP \
     --operation /payments
   ```

7. Создайте ремонтную задачу и только отдельной явной командой разрешите модели писать:

   ```bash
   gigacode-graph debug prepare-repair graph-lab/runs/<run-id>
   gigacode-graph debug repair graph-lab/tasks/<task-id> --allow-write
   ```

Модель работает в отдельном Git worktree. После неё автоматически запускаются pytest,
Ruff и mypy. Результат — `changes.patch`; текущая ветка не меняется. Применение требует
отдельного `debug apply-repair <patch> --yes`. Commit и push CLI не делает.

Два run-а (включая wall time и peak process RSS) сравниваются так:

```bash
gigacode-graph debug compare graph-lab/runs/<baseline> graph-lab/runs/<candidate>
```

## Что находится в одном run

| Файл | Что отвечает на вопрос |
|---|---|
| `case.yaml` | Что просили проверить? |
| `replay-case.yaml` | На каких точных commit-ах повторять статику? |
| `run.json` | Какая версия алгоритма, лимиты, окружение, время и итог? |
| `events.jsonl` | Какой этап шёл и где остановился? |
| `repositories.json` | Что было клонировано/переиспользовано и какой HEAD анализировался? |
| `static-graph.json` | Что доказал детерминированный анализ до модели? |
| `candidates.json` | Какие зависимости увидела статика и какие из них отданы GigaCode? |
| `static-validation.json` | Какие ожидания не выполнила именно статика? |
| `gigacode/*/prompt.txt` | Точный запрос конкретного batch-а к модели. |
| `gigacode/*/schema.json` | JSON Schema ответа модели. |
| `gigacode/*/stdout.jsonl` | Сырые stream-json события GigaCode. |
| `gigacode/*/stderr.log` | Ошибки процесса, авторизации и инструментов. |
| `gigacode/*/result.json` | Нормализованный ответ и способ его извлечения. |
| `verification.json` | Какие кандидаты подтверждены, отклонены, переназначены или найдены. |
| `final-graph.json` | Финальный snapshot после optional verification. |
| `validation.json` | Контрактные ошибки и расхождения с CASE. |
| `report.md` | Короткий итог для человека. |

Удалённые репозитории клонируются в `graph-lab/.state/repositories` и по умолчанию
удаляются только после завершения всего алгоритма и записи артефактов. Локальные checkout-ы
никогда не удаляются. Для ручного исследования есть `--keep-checkouts`.

## Версии алгоритма

Исполняемый реестр находится в `gigacode_graph.algorithms.registry`. Встроенный алгоритм
`static-v2` — обычная реализация общего Python-контракта. Отдельный пакет может добавить
реализацию через entry point `corporate_kb.graph_algorithms`; команда
`gigacode-graph algorithm list` покажет, что реально установлено.

`ALGORITHMS.yaml` хранит жизненный цикл версий и evidence runs. `algorithm promote` не
принимает run с ошибками validation. Stage `production` меняет поле `active` в документе,
но production-конфигурация всё равно выбирает алгоритм явно через
`GIGACODE_GRAPH_BUILDER_ALGORITHM` или поле `algorithm` rebuild API — это защищает от
неожиданного переключения сервера одним редактированием документа.
