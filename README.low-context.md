# Быстрый RAG с уменьшенным контекстом

Этот режим предназначен для production-сервера с большой базой документов. Индекс строится
отдельной задачей, а HTTP-процесс только загружает готовый cache в RAM. На каждый поиск LLM получает
не найденные страницы целиком, а максимум две короткие выдержки.

## Что даёт ускорение

1. `KB_AUTO_INDEX=false` — сервер при запуске и поиске не обходит 11 000 исходных файлов.
2. Явная инкрементальная переиндексация — embeddings неизменившихся чанков переиспользуются.
3. 12 кандидатов ранжируются внутри RAG, но в контекст LLM попадают максимум 2 результата.
4. Одна выдержка ограничена 160 условными токенами, весь поисковый ответ — 500 токенами.
5. Из одного документа возвращается максимум один чанк, поэтому дубликаты не съедают контекст.
6. Полный документ не отправляется автоматически; для деталей используется ограниченный
   `kb_get_chunk` с бюджетом до 350 токенов.
7. В MCP discovery остаются только `kb_search` и `kb_get_chunk`; схемы административных tools не
   занимают стартовый контекст и агент не может самостоятельно запросить список или полный документ.

Готовый профиль находится в `scripts/rag-low-context.sh`. Любое значение можно переопределить
переменной окружения перед запуском.

## 1. Подготовить сервер

После установки положите документы в `knowledge/` и задайте секреты:

```bash
cd /opt/corporate-kb

export KB_MCP_HTTP_BEARER_TOKEN='REPLACE_WITH_SERVER_TOKEN'
export KB_ADMIN_PASSWORD='REPLACE_WITH_SEPARATE_ADMIN_PASSWORD'
export KB_BENCHMARK_PASSWORD='REPLACE_WITH_SEPARATE_BENCHMARK_PASSWORD'
```

Токены должны быть длиннее 32 символов и различаться между собой. В production храните их в
защищённом env-файле или secret manager, а не в Git.

Проверить активный профиль без вывода секретов:

```bash
./scripts/rag-low-context.sh config
```

## 2. Один раз построить индекс

Самый быстрый вариант без отдельной ML-модели использует hash embeddings:

```bash
export KB_EMBEDDING_PROVIDER='hash'
./scripts/rag-low-context.sh reindex
```

Команда выполняет принудительную, но инкрементальную сборку. Все документы перечитываются для
проверки изменений, однако embeddings неизменившихся чанков берутся из предыдущего cache. В логах
виден прогресс загрузки, chunking и embedding.

Для более качественного semantic-поиска используйте локальную модель. Одни и те же настройки
модели должны присутствовать и при переиндексации, и при запуске сервера:

```bash
export KB_EMBEDDING_PROVIDER='sentence_transformers'
export KB_EMBEDDING_MODEL='/opt/models/Qwen3-Embedding-0.6B'
export KB_EMBEDDING_DEVICE='cuda'
export KB_EMBEDDING_BATCH_SIZE='32'

./scripts/rag-low-context.sh reindex
```

Если CUDA отсутствует, укажите `cpu`; индекс будет строиться дольше. Для macOS доступен `mps`.

## 3. Запустить готовый RAG

В новом процессе снова установите те же секреты и embedding-настройки, затем запустите:

```bash
cd /opt/corporate-kb
./scripts/rag-low-context.sh serve
```

Сервер сначала загружает `.cache/kb` в RAM и только после этого открывает порт. В production-логах
должно быть `Loaded prepared knowledge index`; строка `Loading knowledge documents` при обычном
старте означает, что ошибочно включён `KB_AUTO_INDEX=true`.

Проверка:

```bash
curl -i 'http://127.0.0.1:8000/health'

curl -G 'http://127.0.0.1:8000/api/v1/search' \
  -H 'Authorization: Bearer REPLACE_WITH_SERVER_TOKEN' \
  --data-urlencode 'query=как восстановить сервис после сбоя' \
  --data-urlencode 'top_k=3'
```

В JSON поиска проверяйте `context_token_count`: значение не должно превышать 500. Количество
`results` — не больше двух даже при `top_k=10`, а одинаковый `source_path` не должен повторяться.

## 4. Измерить экономию и качество

Заполните `evaluation/questions.json` реальными вопросами и ожидаемыми документами, затем выполните:

```bash
curl -sS -X POST 'http://127.0.0.1:8000/api/v1/admin/context-benchmark' \
  -H 'Authorization: Bearer REPLACE_WITH_SERVER_TOKEN' \
  -H 'X-KB-Benchmark-Password: REPLACE_WITH_SEPARATE_BENCHMARK_PASSWORD'
```

Смотрите на четыре поля:

- `context.token_reduction_percent` — экономия относительно прежних пяти полных чанков;
- `quality.packed_hit_at_3_percent` — качество нового короткого контекста;
- `quality.baseline_hit_at_5_percent` — прежний baseline;
- `failed_questions` — вопросы, для которых нужный документ не попал в сжатый результат.

Нормальный результат: `status=passed`, новый Hit@3 не ниже baseline Hit@5, а список ошибок пуст.
Серверная оценка измеряет именно RAG payload; точные billed tokens конкретной LLM смотрите в
метриках её провайдера на том же фиксированном наборе вопросов.

## 5. Когда нужна переиндексация

Переиндексируйте после добавления или изменения документов, смены embedding-модели, dimension или
настроек chunking:

```bash
./scripts/rag-low-context.sh reindex
```

После успешной сборки перезапустите HTTP-сервис либо нажмите «Создать новый индекс» в `/admin`:
web-панель сама заменит активный store после сохранения cache.

Если менялись только `KB_SEARCH_MAX_RESULTS`, `KB_SEARCH_EXCERPT_TOKENS`,
`KB_SEARCH_CONTEXT_TOKENS`, `KB_SEARCH_MAX_CHUNKS_PER_DOCUMENT`, `KB_DOCUMENT_CONTEXT_TOKENS` или
`KB_MCP_MINIMAL_TOOLS`, перестраивать индекс не нужно — достаточно перезапустить сервер и полностью
перезапустить MCP-клиент, чтобы он заново получил список tools.

## Более строгий режим

После проверки benchmark можно уменьшить бюджет ещё сильнее:

```bash
export KB_SEARCH_MAX_RESULTS='1'
export KB_SEARCH_EXCERPT_TOKENS='120'
export KB_SEARCH_CONTEXT_TOKENS='350'
export KB_DOCUMENT_CONTEXT_TOKENS='250'

./scripts/rag-low-context.sh serve
```

Не уменьшайте лимиты вслепую: после каждого изменения сравнивайте Hit@3 и список промахов. Также не
уменьшайте лимиты только после benchmark: слишком короткая единственная выдержка может не содержать
доказательство, даже если нужный документ был найден среди внутренних кандидатов.
