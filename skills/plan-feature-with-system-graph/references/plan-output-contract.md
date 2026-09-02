# PLAN_OUTPUT_V1

Use this contract for every planning response produced by
`plan-feature-with-system-graph`. Its purpose is to keep the result predictable for business
reviewers, engineers, the independent verifier, and Jira publication.

## Invariants

- Return one Markdown document and no preamble before its title.
- Emit all 12 numbered sections in the exact order below. Do not omit, merge, rename, or insert
  sections.
- Preserve the uppercase section IDs, field order, table columns, enum values, and identifier
  prefixes. Translate only the human-facing Russian labels when the user's language is not Russian.
- Use `Не применимо`, `Не подтверждено`, or `Нет данных` instead of removing a field or section.
  When a required table has no real rows, emit one row whose substantive cell says `Нет` and whose
  remaining cells contain `—`.
- Do not add numeric confidence scores or percentages. Preserve graph confidence only when a tool
  supplied the enum `HIGH`, `MEDIUM`, `LOW`, or `UNRESOLVED`.
- Keep Section 1 short enough to read without the engineering sections. It may summarize service
  names and impact, but must not introduce facts absent from Sections 4, 5, and 11.
- Put detailed technical material in Sections 5–11. Do not expose raw tool transcripts, hidden
  reasoning, or irrelevant retrieval results.
- Reuse stable IDs throughout the document:
  - `E-001` for evidence;
  - `CHG-001` for a required change;
  - `AC-001` for an acceptance criterion;
  - `R-001` for a risk or conflict;
  - `U-001` for an unknown or unresolved decision;
  - `J-001` for a Jira draft or published issue.
- Assign IDs in first-appearance order and never reuse an ID for a different item. Every material
  change must reference evidence or an explicit requirement, every acceptance criterion must trace
  to at least one change, and every confirmed service must have exactly one Jira row.
- Use only the enums defined below. Do not invent synonyms.

## Status vocabulary

### Plan status

- `READY_FOR_REVIEW`: all critical routes and current-state claims have adequate evidence; remaining
  gaps are non-blocking.
- `NEEDS_DECISIONS`: a plan can be reviewed, but named business or engineering decisions must be
  resolved before implementation or Jira publication.
- `BLOCKED`: mandatory tooling, source coverage, service identity, or a critical contract route is
  unavailable or contradictory; the output is informative but not safe to publish or implement.

### Statement type

- `CONFIRMED`: current-system fact supported by evidence.
- `REQUIREMENT`: supplied by the user or source requirement; not proof of current implementation.
- `PROPOSED`: future-state change recommended by the plan.
- `HYPOTHESIS`: plausible but insufficiently evidenced statement, including `LOW` or `UNRESOLVED`
  graph routes.
- `CONFLICT`: authoritative sources or tools disagree.

### Service status

- `CONFIRMED`: evidence establishes that the service is affected.
- `CANDIDATE`: the service may be affected but needs verification; it must not receive a Jira task.
- `EXCLUDED`: checked and excluded from the current plan, with evidence or a bounded explanation.

### Jira status

Document level:

- `DRAFT`: drafts exist, but publication capability or target discovery has not finished.
- `WAITING_FOR_TARGET`: drafts and writable targets are known; the user must select exactly one.
- `PUBLISHED`: every confirmed service maps to a verified created or reused Task.
- `PARTIAL`: at least one Task is verified and at least one Task failed or remains ambiguous.
- `BLOCKED`: no Jira write is safe because a required Jira capability, target, or assignee is missing.

Row level:

- `DRAFT`, `CREATED`, `REUSED`, `FAILED`, or `BLOCKED`.

## Required template

Replace angle-bracket placeholders with evidence-backed content. Placeholder text must not appear in
the final response.

