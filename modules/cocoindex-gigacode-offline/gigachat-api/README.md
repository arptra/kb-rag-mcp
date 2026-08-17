# Подключение CocoIndex к GigaChat Embeddings API по mTLS

Это руководство описывает полностью рабочую схему без Docker, локальной
Transformer-модели, PyTorch и `sentence-transformers`:

```text
Java/Spring repository
        |
        v
    cocoindex-code
        |
        | OpenAI-compatible HTTP, localhost
        v
 gpt2giga :8090
        |
        | HTTPS + client certificate (mTLS)
        v
 GigaChat Embeddings API

Vector index -> local LMDB/SQLite
```

`gpt2giga` нужен как локальный адаптер между OpenAI-compatible интерфейсом
LiteLLM/CocoIndex и GigaChat API. Он передаёт запросы к `/embeddings` в
GigaChat через официальный Python SDK и поддерживает клиентские сертификаты.

Важно: при этой схеме фрагменты исходного кода отправляются в указанный
GigaChat endpoint. Перед индексацией необходимо убедиться, что это разрешено
политиками информационной безопасности. Векторная база при этом остаётся
локальной.

## 1. Что понадобится

- Python 3.10-3.14;
- установленный `cocoindex-code`;
- доступ к Python-пакету `gpt2giga` во внутреннем registry или wheelhouse;
- URL корпоративного GigaChat mTLS endpoint;
- клиентский сертификат в PEM;
- соответствующий закрытый ключ в PEM;
- CA bundle для проверки сертификата сервера;
- доступ к модели `EmbeddingsGigaR`.

Используйте URL, который выдала корпоративная команда GigaChat. Не заменяйте
его публичным URL без согласования: endpoint для mTLS может отличаться.

## 2. Установка gpt2giga без Docker

Держим proxy в отдельном virtual environment, чтобы его зависимости не
конфликтовали с CocoIndex:

```bash
mkdir -p /data/gpt2giga
python3.13 -m venv /data/gpt2giga/.venv

/data/gpt2giga/.venv/bin/python -m pip install \
  --no-cache-dir \
  "gpt2giga==0.2.6"
```

Для корпоративного registry:

```bash
/data/gpt2giga/.venv/bin/python -m pip install \
  --index-url https://INTERNAL-PYPI.EXAMPLE/simple \
  --no-cache-dir \
  "gpt2giga==0.2.6"
```

Посмотреть доступные внутри банка версии:

```bash
/data/gpt2giga/.venv/bin/python -m pip index versions gpt2giga \
  --index-url https://INTERNAL-PYPI.EXAMPLE/simple
```

Если `0.2.6` недоступна, выбирайте самую новую разрешённую версию, в которой
есть OpenAI-compatible `POST /v1/embeddings` и поддержка переменных
`GIGACHAT_CERT_FILE`/`GIGACHAT_KEY_FILE`.

Проверка:

```bash
/data/gpt2giga/.venv/bin/gpt2giga --help
```

## 3. Размещение сертификатов

Рекомендуемая структура:

```text
/data/gpt2giga/certs/client.pem
/data/gpt2giga/certs/client.key
/data/gpt2giga/certs/ca-bundle.crt
```

Назначение файлов:

- `client.pem` — клиентский сертификат, предъявляемый GigaChat;
- `client.key` — закрытый ключ этого сертификата;
- `ca-bundle.crt` — доверенные CA для проверки сертификата сервера GigaChat.

CA bundle нельзя подставлять вместо клиентского сертификата, а клиентский
сертификат — вместо CA bundle.

Ограничиваем права:

```bash
chmod 700 /data/gpt2giga/certs
chmod 600 /data/gpt2giga/certs/client.pem
chmod 600 /data/gpt2giga/certs/client.key
chmod 644 /data/gpt2giga/certs/ca-bundle.crt
```

Проверяем срок действия и издателя сертификата:

```bash
openssl x509 \
  -in /data/gpt2giga/certs/client.pem \
  -noout -subject -issuer -serial -dates
```

Проверяем закрытый ключ:

```bash
openssl pkey \
  -in /data/gpt2giga/certs/client.key \
  -noout -check
```

Публичные ключи сертификата и private key должны совпасть. У обеих команд
должен получиться одинаковый SHA-256:

```bash
openssl x509 \
  -in /data/gpt2giga/certs/client.pem \
  -pubkey -noout \
  | openssl pkey -pubin -outform DER \
  | sha256sum

openssl pkey \
  -in /data/gpt2giga/certs/client.key \
  -pubout -outform DER \
  | sha256sum
```

Не публикуйте вывод private key и не добавляйте каталог `certs` в Git.

