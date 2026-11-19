"""Optional host adapter. The detachable agent_harness package never imports this host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Event
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal
from agent_harness.http import create_app
from agent_harness.runtime import HarnessRuntime
from corporate_kb.access.http import AccessControl
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodeModelProfile, GigaCodeRunner


class CatalogSource:
    """Read-only projection of current RAG indexes, with host authorization per read."""

    def __init__(
        self,
        catalog: RagCatalog,
        authorize: Callable[[Principal], Awaitable[bool]],
    ) -> None:
        self.catalog = catalog
        self.authorize = authorize

    async def _check(self, principal: Principal, cancel: Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        if not await self.authorize(principal):
            raise PermissionError("Доступ к источникам отозван. Войдите заново.")

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        await self._check(principal)
        payload = await asyncio.to_thread(self.catalog.payload)
        return [{"id": item["id"], "name": item["name"]} for item in payload["indexes"]]

    @staticmethod
    def _evidence(item: dict[str, Any], index_id: str) -> Evidence:
        return Evidence(
            id=str(item["document_id"]),
            title=str(item.get("title", item["document_id"])),
            text=str(item.get("excerpt") or item.get("text") or item.get("content") or ""),
            index_id=index_id,
            url=item.get("source_url") or None,
            revision=str(item.get("source_version") or item.get("revision_id") or "") or None,
        )

    async def search(
        self,
        *,
        query: str,
        index_id: str,
        limit: int,
        principal: Principal,
        cancel: Event,
    ) -> list[Evidence]:
        await self._check(principal, cancel)
        tool = self.catalog.tools_for(index_id)
        result = await asyncio.to_thread(tool.search, query=query, top_k=min(20, max(1, limit)))
        await self._check(principal, cancel)
        return [self._evidence(item, index_id) for item in result["results"]]

    async def read(
        self,
        *,
        document_id: str,
        index_id: str,
        principal: Principal,
        cancel: Event,
    ) -> Evidence:
        await self._check(principal, cancel)
        tool = self.catalog.tools_for(index_id)
        result = await asyncio.to_thread(tool.get_document, document_id=document_id)
        await self._check(principal, cancel)
        return self._evidence(result, index_id)


class UnavailableModel:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def status(self) -> dict[str, Any]:
        return {"available": False, "name": "GigaCode", "reason": self.reason}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(self.reason)


class GigaCodeDecisionModel:
    """Translate the portable decision port into the independently tested chat adapter."""

    def __init__(self, chat_model: Any) -> None:
        self.chat_model = chat_model

    def status(self) -> dict[str, Any]:
        return {"available": True, "name": "GigaCode", "reason": None}

    async def decide(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: Event,
        max_input_chars: int,
    ) -> dict[str, Any]:
        from langchain_core.messages import (
            AIMessage,
            BaseMessage,
            HumanMessage,
            SystemMessage,
            ToolMessage,
        )

        history: list[BaseMessage] = []
        for message in messages:
            role, content = message["role"], message.get("content", "")
            if role == "system":
                history.append(SystemMessage(content=content))
            elif role == "user":
                history.append(HumanMessage(content=content))
            elif role == "assistant":
                history.append(AIMessage(content=content, tool_calls=message.get("tool_calls", [])))
            elif role == "tool":
                history.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=message["tool_call_id"],
                        status=message.get("status", "success"),
                    )
                )
            else:
                raise ValueError("Unsupported harness conversation role")
        model = self.chat_model.model_copy(update={"max_input_chars": max_input_chars})
        reply = await model.bind_tools(tools, tool_choice="auto" if tools else "none").ainvoke(
            history,
            cancel=cancel,
        )
        return {"content": reply.content, "tool_calls": [dict(call) for call in reply.tool_calls]}


def _load_profile(path: Path) -> GigaCodeModelProfile:
    if path.stat().st_size > 16_384:
        raise ValueError("Model profile is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {
        "name",
        "cli_version",
        "arguments",
        "isolation_notes",
    }:
        raise ValueError("Model profile must contain name, cli_version, arguments, isolation_notes")
    if not isinstance(data["arguments"], list):
        raise ValueError("Model profile arguments must be an array")
    return GigaCodeModelProfile(**{**data, "arguments": tuple(data["arguments"])})


def build_model(settings: Settings) -> GigaCodeDecisionModel | UnavailableModel:
    if settings.harness_model_profile_path is None:
        return UnavailableModel("Модель не настроена. Доступен поиск по источникам.")
    try:
        profile = _load_profile(settings.harness_model_profile_path)
        from corporate_kb.agents.gigacode_model import GigaCodeChatModel

        runner = GigaCodeRunner(settings)
        status = runner.status()
        if not status["available"] or status["version"] != profile.cli_version:
            return UnavailableModel("Проверьте установленный GigaCode и версию профиля запуска.")
        return GigaCodeDecisionModel(GigaCodeChatModel(runner=runner, launch_profile=profile))
    except ImportError:
        return UnavailableModel("Установите дополнительные зависимости модели: extra agents.")
    except (OSError, ValueError, TypeError):
        return UnavailableModel("Проверенный профиль запуска GigaCode недоступен или некорректен.")


def create_embedded_harness(
    settings: Settings,
    catalog: RagCatalog,
    *,
    access: AccessControl | None,
    authorized: Callable[[Request], Awaitable[bool]],
) -> Starlette:
    # Grants remain in this adapter, not in portable state or model prompts. Recheck
    # the original credentials before each source call; revocation takes effect.
    grants: dict[str, Request] = {}

    async def identity(request: Request) -> Principal | None:
        if access is not None:
            user = await asyncio.to_thread(access.user, request)
            if user is None:
                return None
            return Principal("user:" + str(user["id"]))
        if not await authorized(request):
            return None
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        token = settings.mcp_http_bearer_token
        if (
            token
            and scheme.lower() == "bearer"
            and secrets.compare_digest(supplied, token.get_secret_value())
        ):
            return Principal("shared-token:" + hashlib.sha256(supplied.encode()).hexdigest(), True)
        password = settings.admin_password
        supplied = request.headers.get("x-kb-admin-password", "")
        if password and supplied and secrets.compare_digest(supplied, password.get_secret_value()):
            return Principal("shared-admin:" + hashlib.sha256(supplied.encode()).hexdigest(), True)
        if (token and token.get_secret_value()) or (password and password.get_secret_value()):
            return None
        # Password-free development is a single local workspace, never a personal
        # identity inferred from a caller-supplied user id or arbitrary header.
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            return None
        return Principal("local-development", True)

    async def authenticate(request: Request) -> Principal | None:
        principal = await identity(request)
        if principal is not None:
            if principal.owner_id not in grants and len(grants) >= 1024:
                return None
            grants[principal.owner_id] = request
        return principal

    async def check(principal: Principal) -> bool:
        original = grants.get(principal.owner_id)
        return original is not None and await identity(original) == principal

    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=settings.harness_state_dir),
        source=CatalogSource(catalog, check),
        model=build_model(settings),
    )
    app = create_app(runtime, authenticate)
    app.state.harness_runtime = runtime
    return app


def standalone_providers() -> tuple[CatalogSource, GigaCodeDecisionModel | UnavailableModel]:
    """Optional factory for a separate process reading this portal's configured indexes."""
    from corporate_kb.mcp.tools import KnowledgeTools
    from corporate_kb.service import create_service
    from corporate_kb.usage import UsageTracker

    settings = Settings().resolved()
    service = create_service(settings)
    service.load_read_index()
    usage = UsageTracker()
    catalog = RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)

    async def allowed(_principal: Principal) -> bool:
        # Standalone entrypoint authenticates its own configured account/token.
        return True

    return CatalogSource(catalog, allowed), build_model(settings)
