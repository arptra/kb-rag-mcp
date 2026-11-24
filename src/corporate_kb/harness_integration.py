"""Optional host adapter. The detachable agent_harness package never imports this host."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from threading import Event
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, ModelToolFormatError, Principal
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationStore
from agent_harness.model_connection import ModelPolicyStop, ModelTransientError
from agent_harness.runtime import HarnessRuntime
from corporate_kb.access.http import AccessControl
from corporate_kb.agents.activation import GigaCodeAgentController, GigaCodeDecisionError
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import (
    GigaCodePolicyStop,
    GigaCodeTimedOut,
    GigaCodeTransientError,
)
from corporate_kb.harness_graph import GRAPH_INDEX, read_graph, search_graph
from corporate_kb.models import SearchFilters


class CatalogSource:
    """Read-only projection of current RAG indexes, with host authorization per read."""

    supports_passage_retrieval = True

    def __init__(
        self,
        catalog: RagCatalog,
        authorize: Callable[[Principal], Awaitable[bool]],
        authorize_graph: Callable[[Principal], Awaitable[bool]] | None = None,
    ) -> None:
        self.catalog = catalog
        self.authorize = authorize
        self.authorize_graph = authorize_graph

    async def _check(self, principal: Principal, cancel: Event | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError
        if not await self.authorize(principal):
            raise PermissionError("Доступ к источникам отозван. Войдите заново.")

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        await self._check(principal)
        indexes = await asyncio.to_thread(self.catalog.index_directory)
        await self._check(principal)
        if self.authorize_graph is not None and await self.authorize_graph(principal):
            indexes.append({"id": GRAPH_INDEX, "name": "Граф сервисов"})
        return indexes

    async def _check_graph(self, principal: Principal, cancel: Event) -> None:
        await self._check(principal, cancel)
        if self.authorize_graph is None or not await self.authorize_graph(principal):
            raise PermissionError("Доступ к графу сервисов отозван.")

    @staticmethod
    def _evidence(item: dict[str, Any], index_id: str) -> Evidence:
        return Evidence(
            id=str(item.get("chunk_id") or item["document_id"]),
            document_id=str(item["document_id"]),
            title=str(item.get("title", item["document_id"])),
            text=str(item.get("excerpt") or item.get("text") or item.get("content") or ""),
            index_id=index_id,
            rank=item.get("rank"),
            heading_path=item.get("heading_path") or None,
            source_path=item.get("source_path") or None,
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
        if index_id == GRAPH_INDEX:
            await self._check_graph(principal, cancel)
            items = await asyncio.to_thread(
                search_graph, self.catalog, query, min(20, max(1, limit)),
            )
            await self._check_graph(principal, cancel)
            return items
        await self._check(principal, cancel)
        tool = self.catalog.tools_for(index_id)
        limit = min(20, max(1, limit))
        chunks = await asyncio.to_thread(
            tool.search_candidates, query, limit, filters=SearchFilters(status="current"),
            full_text=True,
        )
        await self._check(principal, cancel)
        # Prefer one hit per page. When a small number of pages dominates the
        # retrieval, retain further matching sections rather than wasting the budget.
        pages, other_sections = {}, []
        for chunk in chunks:
            if chunk.document_id in pages:
                other_sections.append(chunk)
            else:
                pages[chunk.document_id] = chunk
        selected = [*pages.values(), *other_sections][:limit]
        return [
            self._evidence({**chunk.model_dump(), "rank": rank}, index_id)
            for rank, chunk in enumerate(selected, 1)
        ]

    async def read(
        self,
        *,
        document_id: str,
        index_id: str,
        principal: Principal,
        cancel: Event,
    ) -> Evidence:
        if index_id == GRAPH_INDEX:
            await self._check_graph(principal, cancel)
            item = await asyncio.to_thread(read_graph, self.catalog, document_id)
            await self._check_graph(principal, cancel)
            return item
        await self._check(principal, cancel)
        # The portal's MCP get_document deliberately compresses to 800 tokens.
        # Harness owns its own read/context budget and needs the original content
        # for an investigation, rather than another search-sized excerpt.
        tools = self.catalog.tools_for(index_id)
        document = await asyncio.to_thread(tools.read_current_document, document_id=document_id)
        result = document.model_dump(mode="json")
        await self._check(principal, cancel)
        return self._evidence(result, index_id)


class GigaCodeDecisionModel:
    """Public model port over the existing launcher; keep login details administrative."""

    supports_search_assessment = True

    def __init__(self, controller: GigaCodeAgentController, *, owns_controller: bool) -> None:
        self.model_control = controller
        self.owns_controller = owns_controller

    def status(self) -> dict[str, Any]:
        state = self.model_control.status()
        return {
            "name": "GigaCode",
            "enabled": bool(state.get("enabled")),
            "available": bool(state.get("available")),
            "state": state.get("state"),
            "reason": state.get("message", ""),
        }

    def close(self) -> None:
        if self.owns_controller:
            self.model_control.stop()

    async def decide(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        cancel: Event,
        max_input_chars: int,
    ) -> dict[str, Any]:
        try:
            return await self.model_control.decide(
                messages=messages, tools=tools, cancel=cancel, max_input_chars=max_input_chars,
            )
        except GigaCodePolicyStop:
            raise ModelPolicyStop("refusal", field="GigaCode") from None
        except GigaCodeDecisionError:
            raise ModelToolFormatError("GigaCode вернул некорректную структуру ответа.") from None
        except GigaCodeTimedOut:
            raise ModelTransientError("GigaCode не успел ответить на этот шаг.") from None
        except GigaCodeTransientError:
            raise ModelTransientError("Провайдер GigaCode временно недоступен.") from None


def build_model(
    settings: Settings,
    *,
    controller: GigaCodeAgentController | None = None,
) -> GigaCodeDecisionModel:
    return GigaCodeDecisionModel(
        controller if controller is not None else GigaCodeAgentController(settings),
        owns_controller=controller is None,
    )


def create_embedded_harness(
    settings: Settings,
    catalog: RagCatalog,
    *,
    access: AccessControl | None,
    authorized: Callable[[Request], Awaitable[bool]],
    manager_authorized: Callable[[Request], Awaitable[bool]],
    graph_authorized: Callable[[Request], Awaitable[bool]] | None = None,
    controller: GigaCodeAgentController | None = None,
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
        # A request presenting both credentials must never cache dashboard privileges
        # under the shared reader's identity.
        password = settings.admin_password
        expected = password.get_secret_value() if password else ""
        supplied = request.headers.get("x-kb-admin-password", "")
        if len(expected) >= 16 and supplied and secrets.compare_digest(supplied, expected):
            return Principal("shared-admin:" + hashlib.sha256(supplied.encode()).hexdigest(), True)
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        token = settings.mcp_http_bearer_token
        if (
            token
            and scheme.lower() == "bearer"
            and secrets.compare_digest(supplied, token.get_secret_value())
        ):
            return Principal("shared-token:" + hashlib.sha256(supplied.encode()).hexdigest(), True)
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

    async def check_graph(principal: Principal) -> bool:
        original = grants.get(principal.owner_id)
        return (original is not None and graph_authorized is not None
                and await graph_authorized(original))

    async def control_authorized(request: Request) -> bool:
        # Control-plane authority is explicit even in legacy reader/development
        # deployments. A shared RAG token is never an administrator credential.
        if access is not None:
            return await asyncio.to_thread(access.administrator, request) is not None
        password = settings.admin_password
        expected = password.get_secret_value() if password is not None else ""
        supplied = request.headers.get("x-kb-admin-password", "")
        return len(expected) >= 16 and bool(supplied) and secrets.compare_digest(supplied, expected)

    model = build_model(settings, controller=controller)
    integrations = IntegrationHub(IntegrationStore(settings.harness_state_dir), check)
    runtime = HarnessRuntime(
        HarnessSettings(
            enabled=True,
            state_dir=settings.harness_state_dir,
            authentication_timeout_seconds=settings.gigacode_auth_timeout_seconds,
        ),
        source=CatalogSource(catalog, check, check_graph),
        model=model,
        tools=integrations,
    )
    app = create_app(
        runtime,
        authenticate,
        model_control=model.model_control,
        authorize_model_manager=manager_authorized,
        authorize_control_manager=control_authorized,
        integrations=integrations,
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        try:
            async with original_lifespan(application):
                yield
        finally:
            await asyncio.to_thread(model.close)

    app.router.lifespan_context = lifespan
    app.state.harness_runtime = runtime
    return app


def standalone_providers() -> tuple[CatalogSource, GigaCodeDecisionModel]:
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
