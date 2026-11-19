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

from corporate_kb.a2a.compat import LEGACY_METHOD_MAP, legacy_card, legacy_request, legacy_result
from corporate_kb.a2a.mcp_gateway import (
    cors_headers,
    is_mcp_method,
    mcp_config,
    mcp_response,
    origin_allowed,
    preflight,
)
from corporate_kb.a2a.service import (
    PROTOCOL_VERSION,
    SUPPORTED_METHODS,
    A2AService,
    RpcError,
    connection_contract,
    method_not_found,
)
from corporate_kb.config import Settings

Authorization = Callable[[Request], Awaitable[bool]]


def response(
    payload: object, status: int = 200, *, version: str = PROTOCOL_VERSION
) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status,
        headers={"Cache-Control": "no-store", "A2A-Version": version},
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
            "supported_protocol_versions": ["1.0", "0.3"],
            "supported_methods": list(SUPPORTED_METHODS),
            "endpoint": base + settings.a2a_path,
            "agent_card_url": base + "/.well-known/agent-card.json",
        }

    def issued(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            **info(request),
            "mcp_config": mcp_config(info(request)["endpoint"], payload["token"]),
            "config": {
                **connection_contract(),
                "url": info(request)["endpoint"],
                "agentCardUrl": info(request)["agent_card_url"],
                "legacyAgentCardUrl": info(request)["agent_card_url"] + "?A2A-Version=0.3",
                "protocolBinding": "JSONRPC",
                **mcp_config(info(request)["endpoint"], payload["token"]),
                "headers": {
                    "Authorization": "Bearer " + payload["token"],
                    "Content-Type": "application/json",
                    "A2A-Version": PROTOCOL_VERSION,
                },
            },
        }

    @server.custom_route("/.well-known/agent-card.json", methods=["GET"], include_in_schema=False)
    async def card(request: Request) -> JSONResponse:
        version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
        if version in {"0.3", "0.3.0"}:
            return response(legacy_card(engine.card(info(request)["endpoint"])), version="0.3")
        if version and version != PROTOCOL_VERSION:
            return response({"error": "Supported A2A versions: 1.0, 0.3"}, 400)
        return response(engine.card(info(request)["endpoint"]))

    @server.custom_route("/.well-known/agent.json", methods=["GET"], include_in_schema=False)
    async def legacy_discovery(request: Request) -> JSONResponse:
        return response(legacy_card(engine.card(info(request)["endpoint"])), version="0.3")

    @server.custom_route(settings.a2a_path, methods=["GET"], include_in_schema=False)
    async def connection_help(request: Request) -> Response:
        # An agent given only URL/token can discover the request contract instead
        # of guessing RPC method names. This contains no credentials or index data.
        if "text/event-stream" in request.headers.get("accept", ""):
            # MCP permits a stateless JSON transport without a server event stream.
            return Response(
                status_code=405 if origin_allowed(request, settings.a2a_allowed_origins) else 403,
                headers={"Allow": "POST", "Cache-Control": "no-store",
                         **cors_headers(request, settings.a2a_allowed_origins)},
            )
        version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
        if version not in {None, "", "1.0", "0.3", "0.3.0"}:
            return response({"error": "Supported A2A versions: 1.0, 0.3"}, 400)
        selected = "0.3" if version in {"0.3", "0.3.0"} else "1.0"
        return response(
            {
                **connection_contract(selected),
                "url": info(request)["endpoint"],
                "agentCardUrl": info(request)["agent_card_url"]
                + ("?A2A-Version=0.3" if selected == "0.3" else ""),
                "protocolBinding": "JSONRPC",
                "httpMethod": "POST",
                **mcp_config(info(request)["endpoint"], "<agent-token>"),
                "headers": {
                    "Authorization": "Bearer <agent-token>",
                    "Content-Type": "application/json",
                    "A2A-Version": selected,
                },
            },
            version=selected,
        )

    @server.custom_route(settings.a2a_path, methods=["OPTIONS"], include_in_schema=False)
    async def mcp_preflight(request: Request) -> Response:
        return preflight(request, settings.a2a_allowed_origins)

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
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store",
                         **cors_headers(request, settings.a2a_allowed_origins)},
            )
        method, request_id, summary = "unknown", None, {}
        wire_version = PROTOCOL_VERSION
        error: RpcError | None = None
        result: dict[str, Any] | None = None
        try:
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
            if is_mcp_method(method):
                return await mcp_response(request, payload, engine, agent, token, started,
                                          settings.a2a_allowed_origins)
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
            # Decode the envelope before version checks so failed calls retain
            # their actual method/id in the operation journal and JSON-RPC error.
            version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version")
            if not version and method in SUPPORTED_METHODS:
                raise RpcError(-32009, "Set A2A-Version: 1.0", connection_contract())
            if version not in {None, "", "1.0", "0.3", "0.3.0"}:
                raise RpcError(-32009, "Supported A2A versions: 1.0, 0.3", connection_contract())
            wire_version = "1.0" if version == "1.0" else "0.3"
            dispatch_method = method
            if wire_version == "0.3":
                if method in SUPPORTED_METHODS:
                    raise RpcError(
                        -32601, "A2A 1.0 method requires A2A-Version: 1.0. "
                        "For 0.3 use message/send, tasks/get or tasks/cancel.",
                        connection_contract(),
                    )
                if method not in LEGACY_METHOD_MAP:
                    raise method_not_found(method, version="0.3")
                dispatch_method, params = legacy_request(method, params)
            elif method in LEGACY_METHOD_MAP:
                raise method_not_found(method)
            if dispatch_method == "SendMessage":
                command, _, _ = engine.command(params)
                summary = {
                    key: command[key]
                    for key in ("query", "action", "index_id", "version_scope")
                    if key in command
                }
                summary["version_scope"] = "latest"
            elif params.get("id"):
                summary = {"task_id": params["id"]}
            # Reading a streamed body may outlive a token rotation or revocation.
            agent = await asyncio.to_thread(engine.store.authenticate, token)
            if agent is None:
                return response({"error": "A2A access is no longer active"}, 401)
            result = await asyncio.to_thread(engine.dispatch, agent, dispatch_method, params)
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
                    "error": {
                        "code": error.code, "message": str(error),
                        **({"data": error.data} if error.data is not None else {}),
                    },
                },
                version=wire_version,
            )
        if wire_version == "0.3" and result is not None:
            result = legacy_result(method, result)
        return response(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, version=wire_version
        )

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

    @server.custom_route("/admin/api/a2a/calls", methods=["DELETE"], include_in_schema=False)
    async def clear_calls(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return response(
                {"error": "Administrator authentication required"},
                401 if settings.access_enabled else 403,
            )
        try:
            return response(await asyncio.to_thread(
                engine.store.clear_calls, request.query_params.get("agent_id")
            ))
        except ValueError as exc:
            return response({"error": str(exc)}, 400)

    @server.custom_route(
        "/admin/api/a2a/agents/{agent_id}", methods=["DELETE"], include_in_schema=False
    )
    async def delete_agent(request: Request) -> JSONResponse:
        if not await manager_authorized(request):
            return response(
                {"error": "Administrator authentication required"},
                401 if settings.access_enabled else 403,
            )
        try:
            return response(await asyncio.to_thread(
                engine.delete_agent, request.path_params["agent_id"]
            ))
        except KeyError:
            return response({"error": "Agent not found"}, 404)

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