```markdown
# План фичи: <краткое название>

| Поле | Значение |
|---|---|
| Формат | `PLAN_OUTPUT_V1` |
| Статус плана | `READY_FOR_REVIEW` / `NEEDS_DECISIONS` / `BLOCKED` |
| Корневой сервис | `<service_id>` / `Не подтверждено` |
| Подтверждённых сервисов | `<число>` |
| Сервисов-кандидатов | `<число>` |
| Критических неизвестных | `<число>` |
| Graph revision | `<revision>` / `Нет данных` |
| Jira | `DRAFT` / `WAITING_FOR_TARGET` / `PUBLISHED` / `PARTIAL` / `BLOCKED` |

## 1. BUSINESS_SUMMARY — Резюме для бизнеса

| Вопрос | Ответ |
|---|---|
| Что меняем | `<2–3 предложения без деталей реализации>` |
| Зачем | `<бизнес-проблема и ожидаемый результат>` |
| Кто получит результат | `<акторы или группы пользователей>` |
| Что затрагивается | `<процессы, системы и команды>` |
| Главные риски | `<не более трёх R-* или Нет>` |
| Требуемое бизнес-решение | `<решение, U-* или Не требуется>` |

## 2. SCOPE — Границы фичи

| Категория | Содержание | Основание |
|---|---|---|
| В scope | `<что входит>` | `REQUIREMENT` / `PROPOSED` |
| Не входит в scope | `<что сознательно не делаем>` | `REQUIREMENT` / `PROPOSED` |
| Ограничения | `<нефункциональные и организационные ограничения>` | `<тип>` |

## 3. TARGET_FLOW — Сквозной целевой сценарий

1. `<актор>` выполняет `<действие>`.
2. `<service_id>` принимает `<запрос или событие>`.
3. `<service_id>` выполняет `<действие>` и вызывает `<контракт>`.
4. `<service_id>` обрабатывает `<контракт>`.
5. `<актор>` получает `<ожидаемый результат>`.

Каждый шаг помечается как `REQUIREMENT` или `PROPOSED`. Текущие элементы внутри шага должны иметь
ссылку на `E-*`; неподтверждённый переход должен ссылаться на `U-*`.

## 4. SERVICE_MAP — Карта затронутых сервисов

| № | Сервис | Репозиторий | Роль в сценарии | Направление зависимости | Статус | Почему затронут | Evidence |
|---|---|---|---|---|---|---|---|
| 1 | `<service_id>` | `<repository>` | `<роль>` | `IN` / `OUT` / `BOTH` / `Нет данных` | `CONFIRMED` / `CANDIDATE` / `EXCLUDED` | `<причина>` | `E-001` / `U-001` |

## 5. ENGINEERING_PLAN — План изменений по сервисам

### 5.1 `<service_id>` — `<repository>`

| Поле | Значение |
|---|---|
| Роль в целевом сценарии | `<роль>` |
| Текущее состояние | `<только CONFIRMED-факты>` |
| Входящие зависимости | `<service_id и контракт или Нет>` |
| Исходящие зависимости | `<service_id и контракт или Нет>` |
| Evidence текущего состояния | `<E-*>` |

| ID | Область | Действие | Что изменить | Тип | Основание |
|---|---|---|---|---|---|
| `CHG-001` | `DOMAIN` / `API` / `KAFKA` / `DATA` / `CONFIG` / `SECURITY` / `OBSERVABILITY` / `DOCS` | `CREATE` / `CHANGE` / `REMOVE` | `<конкретное изменение>` | `REQUIREMENT` / `PROPOSED` | `<E-* или формулировка требования>` |

| Обязательная область | Результат |
|---|---|
| Domain logic | `<изменение или Не применимо>` |
| API | `<изменение или Не применимо>` |
| Kafka | `<изменение или Не применимо>` |
| Persistence / migration | `<изменение или Не применимо>` |
| Configuration | `<изменение или Не применимо>` |
| Security | `<изменение или Не применимо>` |
| Observability | `<изменение или Не применимо>` |
| Documentation | `<изменение или Не применимо>` |

**Тесты сервиса:** `<AC-* и уровни тестов>`

**Порядок rollout:** `<шаги или Не применимо>`

**Неизвестные:** `<U-* или Нет>`

Повторить подраздел 5.N для каждого `CONFIRMED`-сервиса в порядке Section 4. Для `CANDIDATE` и
`EXCLUDED` отдельный план не создавать.

## 6. CONTRACTS_AND_DATA — Контракты и данные

| Change ID | Тип | Producer / Caller | Consumer / Callee | Текущее состояние | Требуемое изменение | Совместимость и миграция | Evidence / Unknown |
|---|---|---|---|---|---|---|---|
| `CHG-001` | `HTTP` / `KAFKA` / `SCHEMA` / `DB` | `<service_id>` | `<service_id>` | `<CONFIRMED-факт или Не подтверждено>` | `<PROPOSED-изменение>` | `<стратегия или Не применимо>` | `<E-* или U-*>` |

## 7. DELIVERY_SEQUENCE — Последовательность реализации и rollout

| Шаг | Сервис | Изменения | Зависит от | Условие перехода | Rollback / compatibility |
|---|---|---|---|---|---|
| 1 | `<service_id>` | `<CHG-*>` | `Нет` | `<проверяемое условие>` | `<действие>` |

## 8. ACCEPTANCE_AND_TESTS — Критерии приёмки и тесты

| ID | Критерий приёмки | Уровень теста | Сервис / контур | Проверяет изменения | Условие успеха |
|---|---|---|---|---|---|
| `AC-001` | `<наблюдаемый результат>` | `UNIT` / `CONTRACT` / `INTEGRATION` / `MIGRATION` / `E2E` | `<service_id или контур>` | `<CHG-*>` | `<однозначная проверка>` |

## 9. RISKS_AND_UNKNOWNS — Риски, конфликты и неизвестные

| ID | Тип | Описание | Влияние | Требуемое действие | Владелец решения | Статус |
|---|---|---|---|---|---|---|
| `R-001` / `U-001` | `RISK` / `CONFLICT` / `UNKNOWN` | `<описание>` | `<влияние>` | `<проверка или решение>` | `<роль или Не определён>` | `OPEN` / `RESOLVED` |

## 10. JIRA — Черновики и публикация задач

**Статус Jira:** `<document-level Jira status>`

**Целевой проект:** `<name + stable key/ID или Не выбран>`

**Assignee:** `<authenticated principal + account ID или Не подтверждено>`

| ID | Сервис | Summary | Assignee | Jira-проект | Jira key | URL | Статус |
|---|---|---|---|---|---|---|---|
| `J-001` | `<service_id>` | `[<service_id>] <feature title>` | `<principal>` | `<project>` | `<key или —>` | `<URL или —>` | `DRAFT` / `CREATED` / `REUSED` / `FAILED` / `BLOCKED` |

**Проверка публикации:** `<destination, type, assignee и marker проверены / Не выполнялась>`

**Требуемое действие пользователя:** `<один точный вопрос или Не требуется>`

## 11. EVIDENCE_COVERAGE — Источники и покрытие

| Evidence | Что подтверждает | Источник | Ревизия / документ | Получено через |
|---|---|---|---|---|
| `E-001` | `<утверждение>` | `<source_path или source_url>` | `<revision, document ID или chunk ID>` | `<MCP tool>` |

| Проверка покрытия | Результат |
|---|---|
| Graph revision и analysis mode | `<значение>` |
| Индексы и свежесть | `<index_id + состояние>` |
| Использованные tools | `<список>` |
| Недоступные обязательные tools | `<список или Нет>` |
| Конфликты источников | `<R-* или Нет>` |

## 12. NEXT_ACTION — Следующее действие

1. `<первое обязательное решение или проверка>`;
2. `<следующий инженерный шаг>`;
3. запустить `$verify-cross-service-feature` для независимого аудита.

**Блокирующий вопрос:** `<ровно один вопрос или Нет>`
```

