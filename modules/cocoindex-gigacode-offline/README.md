# CocoIndex Code + GigaCode: полностью локальный RAG по репозиторию

Это руководство описывает установку и эксплуатацию `cocoindex-code` на закрытом Linux-ноутбуке:

- без Docker;
- без OpenAI API key;
- без отправки исходного кода внешним LLM-провайдерам;
- с локальной embedding-моделью через `sentence-transformers`;
- с embedded-индексом в каталоге репозитория;
- с подключением к GigaCode через MCP.

Результат первой итерации — семантический и структурный поиск по Java/Spring-коду. GigaCode получает релевантные фрагменты через MCP и использует их для анализа точек входа, исходящих вызовов, контрактов, бизнес-правил и работы с БД.

## 1. Что здесь работает, а что нет

`cocoindex-code` умеет:

- читать локальный репозиторий;
- разбивать код по AST через Tree-sitter;
- строить embeddings локально;
- сохранять локальный индекс;
- обновлять только изменившиеся файлы;
- искать код по смыслу командой `ccc search`;
- выполнять структурный поиск командой `ccc grep`;
- отдавать поиск агенту через `ccc mcp`.

`cocoindex-code` не является точным статическим анализатором всей распределённой системы. Найденная похожесть кода не доказывает межсервисный вызов. Для доказанного call graph позднее понадобится отдельный слой, например SCIP Java и специализированный анализ Spring, Feign, Kafka, JPA и миграций.

Практическое разделение:

```text
CocoIndex Code              точный статический анализ
------------------------    ----------------------------
поиск релевантного кода     definitions/references
AST-фрагменты               вызовы методов
поиск бизнес-правил         REST/Feign/Kafka-связи
контекст для GigaCode       таблицы/сущности/миграции
```

## 2. Зафиксированный набор версий

Для воспроизводимой установки используем:

```text
Python          3.13.x
cocoindex-code  0.2.41
cocoindex       1.0.20
litellm         1.93.0
```

`cocoindex-code==0.2.41` требует:

```text
Python >= 3.11
cocoindex >= 1.0.17, < 1.1.0
```

`cocoindex==1.0.20` допускает `litellm>=1.81.0`, поэтому доступная во внутреннем registry версия `litellm==1.93.0` подходит. Если в банке уже разрешена `1.96.0`, её также можно использовать, но выбранную версию нужно фиксировать явно во всех командах установки.

## 3. Совместимость wheel с Python и Linux

Для обычного CPython 3.13 на Linux x86-64 подходит:

```text
cocoindex-1.0.20-cp311-abi3-manylinux_2_28_x86_64.whl
```

Значение тегов:

```text
cp311-abi3                    совместим с обычным CPython 3.11+
manylinux_2_28                требует glibc 2.28+
x86_64                        Intel/AMD 64-bit
```

Wheel ниже работает только с CPython 3.12 и не устанавливается на Python 3.13:

```text
cocoindex-1.0.20-cp312-cp312-manylinux_2_28_x86_64.whl
```

Проверка машины:

```bash
uname -m
ldd --version
python3.13 --version
```

Ожидается:

```text
x86_64
glibc 2.28 или новее
Python 3.13.x
```

Если доступен только `cp312-cp312`, необходимо создавать окружение через `python3.12`, а не переименовывать wheel.

Поддерживаемые текущим `pip` теги можно посмотреть так:

```bash
python -m pip debug --verbose
```

## 4. Создание чистого окружения

Создаём отдельный каталог, не смешанный с анализируемым Java-репозиторием:

```bash
mkdir cocoindex-test
cd cocoindex-test
python3.13 -m venv .venv
source .venv/bin/activate
```

Проверяем, что активирован правильный Python:

```bash
python --version
which python
python -m pip --version
```

`which python` должен указывать на:

```text
.../cocoindex-test/.venv/bin/python
```

Если `pip` отсутствует:

```bash
python -m ensurepip --upgrade
python -m pip --version
```

Если отсутствует сам модуль `venv`, потребуется системный пакет для выбранной версии Python, например `python3.13-venv`. На управляемом ноутбуке его устанавливает администратор.

Во всех дальнейших командах используется `python -m pip`, а не отдельная команда `pip`.

## 5. Проверка корпоративного Python registry

Посмотреть текущую конфигурацию:

```bash
python -m pip config list
```

Посмотреть доступные версии:

```bash
python -m pip index versions cocoindex-code
python -m pip index versions cocoindex
python -m pip index versions litellm
```

Если внутренний registry не прописан глобально, его передают явно:

```bash
python -m pip install \
  --index-url https://internal-pypi.example/simple \
  "litellm==1.93.0"
```

Нельзя указывать `--extra-index-url` на публичный PyPI: это разрешит `pip` обращаться во внешний контур.

## 6. Установка из внутреннего registry и локального wheel

Сначала фиксируем доступную версию LiteLLM:

```bash
python -m pip install "litellm==1.93.0"
```

Устанавливаем бинарный wheel CocoIndex:

```bash
python -m pip install \
  /absolute/path/cocoindex-1.0.20-cp311-abi3-manylinux_2_28_x86_64.whl
```

Устанавливаем `cocoindex-code` с локальным embedding backend:

```bash
python -m pip install \
  "litellm==1.93.0" \
  "cocoindex-code[full]==0.2.41"
```

`[full]` добавляет `sentence-transformers`, Transformers и локальный inference backend. Без `[full]` установка ориентирована на LiteLLM-провайдеры и не подходит для нашей полностью локальной схемы.

Перед фактической установкой resolver можно проверить так:

```bash
python -m pip install --dry-run \
  "litellm==1.93.0" \
  "cocoindex-code[full]==0.2.41"
```

## 7. Полностью локальная установка из wheelhouse

Если все зависимости заранее собраны в одном каталоге, установка не должна обращаться ни к одному registry:

```bash
python -m pip install \
  --no-index \
  --find-links /absolute/path/wheelhouse \
  "litellm==1.93.0" \
  "cocoindex==1.0.20" \
  "cocoindex-code[full]==0.2.41"
```

Ключи:

- `--no-index` полностью запрещает использование package index;
- `--find-links` задаёт каталог с разрешёнными wheels и архивами.

В wheelhouse должны лежать не только три верхнеуровневых пакета, но и все транзитивные зависимости.

## 8. Проверка установки

```bash
python -m pip check
python -m pip show cocoindex
python -m pip show cocoindex-code
python -m pip show litellm
ccc --version
```

Ожидаемые верхнеуровневые версии:

```text
cocoindex       1.0.20
cocoindex-code  0.2.41
litellm         1.93.0
```

Если `ccc` не находится, проверяем:

```bash
which python
ls -la .venv/bin/ccc
```

Запуск по абсолютному пути всегда возможен:

```bash
/absolute/path/cocoindex-test/.venv/bin/ccc --version
```

## 9. Зачем установлена LiteLLM

LiteLLM — унифицированный клиент для OpenAI, Voyage, Gemini, Ollama и других API. Она умеет отправлять данные внешним провайдерам, если настроить `provider: litellm`.

В нашей конфигурации LiteLLM установлена только потому, что входит в обязательные зависимости `cocoindex-code`. Для построения embeddings она не используется.

Разрешённая конфигурация:

```yaml
embedding:
  provider: sentence-transformers
  model: /absolute/path/to/local-model
  device: cpu
```

Запрещённая для закрытого контура конфигурация:

```yaml
embedding:
  provider: litellm
  model: openai/text-embedding-3-small
```

## 10. Локальная embedding-модель

Python-пакет `sentence-transformers` не содержит веса модели. Каталог модели должен быть доставлен во внутренний контур отдельно и полностью.

Пример каталога:

```text
/opt/models/snowflake-arctic-embed-xs/
├── config.json
├── modules.json
├── model.safetensors
├── tokenizer.json
├── tokenizer_config.json
└── ...
```

Проверка:

```bash
ls -la /opt/models/snowflake-arctic-embed-xs
```

Для Java-кода и русскоязычных запросов лучше использовать модель, проверенную на code retrieval и multilingual retrieval. Модель должна поддерживаться `sentence-transformers` и загружаться из локального каталога.

Если модели локально нет, `ccc index` не сможет построить embeddings в полностью закрытом контуре.

## 11. Строгий офлайн-режим

Перед `ccc init`, индексированием и запуском MCP задаём:

```bash
export COCOINDEX_DISABLE_USAGE_TRACKING=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1
```

Назначение:

- `COCOINDEX_DISABLE_USAGE_TRACKING=1` отключает usage telemetry CocoIndex;
- `HF_HUB_OFFLINE=1` запрещает Hugging Face Hub сетевые обращения;
- `TRANSFORMERS_OFFLINE=1` запрещает Transformers загружать артефакты;
- `HF_HUB_DISABLE_TELEMETRY=1` отключает телеметрию Hugging Face;
- `DO_NOT_TRACK=1` является дополнительным общим запретом для поддерживающих его библиотек.

Это необходимо передавать и MCP-процессу. Окончательная банковская гарантия обеспечивается исходящим firewall: переменные управляют штатным поведением библиотек, а firewall технически блокирует любой внешний трафик.

## 12. Инициализация Java-репозитория

Активируем установленное окружение и переходим в корень репозитория:

```bash
source /absolute/path/cocoindex-test/.venv/bin/activate
cd /absolute/path/to/java-service
```

Экспортируем offline-переменные из предыдущего раздела, затем запускаем:

```bash
ccc init
```

В мастере выбираем:

```text
Provider: sentence-transformers
Model:    /opt/models/snowflake-arctic-embed-xs
Device:   cpu
```

Глобальная конфигурация создаётся здесь:

```text
~/.cocoindex_code/global_settings.yml
```

Она должна выглядеть примерно так:

```yaml
embedding:
  provider: sentence-transformers
  model: /opt/models/snowflake-arctic-embed-xs
  device: cpu

daemon:
  idle_timeout_minutes: 180
```

В репозитории создаётся:

```text
.cocoindex_code/settings.yml
```

Пример настроек для Java/Spring:

```yaml
include_patterns:
  - "**/*.java"
  - "**/*.kt"
  - "**/*.kts"
  - "**/*.sql"
  - "**/*.xml"
  - "**/*.yaml"
  - "**/*.yml"
  - "**/*.properties"
  - "**/*.md"

exclude_patterns:
  - "**/.git/**"
  - "**/.idea/**"
  - "**/target/**"
  - "**/build/**"
  - "**/.gradle/**"
  - "**/generated/**"
  - "**/node_modules/**"
```

Не индексируем бинарные сборочные каталоги, generated sources и зависимости.

## 13. Диагностика перед индексацией

```bash
ccc doctor
```

Проверяем:

- локальная модель существует;
- выбран `sentence-transformers`;
- Java-файлы попадают под `include_patterns`;
- исключённые каталоги не индексируются;
- embedded SQLite/LMDB может быть создан;
- отсутствуют ошибки Python-зависимостей.

Если диагностика пытается скачать модель, значит указан Hugging Face ID вместо абсолютного локального пути либо offline-переменные не переданы процессу.

## 14. Построение индекса

Из корня Java-репозитория:

```bash
ccc index
```

Первый запуск читает весь разрешённый код и строит embeddings. Последующие запуски обрабатывают изменения инкрементально.

Проверяем результат:

```bash
ccc status
```

Локальные базы по умолчанию находятся в:

```text
<java-repository>/.cocoindex_code/cocoindex.db
<java-repository>/.cocoindex_code/target_sqlite.db
```

Docker, Milvus, Qdrant и отдельный сервер БД для этого режима не требуются.

После изменения модели необходима полная перестройка, потому что размерность embeddings может отличаться:

```bash
ccc reset
ccc index
```

## 15. Работа через CLI

Семантический поиск:

```bash
ccc search "где создаётся заказ"
ccc search "REST endpoint создания платежа"
ccc search "вызов внешнего сервиса через Feign"
ccc search "обработка Kafka события создания заказа"
ccc search "сохранение заказа через JPA repository"
ccc search "валидация бизнес-ограничений перед оплатой"
ccc search "миграция таблицы платежей"
```

Структурный поиск по синтаксису:

```bash
ccc grep '@RestController'
ccc grep '@FeignClient(A*)'
ccc grep '@KafkaListener(A*)'
ccc grep '@Scheduled(A*)'
ccc grep '@Entity'
```

После изменения исходников обновляем индекс:

```bash
ccc index
```

Состояние индекса:

```bash
ccc status
```

## 16. Запуск MCP вручную

Из корня уже инициализированного Java-репозитория:

```bash
/absolute/path/cocoindex-test/.venv/bin/ccc mcp
```

MCP работает по `stdio`, поэтому процесс просто ждёт JSON-RPC-команды. Это нормально. Для остановки используется `Ctrl+C`.

В MCP нельзя писать произвольные логи в stdout: stdout зарезервирован для протокола.

## 17. Подключение MCP к GigaCode

В MCP-конфигурацию GigaCode добавляем сервер. Конкретное расположение файла зависит от сборки GigaCode, но структура обычно такая:

```json
{
  "mcpServers": {
    "cocoindex-code": {
      "command": "/absolute/path/cocoindex-test/.venv/bin/ccc",
      "args": ["mcp"],
      "env": {
        "COCOINDEX_DISABLE_USAGE_TRACKING": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1"
      }
    }
  }
}
```

Критично:

- `command` содержит абсолютный путь к `ccc` из нужного `.venv`;
- GigaCode запускается из корня анализируемого репозитория;
- этот репозиторий уже прошёл `ccc init` и `ccc index`;
- offline-переменные передаются именно MCP-процессу;
- в конфигурации отсутствуют OpenAI/Voyage/Gemini ключи и внешние URL.

После изменения MCP-конфигурации перезапускаем GigaCode и проверяем список подключённых MCP tools штатной командой конкретной сборки GigaCode.

## 18. Проверка MCP из GigaCode

Первый тестовый запрос агенту:

```text
Используй только MCP cocoindex-code. Найди REST-контроллеры этого репозитория.
Для каждого результата укажи файл, класс, метод и доступный диапазон строк.
Не делай выводов без найденного фрагмента кода.
```

Второй тест:

```text
Через MCP cocoindex-code найди все места, связанные с созданием заказа.
Раздели результаты на REST-входы, бизнес-сервисы, исходящие вызовы,
события и сохранение в БД. Для каждого утверждения приведи файл и символ.
```

Если агент отвечает общими знаниями без файлов и символов, он не использовал индекс либо MCP не подключён.

## 19. Базовая инструкция для GigaCode

Эту инструкцию можно положить в системные/проектные инструкции GigaCode:

```text
В проекте подключён MCP-сервер cocoindex-code с локальным индексом репозитория.

Правила исследования кода:
1. Перед ответом о реализации, бизнес-логике или архитектуре сначала используй
   semantic search через cocoindex-code.
2. Делай несколько узких поисковых запросов вместо одного общего.
3. Для каждой связи указывай evidence: путь к файлу, класс/метод и строки,
   если они доступны в результате.
4. Разделяй подтверждённые факты и предположения.
5. Не объявляй межсервисный вызов доказанным только по семантической похожести.
   Ищи конкретный клиент, URL, topic, queue, schema, DTO или configuration property.
6. При недостатке данных явно перечисляй, что ещё необходимо найти.
7. Не отправляй исходный код, запросы, embeddings или результаты поиска во внешние сервисы.
8. Не изменяй код, пока пользователь явно не попросил реализовать изменение.

При анализе Java/Spring последовательно ищи:
- точки входа: RestController, Controller, KafkaListener, RabbitListener,
  Scheduled, CommandLineRunner;
- бизнес-логику: service/use-case классы, проверки, ветвления, исключения,
  транзакции и изменения состояния;
- исходящие взаимодействия: FeignClient, WebClient, RestTemplate,
  HTTP clients, KafkaTemplate, message producers;
- контракты: paths, HTTP methods, DTO, schemas, topics, headers и error models;
- данные: Entity, Table, Repository, JdbcTemplate, SQL, Flyway, Liquibase,
  datasource configuration и transaction boundaries.

Формат результата:
- краткое назначение;
- точки входа;
- последовательность выполнения;
- исходящие вызовы и события;
- чтение/запись данных;
- бизнес-правила;
- evidence;
- неизвестные или недоказанные связи.
```

## 20. Шаблон запроса на анализ сервиса

```text
Используй MCP cocoindex-code и исследуй текущий Java/Spring-сервис.

Нужно найти:
1. Все внешние точки входа.
2. Основные бизнес-сценарии и правила.
3. Исходящие REST/Feign/WebClient вызовы.
4. Kafka/Rabbit входы и выходы.
5. JPA entities, repositories, SQL, Flyway/Liquibase и datasource settings.
6. Связи между найденными компонентами.

Не ограничивайся одним поисковым запросом. Проверяй каждое утверждение отдельным
поиском. Для результата указывай файлы и символы. Отмечай confidence:
confirmed, probable или unknown. Не изменяй репозиторий.
```

