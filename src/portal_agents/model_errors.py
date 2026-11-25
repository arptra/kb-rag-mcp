"""Classify model errors without exposing upstream payloads or private URLs."""
# ruff: noqa: RUF001 -- Russian diagnostics.
from __future__ import annotations

import json
import math
import re
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from langchain_core.exceptions import ModelAPIError, ModelConnectionError, ModelTimeoutError
from langchain_core.messages import BaseMessage

from corporate_kb.agents.activation import GigaCodeDecisionError, GigaCodeRecoveryPending
from corporate_kb.confluence.diagnostics import network_error
from corporate_kb.gigacode_runner import (
    GigaCodeAccessError,
    GigaCodePolicyStop,
    GigaCodeProcessCrashed,
    GigaCodeRequestFailed,
    GigaCodeTimedOut,
    GigaCodeTransientError,
)
from portal_agents.wire_diagnostics import WireDiagnostics, safe_request_id

_INVALID_REQUEST = re.compile(r"(?:openai[.:]?)?invalidrequesterror", re.IGNORECASE)
_ERROR_PREFIX = re.compile(
    r"^(?:openai[.:]?)?invalidrequesterror:\s*(?:http\s+|error code:\s*)?([45]\d\d)\b",
    re.IGNORECASE,
)
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}
_BRIDGE_ERRORS = {
    "invalid-request": "Адаптер модели не смог преобразовать запрос",
    "invalid-reply": "API модели вернул нечитаемый ответ",
    "generation-failed": "Сервис модели прервал формирование ответа",
}
_VALIDATION_ROOTS = {
    "functions": "tools", "tools": "tools", "tool_choice": "tools",
    "messages": "messages", "function_call": "messages", "tool_calls": "messages",
    "model": "model", "max_tokens": "max_tokens", "max_completion_tokens": "max_tokens",
}
# Match field paths (e.g. messages[1].tool_calls), never text from a diagnostic.
_PARAMETER_PATH = re.compile(r"([A-Za-z_]\w*)(?:\.(?:[A-Za-z_]\w*|\d+)|\[\d+\])*", re.ASCII)
_DENIAL_CODES = {
    **dict.fromkeys(("blacklist", "contentfilter", "contentfiltered", "contentpolicyviolation",
                     "policyviolation", "responsibleaipolicyviolation", "safetyviolation",
                     "refusal", "safety", "blockedbypolicy", "policyerror",
                     "moderationblocked"), "policy"),
    **dict.fromkeys(("authenticationerror", "permissionerror", "invalidapikey", "invalidtoken",
                     "unauthorized", "permissiondenied", "permissiondeniederror",
                     "forbidden", "accessdenied", "insufficientpermissions",
                     "authenticationrequired"),
                    "access"),
}


def _validation_field(location: object) -> str | None:
    if isinstance(location, (tuple, list)):
        if len(location) > 64 or not all(
            (type(part) is int and part >= 0)
            or (isinstance(part, str) and len(part) <= 256 and part.isidentifier())
            for part in location
        ):
            return None
        parts = location[1:] if location and location[0] == "body" else location
        root = parts[0] if parts else None
    elif isinstance(location, str) and len(location) <= 512:
        match = _PARAMETER_PATH.fullmatch(location.removeprefix("body."))
        root = match.group(1) if match else None
    else:
        root = None
    return _VALIDATION_ROOTS.get(root) if isinstance(root, str) else None


