"""Authenticated discovery inbox; URL proposals never update integration settings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness.contracts import Principal
from agent_harness.discovery import DiscoveryStore
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings
from agent_harness.runtime import HarnessRuntime


def create_discovery_routes(
    runtime: HarnessRuntime,
    store: DiscoveryStore,
    principal: Any,
    manager: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
    integrations: IntegrationHub | None,
) -> list[Route]:
    async def entries(identity: Principal, run_id: str | None = None) -> list[dict[str, Any]]:
        available = await runtime._indexes(identity)
        # Read through runtime's visibility filter before looking at source text.
        if run_id:
            store.scan(identity.owner_id, await runtime.get(identity, run_id))
        else:
            for row in runtime.store.list(identity.owner_id):
                store.scan(identity.owner_id, runtime._visible(row, available))
        settings = integrations.store.load() if integrations else IntegrationSettings()
        return store.list(
            identity.owner_id, {item["id"] for item in available}, settings, run_id=run_id
        )

    async def listing(request: Request) -> Response:
        identity = await principal(request)
        can_manage = await manager(request)
        all_requests = request.query_params.get("scope") == "all"
        if all_requests:
            if not can_manage:
                raise HTTPException(403, "Harness administrator access required")
            settings = integrations.store.load() if integrations else IntegrationSettings()
            values = store.list(identity.owner_id, set(), settings, requests=True)
        else:
            values = await entries(identity, request.query_params.get("run_id"))
        return JSONResponse(
            {"discoveries": values, "can_manage": can_manage},
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def action(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        if (await read_body(request)).strip() not in {b"", b"{}"}:
            raise HTTPException(400, "This action does not accept a request body")
        resource_id = request.path_params["discovery_id"]
        if not any(row["id"] == resource_id for row in await entries(identity)):
            raise HTTPException(404, "Resource not found")
        action = "requested" if request.url.path.endswith("/request") else "dismissed"
        store.set_status(identity.owner_id, resource_id, action)
        return JSONResponse(
            {"id": resource_id, "status": action}, headers={"Cache-Control": "no-store"}
        )

    return [
        Route("/api/discoveries", listing),
        Route("/api/discoveries/{discovery_id}/request", action, methods=["POST"]),
        Route("/api/discoveries/{discovery_id}/dismiss", action, methods=["POST"]),
    ]
