"""Switch between the host's GigaCode runner and a certificate-authenticated model API."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import ssl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_harness.contracts import ModelPort


class ModelConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["gigacode", "openai"] = "gigacode"
    base_url: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)
    api_key_env: str = Field(default="", pattern=r"^([A-Z][A-Z0-9_]{0,127})?$")
    tls_bundle_id: str = Field(default="", pattern=r"^([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})?$")
    timeout_seconds: int = Field(default=60, ge=5, le=300)

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
        if self.provider == "openai" and (not self.base_url or not self.model.strip()):
            raise ValueError("Model endpoint and model name are required")
        return self


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
        return ModelConnection.model_validate_json(row[0]) if row else ModelConnection()

    def save(self, settings: ModelConnection) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO model_connection VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (settings.model_dump_json(),),
            )


class ModelConnectionError(RuntimeError):
    """Safe operator-facing error; never includes a raw upstream body or request headers."""


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
            "name": settings.model,
            "provider": "openai",
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
        task = asyncio.create_task(self._completion(settings, messages, tools))
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                while not task.done():
                    await asyncio.wait({task}, timeout=0.05)
                    if cancel.is_set():
                        raise asyncio.CancelledError
                return await task
        except TimeoutError:
            raise ModelConnectionError("API модели не ответил вовремя.") from None
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
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        secret = os.environ.get(settings.api_key_env, "") if settings.api_key_env else ""
        if settings.api_key_env:
            if not secret:
                raise ModelConnectionError("Ключ API не задан на сервере.")
            headers["Authorization"] = "Bearer " + secret
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": messages,
            "stream": False,
            "max_tokens": 16 if probe else 4096,
        }
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        try:
            async with (
                httpx.AsyncClient(
                    verify=self._ssl(settings),
                    trust_env=False,
                    follow_redirects=False,
                    timeout=settings.timeout_seconds,
                    transport=self.transport,
                ) as client,
                client.stream(
                    "POST",
                    settings.base_url.rstrip("/") + "/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise ModelConnectionError("API отклонил доступ. Проверьте ключ и сертификат.")
                if response.status_code == 429:
                    raise ModelConnectionError("API модели ограничил частоту запросов.")
                if response.status_code != 200:
                    raise ModelConnectionError(f"API модели вернул HTTP {response.status_code}.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ModelConnectionError("Ответ API превышает допустимый размер.")
            decoded = json.loads(data)
            message = decoded["choices"][0]["message"]
            content = message.get("content") or ""
            if not isinstance(content, str):
                raise ValueError("Invalid content")
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 32:
                raise ValueError("Invalid calls")
            parsed_calls = []
            allowed_names = {tool["function"]["name"] for tool in tools}
            for call in calls:
                function = call["function"]
                if function["name"] not in allowed_names:
                    raise ValueError("Unknown tool")
                arguments = json.loads(function["arguments"])
                if not isinstance(arguments, dict):
                    raise ValueError("Invalid arguments")
                parsed_calls.append({"name": function["name"], "args": arguments})

            def redact(value: Any) -> Any:
                if isinstance(value, str) and secret:
                    return value.replace(secret, "[redacted]")
                if isinstance(value, dict):
                    return {redact(key): redact(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [redact(item) for item in value]
                return value

            return {"content": redact(content[:50000]), "tool_calls": redact(parsed_calls)}
        except ModelConnectionError:
            raise
        except httpx.HTTPError as exc:
            raise ModelConnectionError(
                "Соединение не установлено. Проверьте адрес API, сеть и сертификаты."
            ) from exc
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            raise ModelConnectionError("API вернул несовместимый ответ модели.") from None

    async def probe(self) -> dict[str, Any]:
        settings = self.store.load()
        if settings.provider == "gigacode":
            status = await asyncio.to_thread(self.status)
            return {"available": bool(status.get("available")), "message": status.get("reason", "")}
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                result = await self._completion(
                    settings, [{"role": "user", "content": "Reply with OK."}], [], probe=True
                )
            if not result["content"].strip():
                raise ModelConnectionError("API не вернул текстовый ответ.")
            return {"available": True, "message": "Соединение и генерация ответа проверены."}
        except ModelConnectionError as exc:
            return {"available": False, "message": str(exc)}
        except (TimeoutError, ValueError, KeyError, OSError):
            return {
                "available": False,
                "message": "Проверка API не пройдена. Проверьте сертификаты и адрес.",
            }
