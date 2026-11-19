"""Administrative controls; OAuth URLs never appear on the external agent surface."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from corporate_kb.a2a.store import A2AStore


def register_runtime_routes(
    server: FastMCP,
    controller: Any,
    store: A2AStore,
    *,
    manager_authorized: Callable[[Request], Awaitable[bool]],
    reader_authorized: Callable[[Request], Awaitable[bool]],
) -> None:
    def reply(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(
            payload,
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @server.custom_route("/admin/api/a2a/runtime", methods=["GET"], include_in_schema=False)
    async def runtime_status(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return reply({"error": "Administrator access required"}, 401)
        return reply(await asyncio.to_thread(controller.status))

    @server.custom_route("/admin/api/a2a/runtime/enable", methods=["POST"], include_in_schema=False)
    async def enable(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return reply({"error": "Administrator access required"}, 401)
        return reply(await asyncio.to_thread(controller.enable))

    @server.custom_route(
        "/admin/api/a2a/runtime/disable", methods=["POST"], include_in_schema=False
    )
    async def disable(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return reply({"error": "Administrator access required"}, 401)
        return reply(await asyncio.to_thread(controller.disable))

    @server.custom_route("/admin/api/a2a/events", methods=["GET"], include_in_schema=False)
    async def events(request: Request) -> JSONResponse:
        if not await reader_authorized(request):
            return reply({"error": "Access required"}, 401)
        try:
            data = await asyncio.to_thread(
                store.events,
                cursor=int(request.query_params.get("cursor", "0")),
                limit=int(request.query_params.get("limit", "100")),
                agent_id=request.query_params.get("agent_id"),
            )
            return reply(data)
        except ValueError as exc:
            return reply({"error": str(exc)}, 400)

    @server.custom_route("/admin/api/a2a/events", methods=["DELETE"], include_in_schema=False)
    async def clear_events(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return reply({"error": "Administrator access required"}, 401)
        try:
            return reply(await asyncio.to_thread(
                store.clear_events, request.query_params.get("agent_id")
            ))
        except ValueError as exc:
            return reply({"error": str(exc)}, 400)
