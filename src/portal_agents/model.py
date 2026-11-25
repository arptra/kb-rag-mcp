"""Build a LangChain chat model from trusted server configuration."""
from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any
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

from corporate_kb.agents.activation import GigaCodeAgentController
from corporate_kb.agents.gigacode_model import GigaCodeChatModel
from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodeModelProfile, GigaCodeRunner


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
) -> AsyncIterator[BaseChatModel]:
    if not base_url:
        if profile_path is not None:
            profile = TypeAdapter(GigaCodeModelProfile).validate_json(profile_path.read_bytes())
            yield GigaCodeChatModel(runner=GigaCodeRunner(settings), launch_profile=profile)
            return
        if controller is None or not controller.status().get("available"):
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
    if not model_name:
        raise ValueError("Укажите имя модели корпоративного API.")
    if key_file and not cert_file:
        raise ValueError("Для клиентского ключа требуется сертификат.")
    tls = ssl.create_default_context(cafile=ca_file)
    if cert_file:
        tls.load_cert_chain(certfile=cert_file, keyfile=key_file)

    # ChatOpenAI's client requires an API key even when the gateway authenticates
    # with mTLS alone. Never inherit OPENAI_API_KEY or send a placeholder bearer.
    def headers(request: httpx.Request) -> None:
        if not api_key:
            request.headers.pop("Authorization", None)

    async def async_headers(request: httpx.Request) -> None:
        headers(request)

    timeout = settings.gigacode_timeout_seconds
    with httpx.Client(verify=tls, timeout=timeout, event_hooks={"request": [headers]}) as client:
        async with httpx.AsyncClient(verify=tls, timeout=timeout,
                                     event_hooks={"request": [async_headers]}) as async_client:
            yield ChatOpenAI(
                model=model_name, base_url=base_url, api_key=SecretStr(api_key or "mtls"),
                use_responses_api=False, http_client=client, http_async_client=async_client,
                timeout=timeout, max_retries=2, cache=False,
            )
