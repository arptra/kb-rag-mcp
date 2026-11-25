"""Use gpt2giga's OpenAI ↔ GigaChat v1 converters inside the portal process."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Context
from datetime import UTC
from email.utils import format_datetime, parsedate_to_datetime
from uuid import uuid4

import httpx
from gigachat.api.chat import _build_request_json, chat_async
from gigachat.exceptions import ResponseError
from gigachat.models import Chat
from gpt2giga.common.client_params import ClientCompatibilityError
from gpt2giga.common.tool_schema import normalize_tool_schema
from gpt2giga.models.config import GigaChatCLI, ProxyConfig, ProxySettings
from gpt2giga.protocol.request.transformer import RequestTransformer
from gpt2giga.protocol.response.processor import ResponseProcessor
from langchain_openai import ChatOpenAI
from pydantic import SecretStr, ValidationError

from corporate_kb.confluence.diagnostics import safe_http_logging
from portal_agents.model_errors import invalid_request_payload, model_error_metadata
from portal_agents.wire_diagnostics import WireDiagnostics, safe_request_id

# Protocol converter diagnostics can include the request contents. The portal
# reports its own bounded model errors and never logs conversation data here.
_CONVERTER_LOG = logging.Logger("portal_agents.gpt2giga", level=logging.CRITICAL + 1)
_LOCAL_URL = "https://gpt2giga.invalid/v1"


def _error_headers(headers: httpx.Headers | None) -> dict[str, str]:
    safe = {}
    if not headers:
        return safe
    if headers.get("x-should-retry", "").strip().lower() == "false":
        safe["x-should-retry"] = "false"
    if request_id := safe_request_id(headers.get("x-request-id")):
        safe["x-request-id"] = request_id
    if delay := headers.get("retry-after"):
        if re.fullmatch(r"[0-9]{1,10}(?:\.[0-9]{1,6})?", delay):
            safe["retry-after"] = delay
        elif len(delay) <= 64:
            try:
                date = parsedate_to_datetime(delay)
                if date.tzinfo is not None:
                    safe["retry-after"] = format_datetime(date.astimezone(UTC), usegmt=True)
            except (ValueError, TypeError, OverflowError):
                pass
    return safe


def _safe_error_body(message: str, payload: object) -> dict:
    field, denial = model_error_metadata(payload)
    error = {"message": message}
    if field:
        error["param"] = field
    if denial:
        error["code"] = {"policy": "content_filter", "access": "permission_denied"}[denial]
    return {"error": error}


def _bridge_error(status: int, code: str, payload: object = None,
                  request_data: object = None) -> httpx.Response:
    extensions: dict = {"portal_model_error": code}
    if status == 422:
        extensions["portal_wire_diagnostics"] = WireDiagnostics(
            "adapter-input", request_data, payload,
        )
    return httpx.Response(status, headers={"X-Portal-Model-Error": code},
                          extensions=extensions, json=_safe_error_body(code, payload))


def _serial_tool_history(messages: list[dict]) -> list[dict]:
    """Represent completed parallel calls from another model as v1 call/result pairs."""
    result = []
    index = 0
    while index < len(messages):
        message = messages[index]
        index += 1
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls or len(calls) < 2:
            result.append(message)
            continue
        replies = {}
        start = index
        while index < len(messages) and messages[index].get("role") == "tool":
            reply = messages[index]
            index += 1
            replies[reply.get("tool_call_id")] = reply
        if index - start != len(calls):
            raise ClientCompatibilityError("Missing parallel tool results", param="messages")
        for position, call in enumerate(calls):
            reply = replies.pop(call.get("id"), None)
            if reply is None or not call.get("id"):
                raise ClientCompatibilityError("Unmatched parallel tool result", param="messages")
            assistant = message if position == 0 else {"role": "assistant", "content": ""}
            result.extend(({**assistant, "tool_calls": [call]}, reply))
    return result


class _CorporateTransformer(RequestTransformer):
    """Restore a v1 functions_state_id issued by this particular model endpoint."""

    def __init__(self, config: ProxyConfig, state_prefix: str) -> None:
        super().__init__(config, _CONVERTER_LOG)
        self.state_prefix = state_prefix

    def _normalize_function_schemas(self, payload: dict, *, api_mode: str,
                                    forced_model: str | None = None) -> None:
        super()._normalize_function_schemas(payload, api_mode=api_mode, forced_model=forced_model)
        if api_mode != "v1":
            return
        # gpt2giga normalizes the public GigaChat-2 endpoint. For remaining
        # v1 schemas simplify only optional fields that default to null.
        functions = payload.get("functions")
        if isinstance(functions, list):
            for index, definition in enumerate(functions):
                function = self._dump_mapping(definition)
                _normalize_optional_fields(function.get("parameters"))
                functions[index] = function

    def _sanitize_legacy_message_state_ids(self, messages: list[dict]) -> None:
        # Upstream removes v1 state IDs because ordinary OpenAI call IDs can be
        # synthetic. Some corporate deployments require their own ID on the
        # assistant message. Restore only IDs tagged on an actual provider reply.
        for message in messages:
            state = message.pop("functions_state_id", None)
            message.pop("tools_state_id", None)
            if (message.get("role") == "assistant" and message.get("function_call")
                    and isinstance(state, str) and state.startswith(self.state_prefix)):
                _, separator, upstream_id = state[len(self.state_prefix):].partition("_")
                if separator and upstream_id:
                    message["functions_state_id"] = upstream_id


def _normalize_optional_fields(schema: object) -> None:
    if not isinstance(schema, dict):
        return
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, field in properties.items():
            if not isinstance(field, dict):
                continue
            _normalize_optional_fields(field)
            if name in (schema.get("required") or []) or field.get("default", ...) is not None:
                continue
            for keyword in ("anyOf", "oneOf"):
                variants = field.get(keyword)
                if (not isinstance(variants, list) or len(variants) != 2
                        or {"type": "null"} not in variants):
                    continue
                value = next((v for v in variants if v != {"type": "null"}), None)
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    continue
                annotations = {k: v for k, v in field.items() if k not in {keyword, "default"}}
                if annotations.keys() - {"title", "description", "examples", "deprecated"}:
                    continue
                # Reject lossy conversions inside the surviving branch (e.g. a nested union).
                normalized = normalize_tool_schema({keyword: variants})
                if normalized == value:
                    properties[name] = {**normalized, **annotations}
                break
    _normalize_optional_fields(schema.get("items"))


class _GigaChatBridge(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """Translate an SDK request locally; only the configured native client opens a socket."""

    def __init__(self, client: httpx.AsyncClient, model: str | None, token: str | None) -> None:
        self.client = client
        self.model = model
        self.token = token
        # These models are gpt2giga CLI settings. Construct explicit trusted defaults
        # without consuming portal argv, .env, or another service's GIGACHAT_* secrets.
        config = ProxyConfig.model_construct(
            proxy_settings=ProxySettings.model_construct(
                mode="PROD", log_level="INFO", pass_model=True, default_max_tokens=None,
                enable_images=False, normalize_tool_schemas=True,
            ),
            gigachat_settings=GigaChatCLI.model_construct(
                base_url=str(client.base_url), model=model,
            ),
        )
        origin_model = json.dumps([str(client.base_url), model]).encode()
        self.state_prefix = "giga_" + hashlib.sha256(origin_model).hexdigest()[:16] + "_"
        self.encoder = _CorporateTransformer(config, self.state_prefix)
        self.decoder = ResponseProcessor(_CONVERTER_LOG, mode="PROD")

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or str(request.url) != _LOCAL_URL + "/chat/completions":
            return httpx.Response(404)
        data = json.loads(await request.aread())
        if self.model:
            data["model"] = self.model
        else:
            data.pop("model", None)  # Some corporate deployments select the model by URL.
        converted_request = data
        try:
            data["messages"] = _serial_tool_history(data.get("messages", []))
            converted_request = await self.encoder.prepare_chat(data)
            native = Chat.model_validate(converted_request)
        except ClientCompatibilityError as exc:
            return _bridge_error(422, "invalid-request", {"param": exc.param, "code": exc.code,
                                                         "type": exc.error_type}, converted_request)
        except ValidationError as exc:
            issues = exc.errors(include_url=False, include_context=False, include_input=False)
            return _bridge_error(422, "invalid-request", {"detail": issues}, converted_request)

        async def complete() -> httpx.Response:
            with safe_http_logging():
                try:
                    result = await chat_async(self.client, chat=native, access_token=self.token)
                except ResponseError as exc:
                    # Preserve only typed diagnostics; other headers and bodies can contain
                    # internal URLs, authentication material and user text.
                    extensions = {}
                    if exc.status_code == 422:
                        wire = _build_request_json(chat=native, exclude_stream=True)
                        extensions["portal_wire_diagnostics"] = WireDiagnostics(
                            "gigachat-v1", wire, exc.content, exc.url,
                        )
                    return httpx.Response(
                        exc.status_code, headers=_error_headers(exc.headers), extensions=extensions,
                        json=_safe_error_body("GigaChat API rejected the request", exc.content),
                    )
                except ValidationError as exc:
                    # Some endpoints return a typed OpenAI error envelope with HTTP 200.
                    # Keep the non-retryable rejection recognizable to ChatOpenAI.
                    for field in exc.errors(include_url=False, include_context=False):
                        payload = field.get("input")
                        if field["loc"] == ("choices",) and isinstance(payload, dict):
                            error = _safe_error_body("GigaChat API rejected the request", payload)
                            if (invalid_request_payload(payload.get("error"))
                                    or "code" in error["error"]):
                                error["error"]["type"] = "invalid_request_error"
                                return httpx.Response(200, json=error)
                    return _bridge_error(502, "invalid-reply")
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    # The native SDK decodes JSON and unpacks it as keyword arguments.
                    return _bridge_error(502, "invalid-reply")
            if not result.choices:
                return _bridge_error(502, "invalid-reply")
            converted = self.decoder.process_response(result, self.model or result.model,
                                                      uuid4().hex, request_data=data)
            for choice in converted["choices"]:
                for call in choice["message"].get("tool_calls") or []:
                    if state := call.get("tools_state_id"):
                        # LangChain persists the standard call ID in checkpoints; vendor
                        # extensions on the OpenAI tool-call object may be discarded.
                        call["id"] = self.state_prefix + uuid4().hex + "_" + state
                if choice.get("finish_reason") == "error":
                    return _bridge_error(502, "generation-failed")
                if choice.get("finish_reason") == "blacklist":
                    choice["finish_reason"] = "content_filter"
            return httpx.Response(200, json=converted)

        # The native SDK permits context-local Authorization, custom headers and URL
        # overrides. Isolate those ambient variables from the selected portal profile.
        return await asyncio.create_task(complete(), context=Context())

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        raise NotImplementedError("Portal chat uses the asynchronous GigaChat bridge")


@asynccontextmanager
async def gpt2giga_model(
    *, base_url: str, model_name: str | None, tls: ssl.SSLContext, token: str | None,
    timeout: int, output_tokens: int | None, max_retries: int,
) -> AsyncIterator[ChatOpenAI]:
    with safe_http_logging():
        async with httpx.AsyncClient(base_url=base_url, verify=tls, timeout=timeout,
                                     follow_redirects=False) as upstream:
            bridge = _GigaChatBridge(upstream, model_name, token)
            with httpx.Client(transport=bridge) as sync_client:
                async with httpx.AsyncClient(transport=bridge) as client:
                    yield ChatOpenAI(
                        model=model_name or "gateway", base_url=_LOCAL_URL,
                        api_key=SecretStr("in-process"), cache=False, disable_streaming=True,
                        metadata={"portal_chat_reads_first": True},
                        use_responses_api=False, http_client=sync_client, http_async_client=client,
                        timeout=timeout, max_retries=max_retries,
                        extra_body={"max_tokens": output_tokens} if output_tokens else None,
                    )
