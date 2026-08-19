"""HTTP MCP transport, JSON query API, and graph UI in one process."""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import get_args

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp

from gigacode_graph.config import GraphSettings
from gigacode_graph.mcp_server import create_mcp_server
from gigacode_graph.models import NodeType
from gigacode_graph.service import GraphService
from gigacode_graph.store import JsonGraphStore
from gigacode_graph.tools import GraphTools
from gigacode_graph.ui import GRAPH_HTML

logger = logging.getLogger(__name__)
_READ_SCOPE = "code-graph:read"


class ConstantTimeGraphTokenVerifier(TokenVerifier):
    def __init__(self, token: str) -> None:
        super().__init__(required_scopes=[_READ_SCOPE])
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="gigacode-cli",
            subject="repository-graph-reader",
            scopes=[_READ_SCOPE],
        )


def _authorized(request: Request, token: str | None) -> bool:
    if token is None:
        return True
    scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
    return (
        bool(separator) and scheme.lower() == "bearer" and secrets.compare_digest(supplied, token)
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer scope="code-graph:read"'},
    )


def _integer(request: Request, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name)
    try:
        value = default if raw is None or raw == "" else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if value else None


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, KeyError):
        return JSONResponse({"error": str(exc.args[0])}, status_code=404)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, RuntimeError):
        return JSONResponse({"error": str(exc)}, status_code=409)
    logger.exception("Graph API request failed", exc_info=exc)
    return JSONResponse({"error": "internal server error"}, status_code=500)


def create_http_server(
    service: GraphService,
    settings: GraphSettings,
) -> FastMCP:
    """Create a transport-neutral FastMCP object with UI and JSON routes attached."""
    token = settings.validate_http_security()
    auth = ConstantTimeGraphTokenVerifier(token) if token else None
    tools = GraphTools(service)
    server = create_mcp_server(service, auth=auth)

    @server.custom_route("/", methods=["GET"], include_in_schema=False)
    @server.custom_route("/graph", methods=["GET"], include_in_schema=False)
    async def graph_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(
            GRAPH_HTML,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'none'; frame-ancestors 'none'"
                ),
            },
        )

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        stats = service.overview()
        return JSONResponse(
            {
                "status": "ok",
                "generated_at": stats["generated_at"],
                "nodes": stats["node_count"],
                "edges": stats["edge_count"],
            }
        )

    @server.custom_route("/api/overview", methods=["GET"], include_in_schema=False)
    async def overview(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            return JSONResponse(await asyncio.to_thread(tools.overview))
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/graph", methods=["GET"], include_in_schema=False)
    async def graph(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            payload = await asyncio.to_thread(
                service.graph,
                view=_optional(request, "view") or "services",
                service=_optional(request, "service"),
                depth=_integer(request, "depth", 1, 0, 10),
                limit=_integer(request, "limit", 3_000, 1, 20_000),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/search", methods=["GET"], include_in_schema=False)
    async def search(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            query = _optional(request, "query")
            if query is None:
                raise ValueError("query must not be empty")
            raw_types = _optional(request, "types")
            node_types: list[NodeType] | None = None
            if raw_types:
                allowed = set(get_args(NodeType))
                values = [item.strip() for item in raw_types.split(",") if item.strip()]
                unknown = sorted(set(values) - allowed)
                if unknown:
                    raise ValueError(f"unknown node types: {', '.join(unknown)}")
                node_types = values  # type: ignore[assignment]
            payload = await asyncio.to_thread(
                tools.search,
                query,
                node_types=node_types,
                service=_optional(request, "service"),
                limit=_integer(request, "limit", 20, 1, 200),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/service", methods=["GET"], include_in_schema=False)
    async def service_details(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            service_name = _optional(request, "service")
            if service_name is None:
                raise ValueError("service must not be empty")
            return JSONResponse(await asyncio.to_thread(tools.service, service_name))
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/dependencies", methods=["GET"], include_in_schema=False)
    async def dependencies(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            service_name = _optional(request, "service")
            if service_name is None:
                raise ValueError("service must not be empty")
            payload = await asyncio.to_thread(
                tools.dependencies,
                service_name,
                direction=_optional(request, "direction") or "outgoing",
                depth=_integer(request, "depth", 1, 1, 10),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/business", methods=["GET"], include_in_schema=False)
    async def business(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            service_name = _optional(request, "service")
            if service_name is None:
                raise ValueError("service must not be empty")
            payload = await asyncio.to_thread(
                tools.business_operations,
                service_name,
                limit=_integer(request, "limit", 100, 1, 1_000),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/data-model", methods=["GET"], include_in_schema=False)
    async def data_model(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            payload = await asyncio.to_thread(
                tools.data_model,
                service=_optional(request, "service"),
                table=_optional(request, "table"),
                limit=_integer(request, "limit", 500, 1, 5_000),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _error(exc)

    @server.custom_route("/api/evidence", methods=["GET"], include_in_schema=False)
    async def evidence(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized()
        try:
            raw_ids = _optional(request, "ids")
            if raw_ids is None:
                raise ValueError("ids must not be empty")
            ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
            return JSONResponse(await asyncio.to_thread(tools.evidence, ids))
        except Exception as exc:
            return _error(exc)

    return server


def create_http_app(service: GraphService, settings: GraphSettings) -> ASGIApp:
    return create_http_server(service, settings).http_app(
        path=settings.mcp_path,
        host_origin_protection=False,
    )


def run_http_server(settings: GraphSettings) -> None:
    """Load the current snapshot and serve UI, JSON API, and Streamable HTTP MCP."""
    logging.basicConfig(level=settings.log_level)
    settings.validate_http_security()
    service = GraphService(JsonGraphStore(settings.store_path))
    server = create_http_server(service, settings)
    logger.info(
        "Starting GigaCode graph UI on http://%s:%d and MCP on %s",
        settings.http_host,
        settings.http_port,
        settings.mcp_path,
    )
    try:
        server.run(
            transport="http",
            show_banner=False,
            host=settings.http_host,
            port=settings.http_port,
            path=settings.mcp_path,
            log_level=settings.log_level,
            host_origin_protection=False,
        )
    except KeyboardInterrupt:
        return


def main() -> None:
    run_http_server(GraphSettings().resolved())


if __name__ == "__main__":
    main()
