"""Classify model errors without exposing upstream payloads or private URLs."""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx
from langchain_core.exceptions import ModelAPIError, ModelConnectionError, ModelTimeoutError

from corporate_kb.confluence.diagnostics import network_error
from corporate_kb.gigacode_runner import GigaCodeTimedOut, GigaCodeTransientError

_INVALID_REQUEST = re.compile(r"(?:openai[.:]?)?invalidrequesterror", re.IGNORECASE)
_ERROR_PREFIX = re.compile(
    r"^(?:openai[.:]?)?invalidrequesterror:\s*(?:http\s+|error code:\s*)?([45]\d\d)\b",
    re.IGNORECASE,
)
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_REQUEST_ID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_BRIDGE_ERRORS = {
    "invalid-request": "Адаптер модели не смог преобразовать запрос",
    "invalid-reply": "API модели вернул нечитаемый ответ",
    "generation-failed": "Сервис модели прервал формирование ответа",
}


class ModelGatewayError(RuntimeError):
    def __init__(
        self, status: int | None = None, *, message: str | None = None,
        retryable: bool | None = None, retry_after: float | None = None,
        request_id: str | None = None, category: str = "invalid-request",
    ) -> None:
        self.status = status
        self.retryable = status in _TRANSIENT_STATUS if retryable is None else retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.category = category
        self.attempts = 1
        suffix = f" (HTTP {status})" if status is not None else ""
        if message:
            detail = message + suffix + "."
        elif status in (401, 403):
            detail = f"Нет доступа к модели{suffix}. Проверьте сертификат и права доступа."
        elif status == 429:
            detail = f"Достигнут лимит запросов к модели{suffix}."
        elif status == 408:
            detail = f"Сервис модели не успел обработать запрос{suffix}."
        elif status is not None and status >= 500 and status in _TRANSIENT_STATUS:
            detail = f"Сервис модели временно недоступен{suffix}."
        elif status == 404:
            detail = f"Модель или адрес API не найдены{suffix}."
        elif status == 413:
            detail = f"Запрос превышает допустимый для модели размер{suffix}."
        else:
            detail = (f"API модели отклонил запрос{suffix}. "
                      "Проверьте адрес API, имя модели и поддержку вызова инструментов.")
        super().__init__(detail)


def invalid_request_payload(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    return any(isinstance(field, str) and
               (field.casefold() == "invalid_request_error" or _INVALID_REQUEST.fullmatch(field))
               for field in (error.get("type"), error.get("code"), error.get("name")))


def _retry_after(value: str) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def gateway_exception(exc: BaseException) -> ModelGatewayError | None:
    """Only call at the model boundary; tool HTTP errors have different causes."""
    if isinstance(exc, ModelGatewayError):
        return exc
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if type(status) is int and 400 <= status <= 599:
        headers = response.headers if isinstance(response, httpx.Response) else {}
        request_id = headers.get("x-request-id", "")
        bridge_error = headers.get("x-portal-model-error", "")
        category = bridge_error if bridge_error in _BRIDGE_ERRORS else "upstream-http"
        return ModelGatewayError(
            status, retry_after=_retry_after(headers.get("retry-after", "")),
            retryable=status in _TRANSIENT_STATUS and headers.get("x-should-retry") != "false",
            request_id=str(UUID(request_id)) if _REQUEST_ID.fullmatch(request_id) else None,
            category=category, message=_BRIDGE_ERRORS.get(category),
        )
    if isinstance(exc, (GigaCodeTimedOut, ModelTimeoutError, httpx.TimeoutException)):
        return ModelGatewayError(message="Время ожидания ответа модели истекло",
                                 retryable=True, category="timeout")
    if isinstance(exc, (GigaCodeTransientError, ModelAPIError)):
        return ModelGatewayError(message="Сервис модели временно недоступен", retryable=True,
                                 category="provider")
    if isinstance(exc, (ModelConnectionError, httpx.TransportError)):
        # LangChain wraps the OpenAI exception, which wraps the actual transport error.
        cause: BaseException | None = exc
        for _ in range(12):
            if isinstance(cause, httpx.HTTPError):
                diagnostic = network_error(cause).error_code
                if diagnostic in {"tls_certificate", "tls_handshake"}:
                    return ModelGatewayError(message="Проверьте сертификаты TLS подключения модели",
                                             category="tls")
                if diagnostic == "local_protocol":
                    return ModelGatewayError(message="Проверьте настройки подключения модели",
                                             category="local-protocol")
                break
            if cause is None:
                break
            cause = cause.__cause__ or cause.__context__
        return ModelGatewayError(message="Связь между порталом и моделью прервалась",
                                 retryable=True, category="transport")
    # ChatOpenAI raises ValueError(error_dict) for error objects inside HTTP 200.
    if _INVALID_REQUEST.fullmatch(type(exc).__name__) or (
        isinstance(exc, ValueError) and len(exc.args) == 1 and invalid_request_payload(exc.args[0])
    ):
        return ModelGatewayError()
    return None


def check_gateway_reply(content: str) -> None:
    text = content.strip()
    if _INVALID_REQUEST.fullmatch(text):
        raise ModelGatewayError()
    if match := _ERROR_PREFIX.match(text):
        raise ModelGatewayError(int(match.group(1)), category="error-in-reply")
    if not (text.startswith("{") and text.endswith("}")):
        return
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return
    if isinstance(obj, dict) and invalid_request_payload(obj.get("error")):
        raise ModelGatewayError()