## 21. Шаблон запроса на оценку бизнес-потребности

```text
Бизнес-потребность: <описание>.

Используй MCP cocoindex-code для исследования текущего репозитория.
Найди существующие точки входа, бизнес-правила, контракты, вызовы других систем,
события и таблицы, которых касается потребность.

Составь:
1. подтверждённый текущий flow;
2. вероятные точки изменения;
3. файлы и символы для каждой точки;
4. риски и неизвестные зависимости;
5. проверки и тесты, которые потребуются.

Не создавай Jira-задачи и PR, пока пользователь отдельно не подтвердит анализ.
```

## 22. Проверка отсутствия внешнего трафика

Основной контроль должен выполняться корпоративным firewall. Дополнительно на Linux можно посмотреть попытки сетевых соединений через `strace`, если он разрешён:

```bash
strace -f \
  -e trace=connect,sendto,recvfrom \
  -o /tmp/cocoindex-network.log \
  ccc search "создание заказа"
```

Затем:

```bash
less /tmp/cocoindex-network.log
```

Локальные IPC/loopback-соединения могут использоваться внутренним daemon. Соединений с публичными IP быть не должно.

## 23. Частые проблемы

### `pip: command not found`

```bash
python -m ensurepip --upgrade
python -m pip --version
```

Всегда используем `python -m pip`.

### `is not a supported wheel on this platform`

Проверяем:

```bash
python --version
uname -m
ldd --version
python -m pip debug --verbose
```

Для Python 3.13 нужен `cp311-abi3`, `cp313` либо совместимый `py3-none-any`. Wheel `cp312-cp312` предназначен только для Python 3.12.

### Resolver пытается выбрать недоступный LiteLLM

Фиксируем доступную версию в той же команде:

```bash
python -m pip install \
  "litellm==1.93.0" \
  "cocoindex-code[full]==0.2.41"
```

### Нет подходящей версии CocoIndex

`cocoindex-code==0.2.41` требует `cocoindex>=1.0.17,<1.1.0`. Если внутренний registry содержит только `1.0.13-1.0.16`, временно используется `cocoindex-code==0.2.39` либо во внутренний registry добавляется актуальный core wheel.

### `ccc` пытается скачать модель

Причины:

- в `global_settings.yml` указан model ID, а не локальный путь;
- путь не существует или каталог модели неполный;
- offline-переменные не переданы процессу;
- MCP запущен с другим окружением.

### После изменения модели поиск сломан

```bash
ccc reset
ccc index
```

### Индекс не видит новые файлы

```bash
ccc doctor
ccc index
ccc status
```

Проверяем `include_patterns` и `exclude_patterns`.

### MCP работает из терминала, но не из GigaCode

Проверяем:

- абсолютный путь к `.venv/bin/ccc`;
- рабочий каталог GigaCode;
- наличие `.cocoindex_code/settings.yml` в репозитории;
- offline `env` внутри MCP-конфигурации;
- перезапуск GigaCode после изменения конфигурации.

## 24. Минимальный ежедневный сценарий

```bash
source /absolute/path/cocoindex-test/.venv/bin/activate
cd /absolute/path/to/java-service

export COCOINDEX_DISABLE_USAGE_TRACKING=1
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export DO_NOT_TRACK=1

ccc doctor
ccc index
ccc search "нужная бизнес-логика"
```

Затем запускаем GigaCode из того же репозитория. MCP автоматически использует локальный индекс этого проекта.

## 25. Критерий готовности

Установка считается рабочей, когда одновременно выполнено всё:

- `python -m pip check` не сообщает конфликтов;
- `ccc doctor` проходит без критических ошибок;
- `ccc status` показывает проиндексированные Java-файлы и chunks;
- `ccc search` возвращает реальные файлы текущего репозитория;
- GigaCode видит MCP tool `cocoindex-code`;
- ответ GigaCode содержит evidence из репозитория;
- при отключённой сети индексирование и поиск продолжают работать;
- внешний firewall не фиксирует исходящих соединений от процесса.