## Completion rules

- The header counts must equal the actual Section 4 and Section 9 rows. Count only `CONFIRMED`
  services as confirmed; do not count `EXCLUDED` as candidates.
- Section 4 is the canonical service set. The order of confirmed services there controls Sections 5
  and 10.
- Section 5 must contain one subsection for every confirmed service and no subsection for candidates
  or excluded services.
- Section 6 contains only contracts or data changes referenced by a `CHG-*` item. If there are none,
  use the required empty-row convention.
- Section 8 must trace every `CHG-*` to at least one `AC-*`. If a change cannot yet have a test,
  create a `U-*` explaining why and set the plan to at least `NEEDS_DECISIONS`.
- Section 10 always exists. Before publication it contains drafts and no invented Jira key or URL.
  After publication update the same rows and document status; do not append a second Jira section or
  rewrite the rest of the plan unless evidence or scope changed.
- If the target project is not selected and Jira can safely publish, use `WAITING_FOR_TARGET` and ask
  exactly: **“В какое Jira-пространство или проект создать эти задачи?”** Include available stable
  keys/IDs immediately before the question when exposed by the connector.
- Recommend implementation only for `READY_FOR_REVIEW`. For `NEEDS_DECISIONS`, make the unresolved
  decisions the first items in Section 12. For `BLOCKED`, state the restoring action first and do not
  publish Jira tasks.
- Run a final structural check before responding: 12 sections present, IDs unique, counts consistent,
  confirmed-service coverage complete, all changes traced to acceptance criteria, and Jira rows
  aligned with confirmed services.
