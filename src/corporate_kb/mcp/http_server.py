"""Authenticated FastMCP Streamable HTTP entry point for remote clients."""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.types import ASGIApp

from corporate_kb.admin import AdminController
from corporate_kb.config import Settings
from corporate_kb.mcp.admin_ui import ADMIN_HTML
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService, configure_logging, create_service
from corporate_kb.usage import UsageTracker

logger = logging.getLogger(__name__)
_READ_SCOPE = "kb:read"


def _authorized(request: Request, token: str) -> bool:
    scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
    return (
        bool(separator)
        and scheme.lower() == "bearer"
        and secrets.compare_digest(supplied, token)
    )


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        {"error": "unauthorized"},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer scope="kb:read"'},
    )


def _optional_query(request: Request, name: str, default: str | None = None) -> str | None:
    value = request.query_params.get(name)
    return value if value not in (None, "") else default


def _integer_query(
    request: Request,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = request.query_params.get(name)
    try:
        value = default if raw is None or raw == "" else int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_query(request: Request, name: str) -> float | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _api_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, KeyError):
        return JSONResponse({"error": str(exc.args[0])}, status_code=404)
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, RuntimeError):
        return JSONResponse({"error": str(exc)}, status_code=409)
    logger.exception("Remote knowledge API request failed", exc_info=exc)
    return JSONResponse({"error": "internal server error"}, status_code=500)


def _admin_authorized(request: Request, settings: Settings) -> bool:
    configured = settings.admin_password
    if configured is None:
        return False
    expected = configured.get_secret_value()
    supplied = request.headers.get("x-kb-admin-password", "")
    return len(expected) >= 16 and bool(supplied) and secrets.compare_digest(supplied, expected)


def _admin_denied(settings: Settings) -> JSONResponse:
    message = (
        "admin dashboard is disabled"
        if settings.admin_password is None
        else "invalid admin password"
    )
    return JSONResponse({"error": message}, status_code=403)


def validate_http_settings(settings: Settings) -> str:
    """Validate authenticated remote-server settings and return the raw token."""
    secret = settings.mcp_http_bearer_token
    if secret is None:
        raise ValueError(
            "KB_MCP_HTTP_BEARER_TOKEN is required for HTTP mode. "
            "Generate one with: openssl rand -hex 32"
        )
    token = secret.get_secret_value()
    if len(token) < 32:
        raise ValueError("KB_MCP_HTTP_BEARER_TOKEN must contain at least 32 characters")
    return token


class ConstantTimeTokenVerifier(TokenVerifier):
    """Validate one opaque Bearer token without exposing it in server metadata."""

    def __init__(self, token: str) -> None:
        super().__init__(required_scopes=[_READ_SCOPE])
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id="qwen-cli",
            subject="corporate-kb-reader",
            scopes=[_READ_SCOPE],
        )


