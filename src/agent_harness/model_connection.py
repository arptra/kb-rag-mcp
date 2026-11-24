"""Switch between the host's GigaCode runner and a certificate-authenticated model API."""

# ruff: noqa: RUF001  # Russian diagnostics and response phrases.

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import secrets
import sqlite3
import ssl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Event
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_harness.contracts import ModelDecisionError, ModelPort
from agent_harness.model_profiles import (
    ModelProfile,
    ResolvedModelProfile,
    model_profile_defaults,
    resolve_model_profile,
)

RETRYABLE_FINISH_REASONS = frozenset({
    "length", "max_tokens", "insufficient_system_resource", "aborted",
})
INCOMPLETE_FINISH_REASONS = RETRYABLE_FINISH_REASONS


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["gigacode", "openai"] = "gigacode"
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)
    profile: ModelProfile = "auto"
    api_key_env: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,127})?$")
    tls_bundle_id: str = Field(default="", pattern=r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?$")
    timeout_seconds: int = Field(default=60, ge=5, le=3600)
    output_tokens: int = Field(default=4096, ge=128, le=131072)

    @model_validator(mode="before")
    @classmethod
    def profile_defaults(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("provider") == "openai":
            value = dict(value)
            profile = resolve_model_profile(
                str(value.get("profile", "auto")), str(value.get("model", "")),
            )
            defaults = model_profile_defaults(profile)
            for key in ("timeout_seconds", "output_tokens"):
                value.setdefault(key, defaults[key])
        return value

    @property
    def resolved_profile(self) -> ResolvedModelProfile:
        return (
            resolve_model_profile(self.profile, self.model)
            if self.provider == "openai" else "generic"
        )

    @field_validator("base_url", "model", "api_key_env", "tls_bundle_id", mode="before")
    @classmethod
    def trim_fields(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_endpoint(self) -> Self:
        if self.base_url:
            try:
                url = urlsplit(self.base_url)
                valid = (
                    url.scheme == "https"
                    and url.hostname
                    and not url.username
                    and not url.password
                    and not url.query
                    and not url.fragment
                    and not any(c.isspace() or ord(c) < 32 for c in self.base_url)
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Model endpoint must be HTTPS without credentials or query")
        if self.provider == "openai" and not self.base_url:
            raise ValueError("Model endpoint is required")
        return self

    @property
    def completion_url(self) -> str:
        """Accept either an API base or the complete chat endpoint copied by an operator."""
        endpoint = self.base_url.rstrip("/")
        if urlsplit(endpoint).path.endswith("/chat/completions"):
            return endpoint
        return endpoint + "/chat/completions"


class ModelConnectionStore:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "configuration.sqlite3"
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS model_connection (id INTEGER PRIMARY KEY, data TEXT)"
            )
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self) -> ModelConnection:
        with self._connect() as db:
            row = db.execute("SELECT data FROM model_connection WHERE id=1").fetchone()
        if not row:
            return ModelConnection()
        data = json.loads(row[0])
        # Older releases saved their small generic defaults even for DeepSeek.
        # Upgrade those defaults once; retain every non-default operator value.
        # Explicit profiles mark new configurations, including deliberate small
        # limits, so subsequent reads never silently retune them.
        if (
            isinstance(data, dict) and "profile" not in data
            and data.get("provider") == "openai"
            and resolve_model_profile("auto", str(data.get("model", ""))) == "deepseek_v4_flash"
        ):
            for key, old in (("timeout_seconds", 60), ("output_tokens", 4096)):
                if data.get(key) == old:
                    data.pop(key)
        return ModelConnection.model_validate(data)

    def save(self, settings: ModelConnection) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO model_connection VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (settings.model_dump_json(),),
            )


class ModelConnectionError(RuntimeError):
    """Safe operator-facing error; never includes a raw upstream body or request headers."""


class ModelTransientError(ModelConnectionError):
    """A temporary upstream failure; the run may retry within its reserved budget."""

    def __init__(self, message: str, *, retry_after: float = 0) -> None:
        self.retry_after = retry_after
        super().__init__(message)


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return 0.25 if response.status_code in {429, 503} else 0
    if value.isascii() and value.isdigit():
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ModelConnectionError("Запрошенная шлюзом пауза превышает время запроса.")
        return seconds
    try:
        return max(0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (ValueError, OverflowError, TypeError):
        return 0


def _completion_envelope(data: bytearray) -> dict[str, Any]:
    try:
        decoded = json.loads(data)
    except json.JSONDecodeError as exc:
        body = exc.doc.rstrip()
        if not body or (body.startswith("{") and (
            exc.pos >= len(body) - 1 or exc.msg.startswith("Unterminated string")
        )):
            raise ModelTransientError("Шлюз оборвал JSON-ответ модели.") from None
        raise ModelConnectionError("API вернул несовместимый ответ модели.") from None
    except UnicodeDecodeError as exc:
        if exc.end == len(data) and exc.reason == "unexpected end of data":
            raise ModelTransientError("Получен неполный ответ API модели.") from None
        raise ModelConnectionError("API вернул несовместимую кодировку ответа.") from None
    if not isinstance(decoded, dict):
        raise ModelConnectionError("API вернул несовместимый ответ модели.")
    return decoded


class ModelContextTooLarge(ModelConnectionError):
    """The gateway explicitly rejected the size of this model request."""


class ModelPolicyStop(ModelConnectionError):
    """Terminal provider refusal. Only known signals and opaque trace IDs are public."""

    def __init__(self, signal: str, request_id: str = "", *, field: str = "") -> None:
        self.signal = signal
        self.field = field
        self.request_id = ""
        self.http_status: int | None = None
        self.stop_reason = "model_refusal_text" if signal == "text" else "model_policy_stop"
        message = (
            "Модель прислала шаблонный ответ об ограничениях или мнении. "
            "Шлюз не указал причину."
            if signal == "text" else f"API модели сообщил об ограничении ({signal})."
        )
        if re.fullmatch(r"(?:req[-_])?(?:[0-9a-fA-F]{16,64}|[0-9a-fA-F]{8}"
                        r"(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", request_id):
            self.request_id = request_id
            message += f" ID запроса шлюза: {request_id}."
        super().__init__(message)


_POLICY_SIGNALS = {"blacklist", "refusal", "content_filter", "content_policy_violation",
                   "responsibleaipolicyviolation"}
_REFUSAL_SENTENCE = re.compile(
    r"(?:к сожалению[, ]+)?(?:"
    r"(?:я |у меня |как (?:ии|языковая модель)[, ]+(?:я )?)(?:нет |не (?:имею|обладаю) )"
    r"(?:(?:собственн|личн)\w* ){1,2}мнен\w*"
    r"|(?:мой ответ|этот ответ|ответ модели|результат генерации|мои ответы) "
    r"(?:не (?:явля\w*|отража\w*)|может не отражать) (?:официальн\w* )?"
    r"(?:точк\w* зрения|мнени\w*)"
    r"|(?:я )?не могу (?:ответить|предоставить ответ) (?:на (?:этот|ваш) (?:вопрос|запрос) )?"
    r"(?:из-за|в связи с|согласно|в рамках) (?:действующ\w* |корпоративн\w* )?"
    r"(?:правил|политик|ограничени)"
    r"|(?:as an ai(?: language model)?,? )?i (?:do not|don't) have (?:a |any )?"
    r"(?:personal opinions?\b))[^.!?;\[\]\n]{0,110}[.!?…]*", re.I,
)


def check_model_policy(
    message: dict[str, Any], reason: Any, request_id: str = "", *, field: str = "finish_reason",
) -> None:
    if isinstance(reason, str) and reason.lower() in _POLICY_SIGNALS:
        raise ModelPolicyStop(reason.lower(), request_id, field=field)
    if message.get("refusal"):
        raise ModelPolicyStop("refusal", request_id, field="message.refusal")
    content = message.get("content")
    if isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "refusal" for part in content
    ):
        raise ModelPolicyStop("refusal", request_id, field="message.content[].type")
    # A phrase quoted inside a technical answer is not a provider refusal.
    if isinstance(content, str) and len(content) <= 1200 and not message.get("tool_calls"):
        sentences = re.split(r"(?<=[.!?…])\s+|\n+|,\s*(?:но|однако|but|however)\s+",
                             content.strip(), flags=re.I)
        if len(sentences) <= 4 and all(_REFUSAL_SENTENCE.fullmatch(s) for s in sentences):
            raise ModelPolicyStop("text", request_id, field="message.content")


def _check_policy_error(body: Any, request_id: str) -> None:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        for path, data in (("error", error), ("error.innererror", error.get("innererror"))):
            if isinstance(data, dict):
                for key in ("code", "type"):
                    check_model_policy({}, data.get(key), request_id, field=f"{path}.{key}")


def _check_context_error(body: Any) -> None:
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and error.get("code") in (
        "context_length_exceeded", "max_context_length_exceeded", "input_too_long",
    ):
        raise ModelContextTooLarge("Размер запроса превысил контекстное окно шлюза.")


class ModelRouter:
    def __init__(self, state_dir: Path, fallback: ModelPort | None) -> None:
        self.store = ModelConnectionStore(state_dir)
        self.fallback = fallback
        self.state_dir = state_dir
        self.transport: httpx.AsyncBaseTransport | None = None

    def _ssl(self, settings: ModelConnection) -> ssl.SSLContext:
        if not settings.tls_bundle_id:
            return ssl.create_default_context()
        from agent_harness.tls_credentials import TLSCredentialStore

        return TLSCredentialStore(self.state_dir).ssl_context(settings.tls_bundle_id)

    def status(self) -> dict[str, Any]:
        settings = self.store.load()
        if settings.provider == "gigacode":
            result = (
                self.fallback.status()
                if self.fallback
                else {
                    "name": "GigaCode",
                    "available": False,
                    "reason": "GigaCode не подключён. Настройте запуск или выберите API модели.",
                }
            )
            return {**result, "provider": "gigacode"}
        reason = "API настроен. Проверить соединение можно в настройках модели."
        try:
            self._ssl(settings)
            if settings.api_key_env and not os.environ.get(settings.api_key_env):
                reason = "Ключ API отсутствует в указанной переменной окружения."
                available = False
            else:
                available = True
        except (ValueError, KeyError, OSError):
            reason, available = "Сертификаты модели недоступны или некорректны.", False
        return {
            "name": settings.model or "API модели",
            "provider": "openai",
            "profile": settings.resolved_profile,
            "available": available,
            "enabled": True,
            "state": "configured" if available else "not_configured",
            "reason": reason,
        }

    async def decide(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: Event,
        max_input_chars: int,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        settings = self.store.load()
        if settings.provider == "gigacode":
            if self.fallback is None:
                raise ModelConnectionError("GigaCode не подключён.")
            return await self.fallback.decide(
                messages=messages, tools=tools, cancel=cancel, max_input_chars=max_input_chars
            )
        if (
            len(json.dumps(messages, ensure_ascii=False))
            + len(json.dumps(tools, ensure_ascii=False))
            > max_input_chars
        ):
            raise ModelConnectionError("Превышен лимит контекста модели.")
        if cancel.is_set():
            raise asyncio.CancelledError
        task = asyncio.create_task(self._completion(
            settings, messages, tools, max_output_tokens=max_output_tokens,
        ))
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    if cancel.is_set():
                        raise asyncio.CancelledError
                return await task
        except TimeoutError:
            raise ModelTransientError("API модели не ответил вовремя.") from None
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _completion(
        self,
        settings: ModelConnection,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        probe: bool = False,
        max_output_tokens: int | None = None,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        if settings.api_key_env:
            if not secret:
                raise ModelConnectionError("Ключ API не задан на сервере.")
            headers["Authorization"] = "Bearer " + secret
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "max_tokens": 128 if probe else min(
                settings.output_tokens, max_output_tokens or settings.output_tokens,
            ),
        }
        # Some corporate gateways select the model from the URL. Do not send an
        # empty name; endpoints requiring one will report it during the probe.
        if settings.model:
            payload["model"] = settings.model
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if settings.resolved_profile == "deepseek_v4_flash":
            # DeepSeek otherwise enables thinking by default. Its thinking tool
            # protocol requires opaque reasoning_content on every subsequent
            # turn; ModelPort deliberately carries only decisions and evidence.
            # Harness research still plans, calls tools and verifies findings.
            payload["thinking"] = {"type": "disabled"}
        try:
            context = self._ssl(settings)
        except (ValueError, KeyError, OSError):
            raise ModelConnectionError(
                "Ошибка загрузки сертификатов модели. Проверьте выбранный набор, "
                "срок действия сертификатов и приватный ключ клиента."
            ) from None
        try:
            async with (
                httpx.AsyncClient(
                    verify=context,
                    # Corporate networks often require HTTPS_PROXY/NO_PROXY. The
                    # same verified TLS context is used by probes and real runs.
                    trust_env=True,
                    follow_redirects=False,
                    timeout=httpx.Timeout(
                        settings.timeout_seconds,
                        connect=min(30, settings.timeout_seconds),
                        write=min(60, settings.timeout_seconds),
                        pool=min(30, settings.timeout_seconds),
                    ),
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    settings.completion_url,
                    headers=headers,
                    json=payload,
                ) as response,
            ):
                request_id = response.headers.get("x-request-id", "")
                if response.status_code >= 400:
                    error_body = bytearray()
                    # Preserve the actual HTTP status if the secondary error body
                    # itself is broken, especially on an authentication failure.
                    with suppress(httpx.HTTPError):
                        async for chunk in response.aiter_bytes():
                            if len(error_body) + len(chunk) > 32768:
                                break
                            error_body.extend(chunk)
                    # Non-JSON errors still receive the normal HTTP diagnostic.
                    with suppress(ValueError):
                        error_data = json.loads(error_body)
                        _check_policy_error(error_data, request_id)
                        choices = (error_data.get("choices")
                                   if isinstance(error_data, dict) else None)
                        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                            reply = choices[0].get("message")
                            check_model_policy(reply if isinstance(reply, dict) else {},
                                               choices[0].get("finish_reason"), request_id)
                        if response.status_code in {400, 413, 422}:
                            _check_context_error(error_data)
                if response.status_code == 413:
                    raise ModelContextTooLarge("Шлюз отклонил размер запроса (HTTP 413).")
                if response.status_code in {401, 403}:
                    raise ModelConnectionError(
                        "API отклонил доступ. Проверьте клиентский сертификат и права на модель. "
                        "API-ключ нужен только по требованию сервера."
                    )
                if response.status_code == 429:
                    raise ModelTransientError("API модели ограничил частоту запросов.",
                                              retry_after=_retry_after(response))
                if response.status_code in {408, 425, 500, 502, 503, 504}:
                    raise ModelTransientError(
                        f"API модели временно недоступен (HTTP {response.status_code}).",
                        retry_after=_retry_after(response),
                    )
                if response.status_code == 404:
                    raise ModelConnectionError(
                        "API вернул HTTP 404. Укажите базовый адрес API или полный адрес "
                        "/chat/completions и проверьте название модели."
                    )
                if response.status_code in {400, 422}:
                    raise ModelConnectionError(
                        f"API модели вернул HTTP {response.status_code}: запрос не принят. "
                        "Проверьте OpenAI-совместимость API и название модели; "
                        "название обязательно, если сервер требует явный выбор модели."
                    )
                if response.status_code != 200:
                    raise ModelConnectionError(f"API модели вернул HTTP {response.status_code}.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000:
                        raise ModelConnectionError("Ответ API превышает допустимый размер.")
            decoded = _completion_envelope(data)
            _check_policy_error(decoded, request_id)
            if "choices" in decoded and decoded["choices"] in ([], None):
                raise ModelTransientError("Шлюз вернул пустую выдачу модели.")
            choice = decoded["choices"][0]
            reason = choice.get("finish_reason")
            message = choice.get("message") or {}
            check_model_policy(message, reason, request_id)
            if not message and reason in {None, "stop", "tool_calls"}:
                raise ModelTransientError("Шлюз не передал сообщение модели.")
            if reason not in {None, "", "stop", "tool_calls", "function_call", "end_turn",
                              *RETRYABLE_FINISH_REASONS}:
                raise ModelConnectionError(
                    "API прислал неизвестную причину остановки. Проверьте ответ шлюза "
                    "и совместимость с Chat Completions."
                )
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("Invalid content")
            calls = message.get("tool_calls")
            if calls is None:
                calls = []
            if not isinstance(calls, list) or len(calls) > 32:
                raise ModelDecisionError("Модель вернула некорректный список действий.")
            incomplete_calls = bool(calls) and reason in INCOMPLETE_FINISH_REASONS
            if incomplete_calls:
                # A length stop may cut arguments in the middle of JSON. Never
                # parse or execute any action from this unfinished generation.
                calls = []
            parsed_calls = []
            allowed_names = {tool["function"]["name"] for tool in tools}
            try:
                for call in calls:
                    function = call["function"]
                    if function["name"] not in allowed_names:
                        raise ValueError("Unknown tool")
                    arguments = json.loads(function["arguments"])
                    if not isinstance(arguments, dict):
                        raise ValueError("Invalid arguments")
                    parsed_calls.append({"name": function["name"], "args": arguments})
            except (ValueError, KeyError, TypeError):
                # A valid API envelope can contain a bad model decision. Let the
                # existing bounded repair loop ask for corrected actions; never
                # execute a partially accepted batch or expose raw arguments.
                raise ModelDecisionError(
                    "Модель вернула недопустимый инструмент или некорректные аргументы действия."
                ) from None

            def redact(value: Any) -> Any:
                if isinstance(value, str) and secret:
                    return value.replace(secret, "[redacted]")
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [redact(item) for item in value]
                return value

            # The response-byte guard bounds transport memory. The run's configured
            # answer limit is applied by the runtime; do not silently clip it here.
            decision = {"content": redact(content), "tool_calls": redact(parsed_calls)}
            if reason in INCOMPLETE_FINISH_REASONS:
                decision["finish_reason"] = reason
            if incomplete_calls:
                decision["incomplete_tool_calls"] = True
            usage = decoded.get("usage")
            if isinstance(usage, dict):
                counters = {key: value for key in ("prompt_tokens", "completion_tokens")
                            if isinstance(value := usage.get(key), int)
                            and not isinstance(value, bool) and value >= 0}
                if counters:
                    decision["usage"] = counters
            return decision
        except ModelPolicyStop as exc:
            exc.http_status = response.status_code
            raise
        except (ModelConnectionError, ModelDecisionError):
            raise
        except httpx.TimeoutException:
            raise ModelTransientError(
                "API модели не ответил вовремя. Проверьте сеть или увеличьте время ожидания."
            ) from None
        except httpx.ProxyError:
            raise ModelConnectionError(
                "Ошибка подключения через прокси. Проверьте HTTPS_PROXY и NO_PROXY на сервере."
            ) from None
        except (httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            cause: BaseException | None = exc
            for _ in range(8):
                if isinstance(cause, ssl.SSLError):
                    raise ModelConnectionError(
                        "Ошибка проверки TLS-сертификата API модели. Проверьте сертификаты."
                    ) from None
                cause = (cause.__cause__ or cause.__context__) if cause is not None else None
            raise ModelTransientError(
                "Соединение не установлено. Проверьте адрес API, сеть, прокси и сертификаты."
            ) from None
        except httpx.HTTPError:
            raise ModelConnectionError(
                "Соединение не установлено. Проверьте адрес API, сеть, прокси и сертификаты."
            ) from None
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ModelConnectionError("API вернул несовместимый ответ модели.") from None

    async def probe(
        self, settings: ModelConnection | None = None, *, autopilot: bool = False,
    ) -> dict[str, Any]:
        settings = settings if settings is not None else self.store.load()
        if settings.provider == "gigacode":
            status = (
                await asyncio.to_thread(self.fallback.status)
                if self.fallback is not None
                else {"available": False, "reason": "GigaCode не подключён."}
            )
            if not autopilot or not status.get("available"):
                return {"available": bool(status.get("available")),
                        "message": status.get("reason", "")}
        cancel = Event()

        async def complete(
            messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        ) -> dict[str, Any]:
            if settings.provider == "gigacode" and self.fallback is not None:
                result = await self.fallback.decide(messages=messages, tools=tools,
                                                    max_input_chars=16_000, cancel=cancel)
                check_model_policy(result, result.get("finish_reason"))
                return result
            return await self._completion(settings, messages, tools, probe=True)

        stage = ""
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                result = await complete([{"role": "user", "content": "Reply with OK."}], [])
                if result.get("finish_reason") in INCOMPLETE_FINISH_REASONS:
                    raise ModelConnectionError("API не завершил проверочный ответ.")
                if result["content"].strip().rstrip(".!").casefold() != "ok":
                    raise ModelConnectionError(
                        "На простой запрос API вернул текст, отличный от ожидаемого OK. "
                        "Проверьте выбранную модель и маршрут корпоративного API."
                    )
                if autopilot:
                    from agent_harness.library_agent import run_agent_round

                    stage = "Простой запрос прошёл. Проверка выбора инструмента: "
                    tool = {"type": "function", "function": {
                        "name": "read_sample", "description": "Read a synthetic sample by ID.",
                        "parameters": {"type": "object", "properties": {"id": {"type": "string"}},
                                       "required": ["id"], "additionalProperties": False},
                    }}
                    submission = {"type": "function", "function": {
                        "name": "submit_sample",
                        "description": "Submit the value and its source ID.",
                        "parameters": {"type": "object", "properties": {
                            "value": {"type": "string"}, "id": {"type": "string"}},
                            "required": ["value", "id"], "additionalProperties": False},
                    }}
                    sample = "S-" + secrets.token_hex(4)
                    executed = False

                    async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
                        if observations:
                            return await complete([
                                {"role": "system", "content": "Вызови submit_sample. Скопируй "
                                 "sample_value документа в value, id документа в id."},
                                {"role": "user", "content": str(observations[-1]["result"])},
                            ], [submission])
                        prompt = "Вызови read_sample с id sample-1 для чтения учебного документа."
                        return await complete([{"role": "user", "content": prompt}], [tool])

                    async def dispatch(calls: list[dict[str, Any]]) -> list[str]:
                        nonlocal executed, stage
                        expected_call = {"name": "read_sample", "args": {"id": "sample-1"}}
                        if executed or calls != [expected_call]:
                            raise ModelDecisionError("Не удалось прочитать учебный документ.")
                        executed = True
                        stage = "Инструмент выполнен. Проверка итогового ответа: "
                        return [json.dumps({"id": "sample-1", "sample_value": sample})]

                    step = await run_agent_round(tools=[tool], decide=decide, dispatch=dispatch)
                    if not executed:
                        raise ModelConnectionError(
                            "API не вернул ожидаемый вызов read_sample. Проверьте поддержку "
                            "tool calling и политики шлюза для запросов с инструментами."
                        )
                    if step.get("finish_reason") in INCOMPLETE_FINISH_REASONS:
                        raise ModelConnectionError("Модель не завершила итоговый ответ.")
                    missing = []
                    if calls := step.get("tool_calls"):
                        if (not isinstance(calls, list) or len(calls) != 1
                                or not isinstance(calls[0], dict)
                                or calls[0].get("name") != "submit_sample"):
                            raise ModelConnectionError(
                                "Ожидался один итоговый вызов submit_sample."
                            )
                        args = calls[0].get("args")
                        if not isinstance(args, dict) or args.keys() - {"id", "value"}:
                            raise ModelConnectionError("Некорректные аргументы submit_sample.")
                        correct_value = args.get("value") == sample
                        correct_source = args.get("id") == "sample-1"
                    else:
                        content = step.get("content") or ""
                        correct_value = bool(re.search(
                            rf"(?<![\w-]){re.escape(sample)}(?![\w-])", content,
                        ))
                        correct_source = "[sample-1]" in content
                    if not correct_value:
                        missing.append("точное значение sample_value из документа")
                    if not correct_source:
                        missing.append("id документа sample-1" if calls else "ссылку [sample-1]")
                    if missing:
                        raise ModelConnectionError("Модель не вернула " + " и ".join(missing) + ".")
                    return {"available": True, "message":
                            "Агент вызвал инструмент и вернул проверенное значение из документа."}
            return {"available": True, "message": "Соединение проверено. API ответил OK."}
        except (ModelConnectionError, ModelDecisionError) as exc:
            return {"available": False, "message": stage + str(exc)}
        except TimeoutError:
            return {
                "available": False,
                "message": stage + "API модели не ответил вовремя. Увеличьте время ожидания.",
            }
        except (ValueError, KeyError, OSError):
            return {
                "available": False,
                "message": "Проверка API не пройдена. Проверьте сертификаты и адрес.",
            }
        except RuntimeError:
            return {"available": False,
                    "message": stage + "Модель не завершила проверочный запрос."}
        finally:
            cancel.set()