def model_error_metadata(payload: object) -> tuple[str | None, str | None]:
    """Read only typed validation locations and provider refusal codes from an error."""
    if isinstance(payload, bytes) and len(payload) <= 65536:
        try:
            payload = json.loads(payload)
        except (ValueError, RecursionError):
            return None, None
    if not isinstance(payload, dict):
        return None, None

    envelopes = [payload]
    if isinstance(payload.get("error"), dict):
        envelopes.append(payload["error"])
    nodes = list(envelopes)
    fields = set()
    for envelope in envelopes:
        for key in ("innererror", "inner_error"):
            if isinstance(envelope.get(key), dict):
                nodes.append(envelope[key])
        for key in ("detail", "details", "errors"):
            issues = envelope.get(key)
            if isinstance(issues, list):
                for issue in issues:
                    if isinstance(issue, dict):
                        nodes.append(issue)
                    if not isinstance(issue, dict) or not {"loc", "field", "param"} & issue.keys():
                        fields.add(None)
            elif isinstance(issues, dict):
                nodes.append(issues)

    denial = None
    for node in nodes:
        for key in ("loc", "param", "field"):
            if key in node:
                fields.add(_validation_field(node[key]))
        for key in ("code", "type", "name", "finish_reason", "stop_reason", "error"):
            code = node.get(key)
            if isinstance(code, str) and len(code) <= 64:
                marker = code.strip().casefold().replace("_", "").replace("-", "")
                denial = _DENIAL_CODES.get(marker, denial)
        if any(node.get(key) in (401, 403, "401", "403")
               for key in ("status", "status_code", "code")):
            denial = "access"
        if node.get("refusal"):
            denial = "policy"
    # A format change cannot repair a rejection that also cites an unknown field.
    return (fields.pop() if len(fields) == 1 else None), denial


class ModelPolicyRefusal(PermissionError):
    """The model provider explicitly marked a generation as restricted."""


def check_policy_reply(message: BaseMessage) -> None:
    refused_block = isinstance(message.content, list) and any(
        isinstance(block, dict) and block.get("type") == "refusal" for block in message.content
    )
    if (message.response_metadata.get("finish_reason") in {"content_filter", "blacklist", "refusal"}
            or message.additional_kwargs.get("refusal") or refused_block):
        raise ModelPolicyRefusal("Корпоративная модель ограничила ответ на запрос.")


def model_policy_refusal(exc: Exception) -> bool:
    return (isinstance(exc, (ModelPolicyRefusal, GigaCodePolicyStop))
            or (isinstance(exc, ModelGatewayError) and exc.category == "policy"))


