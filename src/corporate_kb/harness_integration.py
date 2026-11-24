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
from agent_harness.contracts import Evidence, Principal
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationStore
from agent_harness.model_connection import ModelPolicyStop
from agent_harness.runtime import HarnessRuntime
from corporate_kb.access.http import AccessControl
from corporate_kb.agents.activation import GigaCodeAgentController
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodePolicyStop


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
            id=str(item.get("chunk_id") or item["document_id"]),
            document_id=str(item["document_id"]),
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
        # The portal's MCP get_document deliberately compresses to 800 tokens.
        # Harness owns its own read/context budget and needs the original content
        # for an investigation, rather than another search-sized excerpt.
        service = self.catalog.service_for(index_id)
        document = await asyncio.to_thread(service.get_document, document_id)
        result = document.model_dump(mode="json")
        await self._check(principal, cancel)
        return self._evidence(result, index_id)


class GigaCodeDecisionModel:
    """Public model port over the existing launcher; keep login details administrative."""

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
            raise ModelPolicyStop("refusal") from None


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
        source=CatalogSource(catalog, check),
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
