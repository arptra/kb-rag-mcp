"""Use gpt2giga's OpenAI ↔ GigaChat v1 converters inside the portal process."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Context
from uuid import uuid4

import httpx
from gigachat.api.chat import chat_async
from gigachat.exceptions import ResponseError
from gigachat.models import Chat
from gpt2giga.common.client_params import ClientCompatibilityError
from gpt2giga.models.config import GigaChatCLI, ProxyConfig, ProxySettings
from gpt2giga.protocol.request.transformer import RequestTransformer
from gpt2giga.protocol.response.processor import ResponseProcessor
from langchain_openai import ChatOpenAI
from pydantic import SecretStr, ValidationError

from corporate_kb.confluence.diagnostics import safe_http_logging

# Protocol converter diagnostics can include the request contents. The portal
# reports its own bounded model errors and never logs conversation data here.
_CONVERTER_LOG = logging.Logger("portal_agents.gpt2giga", level=logging.CRITICAL + 1)
_LOCAL_URL = "https://gpt2giga.invalid/v1"


class _CorporateTransformer(RequestTransformer):
    """Restore a v1 functions_state_id issued by this particular model endpoint."""

    def __init__(self, config: ProxyConfig, state_prefix: str) -> None:
        super().__init__(config, _CONVERTER_LOG)
        self.state_prefix = state_prefix

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
        try:
            native = Chat.model_validate(await self.encoder.prepare_chat(data))
        except (ClientCompatibilityError, ValidationError):
            return httpx.Response(422, json={"error": {"message": "Unsupported GigaChat request"}})

        async def complete() -> httpx.Response:
            with safe_http_logging():
                try:
                    result = await chat_async(self.client, chat=native, access_token=self.token)
                except ResponseError as exc:
                    # Preserve the status for SDK diagnostics and retry handling. Provider
                    # bodies and response headers can contain internal URLs and user text.
                    return httpx.Response(exc.status_code, json={"error": {
                        "message": "GigaChat API rejected the request",
                    }})
                except ValidationError:
                    return httpx.Response(502, json={"error": {"message": "Invalid native reply"}})
            if not result.choices:
                return httpx.Response(502, json={"error": {"message": "Missing GigaChat choices"}})
            converted = self.decoder.process_response(result, self.model or result.model,
                                                      uuid4().hex, request_data=data)
            for choice in converted["choices"]:
                for call in choice["message"].get("tool_calls") or []:
                    if state := call.get("tools_state_id"):
                        # LangChain persists the standard call ID in checkpoints; vendor
                        # extensions on the OpenAI tool-call object may be discarded.
                        call["id"] = self.state_prefix + uuid4().hex + "_" + state
                if choice.get("finish_reason") == "error":
                    return httpx.Response(502, json={"error": {"message": "Generation failed"}})
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
                        use_responses_api=False, http_client=sync_client, http_async_client=client,
                        timeout=timeout, max_retries=max_retries,
                        extra_body={"max_tokens": output_tokens} if output_tokens else None,
                    )