## 4. Конфигурация mTLS

Генерируем отдельный ключ для защиты только локального proxy:

```bash
openssl rand -hex 32
```

Создаём `/data/gpt2giga/config/gpt2giga.env`:

```bash
mkdir -p /data/gpt2giga/config
chmod 700 /data/gpt2giga/config
```

Содержимое файла:

```dotenv
# Локальный OpenAI-compatible proxy. Не открываем порт во внешнюю сеть.
GPT2GIGA_MODE=PROD
GPT2GIGA_HOST=127.0.0.1
GPT2GIGA_PORT=8090

# Авторизация между CocoIndex и локальным proxy.
GPT2GIGA_ENABLE_API_KEY_AUTH=True
GPT2GIGA_API_KEY=REPLACE_WITH_LOCAL_RANDOM_KEY

# Embeddings route.
GPT2GIGA_PASS_MODEL=True
GPT2GIGA_EMBEDDINGS=EmbeddingsGigaR

# Не записываем код и тела запросов в логи.
GPT2GIGA_LOG_LEVEL=INFO
GPT2GIGA_LOG_REDACT_SENSITIVE=True
GPT2GIGA_TRAFFIC_LOG_ENABLED=False

# Выданный организацией mTLS endpoint.
GIGACHAT_BASE_URL=https://GIGACHAT-MTLS.EXAMPLE/api/v1

# Клиентская аутентификация mTLS.
GIGACHAT_CERT_FILE=/data/gpt2giga/certs/client.pem
GIGACHAT_KEY_FILE=/data/gpt2giga/certs/client.key

# Оставьте строку только для зашифрованного private key.
GIGACHAT_KEY_FILE_PASSWORD=REPLACE_WITH_KEY_PASSWORD

# Проверка сертификата сервера обязательна.
GIGACHAT_CA_BUNDLE_FILE=/data/gpt2giga/certs/ca-bundle.crt
GIGACHAT_VERIFY_SSL_CERTS=True

GIGACHAT_TIMEOUT=120
GIGACHAT_MAX_RETRIES=3
GIGACHAT_RETRY_BACKOFF_FACTOR=1
```

Если private key не зашифрован, удалите
`GIGACHAT_KEY_FILE_PASSWORD`. Для чистого mTLS не задавайте:

```text
GIGACHAT_CREDENTIALS
GIGACHAT_ACCESS_TOKEN
GIGACHAT_USER
GIGACHAT_PASSWORD
GIGACHAT_SCOPE
```

Иначе SDK может выбрать другой способ авторизации по приоритету.

Защищаем конфигурацию:

```bash
chmod 600 /data/gpt2giga/config/gpt2giga.env
```

## 5. Проверка mTLS без CocoIndex

Сначала проверяем соединение напрямую. Подставьте выданный endpoint:

```bash
curl \
  --cert /data/gpt2giga/certs/client.pem \
  --key /data/gpt2giga/certs/client.key \
  --cacert /data/gpt2giga/certs/ca-bundle.crt \
  https://GIGACHAT-MTLS.EXAMPLE/api/v1/models
```

Для зашифрованного ключа `curl` запросит пароль. Успешный TLS handshake и
ответ API подтверждают, что сертификат, private key, CA bundle и endpoint
согласованы.

Не используйте `curl -k` и не задавайте
`GIGACHAT_VERIFY_SSL_CERTS=False`: это отключает проверку сервера.

## 6. Запуск локального proxy

На всякий случай удаляем старые OAuth-переменные из текущего процесса:

```bash
unset GIGACHAT_CREDENTIALS
unset GIGACHAT_ACCESS_TOKEN
unset GIGACHAT_USER
unset GIGACHAT_PASSWORD
unset GIGACHAT_SCOPE
```

Запускаем proxy с явным env-файлом:

```bash
/data/gpt2giga/.venv/bin/gpt2giga \
  --env-path /data/gpt2giga/config/gpt2giga.env
```

Процесс должен слушать только:

```text
127.0.0.1:8090
```

Проверка состояния:

```bash
curl http://127.0.0.1:8090/health
```

## 7. Проверка EmbeddingsGigaR через proxy

Подставьте тот же локальный ключ, который записан в
`GPT2GIGA_API_KEY`:

```bash
curl http://127.0.0.1:8090/v1/embeddings \
  -H "Authorization: Bearer REPLACE_WITH_LOCAL_RANDOM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "EmbeddingsGigaR",
    "input": [
      "public Order createOrder(CreateOrderRequest request)"
    ]
  }'
```

Успешный ответ содержит `data[0].embedding` — массив из 2560 чисел.

## 8. Подключение CocoIndex