class ModelGatewayError(RuntimeError):
    def __init__(
        self, status: int | None = None, *, message: str | None = None,
        retryable: bool | None = None, retry_after: float | None = None,
        request_id: str | None = None, category: str = "invalid-request",
        validation_field: str | None = None, retry_forbidden: bool = False,
    ) -> None:
        self.status = status
        self.retry_forbidden = (retry_forbidden or status in (401, 403)
                                or category in {"access", "policy"})
        self.retryable = (not self.retry_forbidden and status != 422
                          and (status in _TRANSIENT_STATUS if retryable is None else retryable))
        self.validation_field = (_VALIDATION_ROOTS.get(validation_field) if status == 422 else None)
        self.retry_after = retry_after
        self.request_id = request_id
        self.category = category
        self.attempts = 1
        self.stage = "model"
        self.diagnostic_reference: str | None = None
        self.tool_count = 0
        self.diagnostics: dict[str, Any] = {}
        suffix = f" (HTTP {status})" if status is not None else ""
        if message:
            detail = message + suffix + "."
        elif category == "policy":
            detail = f"Сервис модели отклонил запрос по правилам доступа к содержимому{suffix}."
        elif status in (401, 403) or category == "access":
            detail = f"Нет доступа к модели{suffix}. Проверьте сертификат и права доступа."
        elif status == 429:
            detail = f"Достигнут лимит запросов к модели{suffix}."
        elif status == 408:
            detail = f"Сервис модели не успел обработать запрос{suffix}."
        elif status is not None and status >= 500 and status in _TRANSIENT_STATUS:
            detail = f"API модели вернул ошибку при обработке этого запроса{suffix}."
        elif status == 404:
            detail = f"Модель или адрес API не найдены{suffix}."
        elif status == 413:
            detail = f"Запрос превышает допустимый для модели размер{suffix}."
        elif status == 422:
            detail = f"API модели отклонил формат запроса{suffix}."
        else:
            detail = (f"API модели отклонил запрос{suffix}. "
                      "Проверьте адрес API, имя модели и поддержку вызова инструментов.")
        if self.validation_field and category not in {"policy", "access"}:
            detail += f" Поле запроса — {self.validation_field}."
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
    if isinstance(exc, GigaCodeRecoveryPending):
        return ModelGatewayError(category="restart-pending", retryable=False,
                                 message="GigaCode ещё восстанавливается. "
                                         "Портал продолжает проверять готовность")
    if isinstance(exc, GigaCodeDecisionError):
        return ModelGatewayError(category="incomplete-reply", retryable=False,
                                 message="GigaCode не вернул завершённый ответ")
    if isinstance(exc, GigaCodeRequestFailed):
        category, message = {
            "request": ("runner-request", "GigaCode завершил этот запрос с ошибкой"),
            "configuration": (
                "runner-configuration", "Проверьте параметры запуска и авторизацию GigaCode",
            ),
            "native-tool": (
                "runner-native-tool", "GigaCode попытался вызвать встроенный инструмент",
            ),
            "unexpected-exit": (
                "runner-exit", "Процесс GigaCode завершился. Портал проверяет его готовность",
            ),
        }[exc.reason]
        failure = ModelGatewayError(category=category, message=message)
        if type(exc.returncode) is int:
            failure.diagnostics["exit_code"] = exc.returncode
        return failure
    if isinstance(exc, GigaCodeAccessError):
        return ModelGatewayError(exc.status, message="GigaCode отклонил доступ к модели",
                                 retryable=False, category="access")
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    if type(status) is int and 400 <= status <= 599:
        headers = response.headers if isinstance(response, httpx.Response) else {}
        request_id = headers.get("x-request-id", "")
        bridge_error = (response.extensions.get("portal_model_error")
                        if isinstance(response, httpx.Response) else None)
        body = getattr(exc, "body", None)
        if isinstance(response, httpx.Response):
            with suppress(httpx.ResponseNotRead):
                body = response.content
        field, denial = model_error_metadata(body)
        category = denial or (bridge_error if bridge_error in _BRIDGE_ERRORS else "upstream-http")
        failure = ModelGatewayError(
            status, retry_after=_retry_after(headers.get("retry-after", "")),
            retry_forbidden=headers.get("x-should-retry", "").strip().lower() == "false",
            validation_field=field,
            request_id=safe_request_id(request_id),
            category=category, message=_BRIDGE_ERRORS.get(category),
        )
        if status == 422:
            attached = (response.extensions.get("portal_wire_diagnostics")
                        if isinstance(response, httpx.Response) else None)
            if isinstance(attached, WireDiagnostics):
                failure.diagnostics = attached
            else:
                request = None
                if isinstance(response, httpx.Response):
                    with suppress(RuntimeError):
                        request = response.request
                content = None
                if request is not None:
                    with suppress(httpx.RequestNotRead):
                        content = request.content
                failure.diagnostics = WireDiagnostics(
                    "openai", content, body, request.url if request is not None else None,
                )
        return failure
    if isinstance(exc, (GigaCodeTimedOut, ModelTimeoutError, httpx.TimeoutException)):
        return ModelGatewayError(message="Время ожидания ответа модели истекло",
                                 retryable=True, category="timeout")
    if isinstance(exc, GigaCodeTransientError):
        hints = {
            "rate-limit": "Достигнут лимит запросов GigaCode",
            "overload": "GigaCode сообщил о перегрузке",
            "timeout": "Истекло время обработки запроса GigaCode",
            "server-error": "GigaCode сообщил об ошибке при обработке запроса",
            "process-crash": "Процесс GigaCode аварийно завершился",
        }
        reason = {408: "timeout", 429: "rate-limit"}.get(exc.status, exc.reason)
        category = reason if reason in hints else "provider"
        failure = ModelGatewayError(
            exc.status, message=hints.get(category, "Ошибка вызова GigaCode"),
            retryable=True, category=category,
        )
        if isinstance(exc, GigaCodeProcessCrashed) and type(exc.returncode) is int:
            failure.diagnostics["exit_code"] = exc.returncode
        return failure
    if isinstance(exc, ModelAPIError):
        return ModelGatewayError(message="Модель сообщила об ошибке при обработке запроса",
                                 retryable=True, category="provider")
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
    if isinstance(exc, ValueError) and len(exc.args) == 1 and isinstance(exc.args[0], dict):
        _, denial = model_error_metadata(exc.args[0])
        if denial or invalid_request_payload(exc.args[0]):
            return ModelGatewayError(category=denial or "invalid-request")
    if _INVALID_REQUEST.fullmatch(type(exc).__name__):
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
