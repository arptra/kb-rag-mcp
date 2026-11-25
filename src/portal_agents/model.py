"""Build a LangChain chat model from trusted server configuration."""
from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, SecretStr, TypeAdapter

from corporate_kb.agents.activation import GigaCodeAgentController, accepts_decisions
from corporate_kb.agents.gigacode_model import GigaCodeChatModel
from corporate_kb.config import Settings
from corporate_kb.confluence.diagnostics import safe_http_logging
from corporate_kb.gigacode_runner import GigaCodeModelProfile, GigaCodeRunner


def short_call_options(model: BaseChatModel, limit: int) -> dict[str, Any]:
    """Limit a helper's reply while retaining the configured corporate wire options."""
    from langchain_openai import ChatOpenAI

    if not isinstance(model, ChatOpenAI):
        return {}
    body = dict(model.extra_body or {})
    body["max_tokens"] = min(body.get("max_tokens") or model.max_tokens or limit, limit)
    return {"extra_body": body}


class _ActivatedGigaCode(BaseChatModel):
    """Adapt the portal's existing authenticated, decision-only GigaCode controller."""

    controller: GigaCodeAgentController = Field(exclude=True, repr=False)
    disable_streaming: bool = True

    @property
    def _llm_type(self) -> str:
        return "gigacode-portal"

    def bind_tools(
        self, tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *, tool_choice: Any = None, **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        if tool_choice not in (None, "auto"):
            raise ValueError("GigaCode supports automatic tool selection")
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("The portal controller supports asynchronous requests")

    async def _agenerate(
        self, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        cancel = Event()
        try:
            decision = await self.controller.decide(
                messages=list(convert_to_openai_messages(messages, text_format="string")),
                tools=kwargs.get("tools", []), cancel=cancel, max_input_chars=1_000_000,
            )
        finally:
            cancel.set()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=decision["content"],
            tool_calls=[{"name": call["name"], "args": call["args"], "id": uuid4().hex}
                        for call in decision["tool_calls"]],
        ))])


@asynccontextmanager
async def create_model(
    settings: Settings, *, controller: GigaCodeAgentController | None = None,
    base_url: str | None = None, model_name: str | None = None,
    ca_file: Path | None = None, cert_file: Path | None = None, key_file: Path | None = None,
    api_key: str | None = None, profile_path: Path | None = None,
    timeout_seconds: int | None = None, output_tokens: int | None = None,
    max_retries: int = 0, disable_thinking: bool = False,
    adapter: Literal["openai", "gpt2giga"] | None = None,
) -> AsyncIterator[BaseChatModel]:
    if not base_url:
        if adapter == "gpt2giga":
            raise ValueError("Для gpt2giga нужен адрес корпоративного API.")
        if profile_path is not None:
            profile = TypeAdapter(GigaCodeModelProfile).validate_json(profile_path.read_bytes())
            yield GigaCodeChatModel(runner=GigaCodeRunner(settings), launch_profile=profile)
            return
        if controller is None or not accepts_decisions(controller.status()):
            raise RuntimeError("Включите GigaCode в настройках портала и завершите вход.")
        yield _ActivatedGigaCode(controller=controller, cache=False)
        return

    from langchain_openai import ChatOpenAI

    endpoint = urlsplit(base_url)
    if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query \
            or endpoint.fragment:
        raise ValueError("Укажите базовый URL API модели без параметров и пароля.")
    if endpoint.scheme != "https" and not (
        endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1", "::1"}
        and not (api_key or cert_file or key_file)
    ):
        raise ValueError("Корпоративному API модели требуется HTTPS.")
    if key_file and not cert_file:
        raise ValueError("Для клиентского ключа требуется сертификат.")
    tls = ssl.create_default_context()
    if ca_file:
        tls.load_verify_locations(cafile=ca_file)
    if cert_file:
        tls.load_cert_chain(certfile=cert_file, keyfile=key_file, password=b"")

    timeout = timeout_seconds or settings.gigacode_timeout_seconds
    if (adapter or settings.agents_model_adapter) == "gpt2giga":
        from portal_agents.gpt2giga_model import gpt2giga_model

        async with gpt2giga_model(
            base_url=base_url, model_name=model_name, tls=tls, token=api_key,
            timeout=timeout, output_tokens=output_tokens, max_retries=max_retries,
        ) as model:
            yield model
        return

    # ChatOpenAI's client requires an API key even when the gateway authenticates
    # with mTLS alone. Never inherit OPENAI_API_KEY or send a placeholder bearer.
    def headers(request: httpx.Request) -> None:
        for name in ("OpenAI-Organization", "OpenAI-Project"):
            request.headers.pop(name, None)
        if not api_key:
            request.headers.pop("Authorization", None)

    async def async_headers(request: httpx.Request) -> None:
        headers(request)

    # Some corporate gateways select the model from the URL alone. ChatOpenAI
    # expects a model identifier locally, the OpenAI SDK's omit sentinel removes
    # the field on the wire. Preserve the gateway's Chat Completions token option.
    extra_body: dict[str, Any] = {}
    if not model_name:
        from openai import omit

        extra_body["model"] = omit
    if output_tokens is not None:
        extra_body["max_tokens"] = output_tokens
    if disable_thinking:
        extra_body["thinking"] = {"type": "disabled"}
    with (
        safe_http_logging(),
        httpx.Client(verify=tls, timeout=timeout, event_hooks={"request": [headers]}) as client,
    ):
        async with httpx.AsyncClient(verify=tls, timeout=timeout,
                                     event_hooks={"request": [async_headers]}) as async_client:
            yield ChatOpenAI(
                model=model_name or "gateway", base_url=base_url,
                api_key=SecretStr(api_key or "mtls"),
                use_responses_api=False, http_client=client, http_async_client=async_client,
                timeout=timeout, max_retries=max_retries, cache=False,
                extra_body=extra_body or None,
            )
