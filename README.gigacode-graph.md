# Repository Graph для GigaCode

Изолированный модуль `gigacode_graph` принимает Git URL или локальные checkout-ы и строит
evidence-backed граф Java/Spring-сервисов. Git URL автоматически клонируются в управляемый cache;
при следующем запуске тот же command обновляет их до нового commit. Один snapshot используется
тремя интерфейсами:

- read-only MCP для GigaCode CLI;
- обычный CLI для индексирования и отладки запросов;
- HTTP-сервер с JSON API и встроенным SVG UI.

Модуль не исполняет Maven/Gradle, исходный код сервисов или миграции. На первой итерации граф
хранится в понятном versioned JSON. Интерфейс `GraphStore` отделяет хранение, поэтому после проверки
метамодели его можно заменить Neo4j без изменения MCP tools и UI API.

## Что попадает в граф

```mermaid
flowchart LR
  Repo["Repository"] --> Service["Service"]
  Service --> Operation["BusinessOperation"]
  Operation --> Entry["EntryPoint: HTTP / Kafka / Scheduled"]
  Entry --> Symbol["CodeSymbol"]
  Symbol -->|CALLS| Symbol2["CodeSymbol"]
  Operation --> Exit["ExitPoint: HTTP / Kafka"]
  Exit --> Service2["Service / ExternalSystem"]
  Operation --> Rule["BusinessRule: raw if condition"]
  Operation -->|READS / WRITES| Table["Table"]
  Service --> Entity["DomainEntity"]
  Entity -->|MAPS_TO| Table
  Table --> Column["Column"]
  Service -->|PUBLISHES / CONSUMES| Event["Event / Kafka topic"]
  Service -->|DEPENDS_ON| Service2
```

Сканер извлекает:

- имя сервиса из `gigacode-graph.json`, `spring.application.name`, Maven `artifactId` или имени
  каталога;
- Spring MVC endpoints, Feign-клиенты, `@KafkaListener`, literal Kafka producers и `@Scheduled`;
- неглубокий Java call graph от точек входа;
- сырые условия `if` как кандидаты бизнес-правил;
- JPA entities, tables, columns и Spring Data repositories;
- таблицы из Flyway/Liquibase SQL, YAML и XML migrations;
- межрепозиторные HTTP и Kafka-зависимости.

У каждого извлечённого факта есть `evidence_id`, ведущий к repository, commit, file, line, snippet и
extractor. Выводы имеют `DECLARED`, `HIGH`, `MEDIUM`, `LOW` или `UNRESOLVED` confidence.

## Быстрый запуск

Требуется уже созданная `.venv` проекта с Python 3.12.

Для одного репозитория достаточно ссылки:

```bash
source .venv/bin/activate
python -m gigacode_graph.cli up \
  git@gitlab.company.ru:commerce/order-service.git
```

`up` выполняет весь pipeline и остаётся запущенным: clone/update → analysis → artifacts → UI +
MCP. После команды UI доступен на `http://127.0.0.1:8077/graph`, MCP — на
`http://127.0.0.1:8077/mcp`. Если сервер уже запущен отдельно, используйте неблокирующий `index` —
он только обновит repositories и artifacts, а сервер подхватит новый snapshot автоматически.

Несколько репозиториев также можно передать одной командой:

```bash
python -m gigacode_graph.cli index \
  https://gitlab.company.ru/commerce/order-service.git \
  https://gitlab.company.ru/warehouse/inventory-service.git \
  https://gitlab.company.ru/payments/payment-service.git
```

Повтор той же команды выполняет `git fetch`, переключает управляемый checkout на новый commit,
атомарно заменяет `graph.json` и записывает связанный ingestion inventory. Конкретная branch, tag
или commit задаётся через `--ref`:

```bash
python -m gigacode_graph.cli index \
  git@gitlab.company.ru:commerce/order-service.git \
  --ref release/2026.08
```

После этого доступны обычные запросы:

```bash

python -m gigacode_graph.cli stats
python -m gigacode_graph.cli services
python -m gigacode_graph.cli show order-service
python -m gigacode_graph.cli dependencies order-service --direction both --depth 2
python -m gigacode_graph.cli business order-service
python -m gigacode_graph.cli data-model --service order-service
python -m gigacode_graph.cli search "проверка лимита"
```

По умолчанию всё раскладывается автоматически:

```text
.cache/gigacode-graph/
├── repositories/       # управляемые shallow Git checkouts
├── graph.json          # versioned nodes, edges, evidence и issues
└── ingestion.json      # URL/ref → checkout → точный commit → graph snapshot
```

Другой путь графа задаётся `--store` или `GIGACODE_GRAPH_STORE_PATH`. При `--store` clone cache и
`ingestion.json` создаются рядом с указанным `graph.json`. Полный список переменных есть в
[`examples/gigacode-graph.env.example`](examples/gigacode-graph.env.example).

