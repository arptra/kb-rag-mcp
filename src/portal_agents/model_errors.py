"""Recognize model gateway failures without exposing upstream payloads."""

from __future__ import annotations

import json
import re
from typing import Any

_INVALID_REQUEST = re.compile(r"(?:openai[.:]?)?invalidrequesterror", re.IGNORECASE)
_ERROR_PREFIX = re.compile(
    r"^(?:openai[.:]?)?invalidrequesterror:\s*(?:http\s+|error code:\s*)?[45]\d\d\b",
    re.IGNORECASE,
)


class ModelGatewayError(RuntimeError):
    def __init__(self, status: int | None = None) -> None:
        label = f" (HTTP {status})" if status is not None else ""
        if status in (401, 403):
            hint = "Проверьте сертификат и права на выбранную модель."
        elif status == 429:
            hint = "Достигнут лимит запросов модели."
        elif status is not None and status >= 500:
            hint = "Проверьте доступность сервиса модели."
        elif status == 404:
            hint = "Проверьте адрес API и имя модели."
        elif status == 413:
            hint = "Превышен допустимый размер запроса."
        else:
            hint = "Проверьте протокол API, имя модели и поддержку вызова инструментов."
        super().__init__(f"Шлюз модели отклонил запрос{label}. {hint}")


def _invalid_payload(error: Any) -> bool:
    if not isinstance(error, dict):
        return False
    return any(isinstance(field, str) and
               (field.casefold() == "invalid_request_error" or _INVALID_REQUEST.fullmatch(field))
               for field in (error.get("type"), error.get("code"), error.get("name")))


def gateway_exception(exc: BaseException) -> ModelGatewayError | None:
    """Only call at the model boundary; tool HTTP errors have different causes."""
    if isinstance(exc, ModelGatewayError):
        return exc
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if type(status) is int and 400 <= status <= 599:
        return ModelGatewayError(status)
    # ChatOpenAI raises ValueError(error_dict) for error objects inside HTTP 200.
    if _INVALID_REQUEST.fullmatch(type(exc).__name__) or (
        isinstance(exc, ValueError) and len(exc.args) == 1 and _invalid_payload(exc.args[0])
    ):
        return ModelGatewayError()
    return None


def check_gateway_reply(content: str) -> None:
    text = content.strip()
    if _INVALID_REQUEST.fullmatch(text) or _ERROR_PREFIX.match(text):
        raise ModelGatewayError()
    if not (text.startswith("{") and text.endswith("}")):
        return
    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        return
    if isinstance(obj, dict) and _invalid_payload(obj.get("error")):
        raise ModelGatewayError()