Откройте глобальную конфигурацию CocoIndex:

```text
$COCOINDEX_CODE_DIR/global_settings.yml
```

Если `COCOINDEX_CODE_DIR` не задан, стандартный путь:

```text
~/.cocoindex_code/global_settings.yml
```

Конфигурация:

```yaml
embedding:
  provider: litellm
  model: openai/EmbeddingsGigaR
  min_interval_ms: 10

envs:
  OPENAI_BASE_URL: http://127.0.0.1:8090/v1
  OPENAI_API_KEY: REPLACE_WITH_LOCAL_RANDOM_KEY

daemon:
  idle_timeout_minutes: 180
```

`OPENAI_API_KEY` здесь — локальный ключ `gpt2giga`, а не GigaChat OAuth
credential. CocoIndex не должен получать пути к сертификату и private key:
mTLS полностью обслуживает локальный proxy.

Если API отвечает `429`, увеличьте `min_interval_ms`, например до `50` или
`100`, с учётом корпоративных квот.

## 9. Перестроение индекса

Смена embedding-модели меняет размерность вектора, поэтому старый индекс
нужно пересоздать. Сначала должен работать `gpt2giga`, затем из корня
индексируемого репозитория выполните:

```bash
/data/cocoindex/.venv/bin/ccc daemon stop
/data/cocoindex/.venv/bin/ccc reset
/data/cocoindex/.venv/bin/ccc daemon restart
/data/cocoindex/.venv/bin/ccc doctor
/data/cocoindex/.venv/bin/ccc index
/data/cocoindex/.venv/bin/ccc status
```

Проверка поиска:

```bash
/data/cocoindex/.venv/bin/ccc search \
  "где вызывается внешний платёжный сервис"
```

## 10. Подключение MCP к GigaCode

MCP по-прежнему запускает `ccc mcp`. Сертификаты в конфигурацию GigaCode
передавать не нужно:

```json
{
  "mcpServers": {
    "cocoindex-code": {
      "command": "/data/cocoindex/.venv/bin/ccc",
      "args": ["mcp"],
      "env": {
        "COCOINDEX_CODE_DIR": "/data/cocoindex/config",
        "OPENAI_BASE_URL": "http://127.0.0.1:8090/v1",
        "OPENAI_API_KEY": "REPLACE_WITH_LOCAL_RANDOM_KEY"
      }
    }
  }
}
```

Перед запуском GigaCode должны быть запущены:

1. `gpt2giga` на `127.0.0.1:8090`;
2. CocoIndex daemon, либо он запустится автоматически при первом `ccc`;
3. MCP-процесс `ccc mcp`.

## 11. Типичные ошибки

### `certificate verify failed`

- неверный или неполный `GIGACHAT_CA_BUNDLE_FILE`;
- CA bundle не содержит корпоративный intermediate/root CA;
- `GIGACHAT_BASE_URL` не соответствует имени в сертификате сервера;
- системное время ноутбука неверно.

### `tlsv1 alert unknown ca` или `bad certificate`

- сервер не доверяет издателю клиентского сертификата;
- передан не тот `client.pem`;
- клиентский сертификат просрочен или отозван;
- сертификат не разрешён для client authentication.

### `KEY_VALUES_MISMATCH`

`client.pem` и `client.key` не являются парой. Сравните SHA-256 публичных
ключей командами из раздела 3.

### `401` от локального proxy

`OPENAI_API_KEY` в CocoIndex не совпадает с `GPT2GIGA_API_KEY`.

### `401` или `403` от GigaChat

- mTLS handshake прошёл, но сертификат не имеет доступа к API или модели;
- используется неверный корпоративный endpoint;
- endpoint дополнительно требует другой вид авторизации — это необходимо
  уточнить у владельца корпоративного шлюза.

### CocoIndex требует `sentence-transformers`

Проверьте, что глобальная конфигурация содержит:

```yaml
embedding:
  provider: litellm
  model: openai/EmbeddingsGigaR
```

После изменения перезапустите daemon. При таком provider библиотека
`sentence-transformers` для embeddings не используется.

## 12. Ссылки

- [GigaChat Python SDK: mTLS и переменные окружения](https://github.com/ai-forever/gigachat#authentication)
- [gpt2giga: OpenAI-compatible gateway](https://github.com/ai-forever/gpt2giga)
- [Конфигурация gpt2giga](https://ai-forever.github.io/gpt2giga/configuration/)
- [GigaChat Embeddings API](https://developers.sber.ru/docs/ru/gigachat/guides/embeddings)
- [CocoIndex Code: embedding providers](https://github.com/cocoindex-io/cocoindex-code#embedding-models)
