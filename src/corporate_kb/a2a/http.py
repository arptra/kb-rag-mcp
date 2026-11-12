"""A2A wire endpoint and separately protected agent management routes."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from corporate_kb.a2a.service import PROTOCOL_VERSION, A2AService, RpcError
from corporate_kb.config import Settings

Authorization = Callable[[Request], Awaitable[bool]]


def response(payload: object, status: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status,
        headers={"Cache-Control": "no-store", "A2A-Version": PROTOCOL_VERSION},
    )


async def body(request: Request, limit: int) -> dict[str, Any]:
    def invalid_number(value: str) -> NoReturn:
        raise json.JSONDecodeError("Non-finite numbers are not JSON", value, 0)

    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("Request body exceeds the configured size limit")
    parsed = json.loads(data, parse_constant=invalid_number)
    if not isinstance(parsed, dict):
        raise ValueError("Request must be a JSON object")
    return parsed


def register_a2a_routes(
    server: FastMCP,
    engine: A2AService,
    settings: Settings,
    *,
    reader_authorized: Authorization,
    manager_authorized: Authorization,
    indexes: Callable[[], list[dict[str, Any]]],
) -> None:
    def info(request: Request) -> dict[str, Any]:
        base = str(request.base_url).rstrip("/")
        return {
            "enabled": True,
            "protocol_version": PROTOCOL_VERSION,
            "endpoint": base + settings.a2a_path,
            "agent_card_url": base + "/.well-known/agent-card.json",
        }

    def issued(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            **info(request),
            "config": {
                "url": info(request)["endpoint"],
                "agentCardUrl": info(request)["agent_card_url"],
                "protocolBinding": "JSONRPC",
                "headers": {
                    "Authorization": "Bearer " + payload["token"],
                    "A2A-Version": PROTOCOL_VERSION,
                },
            },
        }

    @server.custom_route("/.well-known/agent-card.json", methods=["GET"], include_in_schema=False)
    async def card(request: Request) -> JSONResponse:
        return response(engine.card(info(request)["endpoint"]))

    @server.custom_route(settings.a2a_path, methods=["POST"], include_in_schema=False)
    async def rpc(request: Request) -> Response:
        started = time.monotonic()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        agent = (
            await asyncio.to_thread(engine.store.authenticate, token)
            if scheme.lower() == "bearer"
            else None
        )
        if agent is None:
            # Invalid credentials cannot impersonate an agent or pollute its counter.
            return JSONResponse(
                {"error": "invalid or expired A2A token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )
        method, request_id, summary = "unknown", None, {}
        error: RpcError | None = None
        result: dict[str, Any] | None = None
        try:
            version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
            if version != PROTOCOL_VERSION:
                raise RpcError(-32009, "Set A2A-Version: 1.0")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                raise RpcError(-32005, "Content-Type must be application/json")
            try:
                payload = await body(request, settings.a2a_max_request_bytes)
            except json.JSONDecodeError:
                raise
            except ValueError as exc:
                raise RpcError(-32600, str(exc)) from None
            request_id = payload.get("id")
            method = payload.get("method", "unknown")
            if (
                payload.get("jsonrpc") != "2.0"
                or not isinstance(method, str)
                or not method
                or "id" not in payload
                or isinstance(request_id, (bool, list, dict))
            ):
                raise RpcError(-32600, "Expected a JSON-RPC 2.0 request with an id")
            params = payload.get("params", {})
            if not isinstance(params, dict):
                raise RpcError(-32602, "params must be an object")
            if method == "SendMessage":
                command, _, _ = engine.command(params)
                summary = {
                    key: command[key]
                    for key in ("query", "action", "index_id", "version_scope")
                    if key in command
                }
                summary.setdefault("version_scope", "latest")
            elif params.get("id"):
                summary = {"task_id": params["id"]}
            # Reading a streamed body may outlive a token rotation or revocation.
            agent = await asyncio.to_thread(engine.store.authenticate, token)
            if agent is None:
                return response({"error": "A2A access is no longer active"}, 401)
            result = await asyncio.to_thread(engine.dispatch, agent, method, params)
            if result.get("task", {}).get("id"):
                summary["task_id"] = result["task"]["id"]
        except json.JSONDecodeError:
            error = RpcError(-32700, "Parse error")
        except RpcError as exc:
            error = exc
        except KeyError:
            error = RpcError(-32001, "Task not found")
        except PermissionError as exc:
            error = RpcError(-32004, str(exc))
        except (ValueError, TypeError) as exc:
            error = RpcError(-32602, str(exc))
        except Exception:
            error = RpcError(-32603, "Internal error")
        failed_task = (result or {}).get("task", {}).get("status", {}).get(
            "state"
        ) == "TASK_STATE_FAILED"
        await asyncio.to_thread(
            engine.store.record,
            agent,
            method,
            "error" if error or failed_task else "success",
            (time.monotonic() - started) * 1000,
            summary,
            str(error) if error else "Knowledge task failed" if failed_task else None,
        )
        if error:
            return response(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": error.code, "message": str(error)},
                }
            )
        return response({"jsonrpc": "2.0", "id": request_id, "result": result})

    @server.custom_route("/auth/a2a/status", methods=["GET"], include_in_schema=False)
    async def status(request: Request) -> JSONResponse:
        manager = await manager_authorized(request)
        return response(
            {**info(request), "can_manage": manager, "indexes": indexes() if manager else []}
        )

    @server.custom_route("/admin/api/a2a", methods=["GET"], include_in_schema=False)
    async def overview(request: Request) -> JSONResponse:
        manager = await manager_authorized(request)
        if not manager and not await reader_authorized(request):
            return response({"error": "unauthorized"}, 401)
        return response(
            {
                **info(request),
                "can_manage": manager,
                "indexes": indexes(),
                "agents": await asyncio.to_thread(engine.store.agents),
                "stats": await asyncio.to_thread(engine.store.stats),
                **await asyncio.to_thread(engine.store.calls),
            }
        )

    @server.custom_route("/admin/api/a2a/calls", methods=["GET"], include_in_schema=False)
    async def calls(request: Request) -> JSONResponse:
        if not await manager_authorized(request) and not await reader_authorized(request):
            return response({"error": "unauthorized"}, 401)
        try:
            return response(
                await asyncio.to_thread(
                    engine.store.calls,
                    cursor=int(request.query_params.get("cursor", "0")),
                    limit=int(request.query_params.get("limit", "100")),
                    agent_id=request.query_params.get("agent_id"),
                )
            )
        except ValueError as exc:
            return response({"error": str(exc)}, 400)

    @server.custom_route("/admin/api/a2a/agents", methods=["POST"], include_in_schema=False)
    async def create(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return response(
                {"error": "Administrator authentication required"},
                401 if settings.access_enabled else 403,
            )
        try:
            data = await body(request, 8192)
            if not await manager_authorized(request):
                return response({"error": "Administrator session expired"}, 401)
            ids = data.get("index_ids", [])
            if not isinstance(ids, list) or not all(
                isinstance(item, str) and engine.catalog.has_index(item) for item in ids
            ):
                raise ValueError("index_ids must contain existing indexes")
            value = await asyncio.to_thread(
                engine.store.issue, data.get("name", ""), ids, data.get("expires_in_days", 30)
            )
            return response(issued(request, value), 201)
        except (ValueError, TypeError) as exc:
            return response({"error": str(exc)}, 400)

    @server.custom_route(
        "/admin/api/a2a/agents/{agent_id}/rotate", methods=["POST"], include_in_schema=False
    )
    async def rotate(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return response(
                {"error": "Administrator authentication required"},
                401 if settings.access_enabled else 403,
            )
        try:
            data = await body(request, 8192)
            if not await manager_authorized(request):
                return response({"error": "Administrator session expired"}, 401)
            value = await asyncio.to_thread(
                engine.store.rotate,
                request.path_params["agent_id"],
                data.get("expires_in_days", 30),
            )
            return response(issued(request, value))
        except KeyError:
            return response({"error": "Agent not found"}, 404)
        except (ValueError, TypeError) as exc:
            return response({"error": str(exc)}, 400)

    @server.custom_route(
        "/admin/api/a2a/agents/{agent_id}/revoke", methods=["POST"], include_in_schema=False
    )
    async def revoke(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return response(
                {"error": "Administrator authentication required"},
                401 if settings.access_enabled else 403,
            )
        try:
            return response(
                {
                    "agent": await asyncio.to_thread(
                        engine.store.revoke, request.path_params["agent_id"]
                    )
                }
            )
        except KeyError:
            return response({"error": "Agent not found"}, 404)