def create_http_server(service: KnowledgeService, settings: Settings) -> FastMCP:
    """Create an authenticated FastMCP server with a public health route."""
    token = validate_http_settings(settings)
    usage = UsageTracker()
    tools = KnowledgeTools(service, usage=usage)
    managed_tools = ManagedToolRegistry(settings.managed_tools_path, tools)
    admin = AdminController(service, usage)
    server = create_mcp_server(
        service,
        auth=ConstantTimeTokenVerifier(token),
        knowledge_tools=tools,
        managed_tools=managed_tools,
    )

    @server.custom_route("/admin", methods=["GET"], include_in_schema=False)
    async def admin_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(
            ADMIN_HTML,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; img-src 'none'; frame-ancestors 'none'"
                ),
            },
        )

    @server.custom_route("/admin/api/overview", methods=["GET"], include_in_schema=False)
    async def admin_overview(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await asyncio.to_thread(admin.overview)
            payload["managed_tools"] = managed_tools.payload()
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/documents", methods=["POST"], include_in_schema=False)
    async def admin_upload_document(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            relative_path = payload.get("path")
            content = payload.get("content")
            overwrite = payload.get("overwrite", False)
            if not isinstance(relative_path, str) or not isinstance(content, str):
                raise ValueError("path and content must be strings")
            if not isinstance(overwrite, bool):
                raise ValueError("overwrite must be a boolean")
            result = await asyncio.to_thread(
                admin.upload_document,
                relative_path=relative_path,
                content=content,
                overwrite=overwrite,
            )
            return JSONResponse(result, status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/index", methods=["POST"], include_in_schema=False)
    async def admin_start_index(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await asyncio.to_thread(admin.start_index)
            return JSONResponse(payload, status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/tools", methods=["POST"], include_in_schema=False)
    async def admin_save_tool(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            definition = ManagedToolDefinition.model_validate(payload)
            replacement = managed_tools.create_tool(definition)
            existing = {item.name for item in managed_tools.list()}
            if definition.name in existing and not settings.mcp_minimal_tools:
                server.local_provider.remove_tool(definition.name)
            managed_tools.upsert(definition)
            if not settings.mcp_minimal_tools:
                server.add_tool(replacement)
            return JSONResponse(definition.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/tools/delete", methods=["POST"], include_in_schema=False)
    async def admin_delete_tool(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            name = payload.get("name") if isinstance(payload, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("name must be a non-empty string")
            managed_tools.delete(name)
            if not settings.mcp_minimal_tools:
                server.local_provider.remove_tool(name)
            return JSONResponse({"status": "deleted", "name": name})
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health_check(_request: Request) -> JSONResponse:
        stats = await asyncio.to_thread(service.stats)
        return JSONResponse(
            {
                "status": "ok",
                "documents": stats.document_count,
                "chunks": stats.chunk_count,
                "embedding_provider": stats.embedding_provider,
            }
        )

    @server.custom_route("/api/v1/search", methods=["GET"], include_in_schema=False)
    async def api_search(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            query = request.query_params.get("query", "")
            if not query.strip():
                raise ValueError("query must not be empty")
            payload = await asyncio.to_thread(
                tools.search,
                query=query,
                top_k=_integer_query(request, "top_k", 3, minimum=1, maximum=20),
                min_score=_float_query(request, "min_score"),
                service=_optional_query(request, "service"),
                domain=_optional_query(request, "domain"),
                document_type=_optional_query(request, "document_type"),
                status=_optional_query(request, "status", "current"),
                authority=_optional_query(request, "authority"),
                source_type=_optional_query(request, "source_type"),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/document", methods=["GET"], include_in_schema=False)
    async def api_document(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            document_id = request.query_params.get("document_id", "")
            if not document_id:
                raise ValueError("document_id must not be empty")
            payload = await asyncio.to_thread(
                tools.get_document,
                document_id,
                _integer_query(
                    request,
                    "max_tokens",
                    settings.document_context_tokens,
                    minimum=1,
                    maximum=settings.document_context_tokens,
                ),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/chunk", methods=["GET"], include_in_schema=False)
    async def api_chunk(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            chunk_id = request.query_params.get("chunk_id", "")
            if not chunk_id:
                raise ValueError("chunk_id must not be empty")
            payload = await asyncio.to_thread(
                tools.get_chunk,
                chunk_id,
                _integer_query(
                    request,
                    "max_tokens",
                    settings.document_context_tokens,
                    minimum=1,
                    maximum=settings.document_context_tokens,
                ),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/api/v1/admin/context-benchmark",
        methods=["POST"],
        include_in_schema=False,
    )
    async def api_context_benchmark(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            password = request.headers.get("x-kb-benchmark-password", "")
            payload = await asyncio.to_thread(tools.run_context_benchmark, password)
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/documents", methods=["GET"], include_in_schema=False)
    async def api_documents(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            payload = await asyncio.to_thread(
                tools.list_documents,
                service=_optional_query(request, "service"),
                domain=_optional_query(request, "domain"),
                document_type=_optional_query(request, "document_type"),
                status=_optional_query(request, "status"),
                limit=_integer_query(request, "limit", 50, minimum=1, maximum=500),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/stats", methods=["GET"], include_in_schema=False)
    async def api_stats(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            payload = await asyncio.to_thread(tools.stats)
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/tools", methods=["GET"], include_in_schema=False)
    async def api_managed_tools(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        return JSONResponse(managed_tools.payload())

    @server.custom_route("/api/v1/tools/call", methods=["POST"], include_in_schema=False)
    async def api_call_managed_tool(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            name = payload.get("name")
            arguments = payload.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("name must be a string and arguments must be an object")
            result = await asyncio.to_thread(managed_tools.execute, name, arguments)
            return JSONResponse(result)
        except Exception as exc:
            return _api_error(exc)

    return server


def create_http_app(service: KnowledgeService, settings: Settings) -> ASGIApp:
    """Build the authenticated FastMCP ASGI app for tests or external ASGI servers."""
    server = create_http_server(service, settings)
    return server.http_app(
        path=settings.mcp_http_path,
        host_origin_protection=False,
    )


def main() -> None:
    """Preload the index, then serve authenticated FastMCP Streamable HTTP."""
    settings = Settings().resolved()
    configure_logging(settings.log_level)
    validate_http_settings(settings)

    service = create_service(settings)
    stats = service.load_read_index()
    logger.info(
        "Preloaded knowledge index: documents=%d chunks=%d provider=%s",
        stats.document_count,
        stats.chunk_count,
        stats.embedding_provider,
    )
    server = create_http_server(service, settings)
    logger.info(
        "Starting authenticated FastMCP HTTP server on %s:%d%s",
        settings.mcp_http_host,
        settings.mcp_http_port,
        settings.mcp_http_path,
    )
    try:
        server.run(
            transport="http",
            show_banner=False,
            host=settings.mcp_http_host,
            port=settings.mcp_http_port,
            path=settings.mcp_http_path,
            log_level=settings.log_level,
            host_origin_protection=False,
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