Приватные repositories используют стандартную Git-аутентификацию машины: SSH agent/key или Git
credential helper. Логин, пароль и token нельзя вставлять в HTTP URL — ingestion отклонит такую
ссылку, чтобы секрет не попал в `.git/config`, логи и artifacts. Для полностью автоматического
запуска credentials должны быть настроены заранее без interactive prompt.

Локальные checkout-ы по-прежнему поддерживаются и никогда не обновляются/изменяются индексатором:

```bash
python -m gigacode_graph.cli index /absolute/repos/order-service
```

Для большого списка репозиториев удобнее manifest:

```bash
python -m gigacode_graph.cli index \
  --manifest examples/gigacode-graph-repositories.example.json
```

Manifest принимает `url`, необязательный `ref` и локальный `path`; относительный `path` разрешается
относительно самого manifest-файла. В корне отдельного сервиса можно добавить точное описание
`gigacode-graph.json`:

```json
{
  "service": {
    "id": "order-service",
    "displayName": "Orders",
    "owner": "commerce-platform",
    "aliases": ["orders", "order-api"]
  }
}
```

Aliases нужны для связывания `@FeignClient(name = "orders")`, DNS hostname и имени репозитория с
одним service node.

## MCP для GigaCode CLI

После индексирования добавьте server entry из
[`examples/gigacode-graph-mcp.example.json`](examples/gigacode-graph-mcp.example.json) в настройки
MCP вашей версии GigaCode. Все пути должны быть абсолютными. Надёжный вариант запускает модуль
через Python из `.venv`:

```json
{
  "mcpServers": {
    "repository-graph": {
      "command": "/absolute/project/.venv/bin/python",
      "args": ["-m", "gigacode_graph.mcp_server"],
      "cwd": "/absolute/project",
      "env": {
        "PYTHONPATH": "/absolute/project/src",
        "GIGACODE_GRAPH_STORE_PATH": "/absolute/project/.cache/gigacode-graph/graph.json"
      }
    }
  }
}
```

GigaCode получает семь read-only tools:

- `code_graph_overview`;
- `code_graph_search`;
- `code_graph_service`;
- `code_graph_dependencies`;
- `code_graph_business_operations`;
- `code_graph_data_model`;
- `code_graph_evidence`.

Рекомендуемый порядок для задачи бизнеса: найти термины и операции, получить карточки найденных
сервисов, пройти зависимости, проверить модель данных, затем запросить evidence спорных связей.
Нельзя строить план изменения только по `LOW`/`UNRESOLVED` фактам.

## Сервер и UI

```bash
PYTHONPATH=src .venv/bin/python -m gigacode_graph.http_server
```

Откройте `http://127.0.0.1:8077/graph`. MCP Streamable HTTP доступен по
`http://127.0.0.1:8077/mcp`, JSON API — под `/api/*`, health check — `/health`.
Если CLI заменил `graph.json`, уже работающие MCP и UI загружают новый snapshot автоматически на
следующем запросе.

Локальный loopback по умолчанию работает без токена. Если задан
`GIGACODE_GRAPH_BEARER_TOKEN`, токен должен содержать не менее 32 символов и защищает MCP и JSON
API. При bind на адрес вне loopback сильный токен обязателен:

```bash
export GIGACODE_GRAPH_HTTP_HOST=0.0.0.0
export GIGACODE_GRAPH_BEARER_TOKEN="$(openssl rand -hex 32)"
PYTHONPATH=src .venv/bin/python -m gigacode_graph.http_server
```

В production TLS должен завершаться на корпоративном reverse proxy; сам процесс не реализует TLS.

## Жёсткие ограничения первой версии

Это архитектурный индекс, а не компилятор и не истина о runtime:

- Tree-sitter разбирает Java-структуру, но framework/source extractors не видят вызовы через
  reflection, generated code, dynamic proxies и сложный polymorphism;
- Spring profiles, conditional beans, service discovery, gateway rewrite и внешняя конфигурация
  могут менять реальный маршрут;
- динамически собранные URL и topic names будут неполными или `UNRESOLVED`;
- raw `if` — лишь кандидат бизнес-правила. Без доменной документации он не доказывает бизнес-смысл;
- JPA mapping не доказывает владение БД, а одинаковые таблицы в разных сервисах намеренно имеют
  разные IDs;
- snapshot показывает статически возможную связь, а не её частоту и не факт вызова в production.

Поэтому следующий правильный слой — сопоставление static graph с OpenTelemetry traces, Kafka
metadata, database catalog и SSOT/RAG. LLM-обогащение стоит добавлять отдельным контролируемым
этапом: модель предлагает summary и доменные теги, но не создаёт доказательные edges без source или
runtime evidence.

Автоматическое создание Jira-задач и pull requests в этот модуль не входит. До приемлемой полноты
и precision графа такой write-agent опасен: он быстро масштабирует ошибочный impact analysis. MCP
намеренно read-only.

## Проверка

```bash
PYTHONPATH=src .venv/bin/pytest -q tests/test_gigacode_graph.py
.venv/bin/ruff check src/gigacode_graph tests/test_gigacode_graph.py
.venv/bin/mypy src/gigacode_graph
```
