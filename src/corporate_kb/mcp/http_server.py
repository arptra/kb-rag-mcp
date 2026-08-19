"""Authenticated FastMCP Streamable HTTP entry point for remote clients."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp

from corporate_kb.admin import AdminController
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import (
    KnowledgeService,
    configure_logging,
    create_service,
    create_ssot_service,
)
from corporate_kb.usage import UsageTracker

logger = logging.getLogger(__name__)
_READ_SCOPE = "kb:read"
_ADMIN_DIST = Path(__file__).with_name("admin_dist")


def _authorized(request: Request, token: str | None) -> bool:
    if token is None:
        return True
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
        return True
    expected = configured.get_secret_value()
    if not expected:
        return True
    supplied = request.headers.get("x-kb-admin-password", "")
    return len(expected) >= 16 and bool(supplied) and secrets.compare_digest(supplied, expected)


def _admin_denied(settings: Settings) -> JSONResponse:
    return JSONResponse({"error": "invalid admin password"}, status_code=403)


def validate_http_settings(settings: Settings) -> str | None:
    """Return an optional validated token; no token means open HTTP access."""
    secret = settings.mcp_http_bearer_token
    if secret is None:
        return None
    token = secret.get_secret_value()
    if not token:
        return None
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
    """Create a FastMCP server with optional token and admin authentication."""
    token = validate_http_settings(settings)
    usage = UsageTracker()
    ssot_service = None
    if settings.ssot_enabled:
        ssot_service = create_ssot_service(settings, provider=service.provider)
        ssot_stats = ssot_service.load_read_index()
        logger.info(
            "Preloaded global SSOT index: documents=%d chunks=%d",
            ssot_stats.document_count,
            ssot_stats.chunk_count,
        )
    tools = KnowledgeTools(service, ssot_service=ssot_service, usage=usage)
    catalog = RagCatalog(settings, service, tools, usage)
    managed_tools = ManagedToolRegistry(
        settings.managed_tools_path,
        tools,
        index_tools=catalog.tools_for,
        index_exists=catalog.has_index,
    )
    admin = AdminController(service, usage)
    server = create_mcp_server(
        service,
        auth=ConstantTimeTokenVerifier(token) if token is not None else None,
        knowledge_tools=tools,
        managed_tools=managed_tools,
    )

    @server.custom_route("/admin", methods=["GET"], include_in_schema=False)
    async def admin_page(_request: Request) -> Response:
        index_path = _ADMIN_DIST / "index.html"
        if not index_path.is_file():
            return HTMLResponse(
                "<h1>RAG Control Plane UI is not built</h1><p>Run npm run dashboard:build.</p>",
                status_code=503,
            )
        return FileResponse(
            index_path,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
                ),
            },
        )

    @server.custom_route(
        "/admin/assets/{asset_path:path}",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_asset(request: Request) -> Response:
        assets = (_ADMIN_DIST / "assets").resolve()
        candidate = (assets / request.path_params["asset_path"]).resolve()
        if not candidate.is_relative_to(assets) or not candidate.is_file():
            return JSONResponse({"error": "asset not found"}, status_code=404)
        return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000"})

    @server.custom_route("/admin/api/overview", methods=["GET"], include_in_schema=False)
    async def admin_overview(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await asyncio.to_thread(admin.overview)
            payload["managed_tools"] = managed_tools.payload()
            payload["catalog"] = catalog.payload()
            payload["graph"] = await asyncio.to_thread(catalog.graph_overview)
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

    @server.custom_route("/admin/api/catalog", methods=["GET"], include_in_schema=False)
    async def admin_catalog(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        return JSONResponse(catalog.payload())

    @server.custom_route("/admin/api/indexes", methods=["POST"], include_in_schema=False)
    async def admin_create_index(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            name = payload.get("name")
            description = payload.get("description", "")
            if not isinstance(name, str) or not isinstance(description, str):
                raise ValueError("name and description must be strings")
            index = await asyncio.to_thread(
                catalog.create_index,
                name=name,
                description=description,
            )
            return JSONResponse(index.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/indexes/build",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_build_index(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            index_id = payload.get("index_id") if isinstance(payload, dict) else None
            if not isinstance(index_id, str) or not index_id:
                raise ValueError("index_id must be a non-empty string")
            job = catalog.start_index_build(index_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/repositories",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_add_repository(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            name = payload.get("name")
            git_url = payload.get("git_url")
            index_id = payload.get("index_id")
            index_name = payload.get("index_name")
            ref = payload.get("ref")
            if not isinstance(name, str) or not isinstance(git_url, str):
                raise ValueError("name and git_url must be strings")
            if index_id is not None and not isinstance(index_id, str):
                raise ValueError("index_id must be a string or null")
            if index_name is not None and not isinstance(index_name, str):
                raise ValueError("index_name must be a string or null")
            if ref is not None and not isinstance(ref, str):
                raise ValueError("ref must be a string or null")
            job = catalog.start_repository_ingestion(
                name=name,
                git_url=git_url,
                index_id=index_id,
                index_name=index_name,
                ref=ref,
            )
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/graph/rebuild",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_build_graph(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        job = catalog.start_graph_build()
        return JSONResponse(job.model_dump(mode="json"), status_code=202)

    @server.custom_route("/admin/api/graph/overview", methods=["GET"], include_in_schema=False)
    async def admin_graph_overview(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            return JSONResponse(await asyncio.to_thread(catalog.graph_overview))
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/graph", methods=["GET"], include_in_schema=False)
    async def admin_graph(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await asyncio.to_thread(
                catalog.graph,
                view=_optional_query(request, "view", "services") or "services",
                service=_optional_query(request, "service"),
                depth=_integer_query(request, "depth", 1, minimum=0, maximum=10),
                limit=_integer_query(request, "limit", 3000, minimum=1, maximum=20_000),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/graph/evidence", methods=["GET"], include_in_schema=False)
    async def admin_graph_evidence(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            raw = request.query_params.get("ids", "")
            ids = [item.strip() for item in raw.split(",") if item.strip()]
            return JSONResponse(await asyncio.to_thread(catalog.graph_evidence, ids))
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
            managed_tools.upsert(definition)
            if definition.name in existing:
                server.remove_tool(definition.name)
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
            server.remove_tool(name)
            return JSONResponse({"status": "deleted", "name": name})
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/tools/test", methods=["POST"], include_in_schema=False)
    async def admin_test_tool(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            name = payload.get("name")
            query = payload.get("query")
            if not isinstance(name, str) or not isinstance(query, str):
                raise ValueError("name and query must be strings")
            result = await asyncio.to_thread(managed_tools.execute, name, {"query": query})
            return JSONResponse(result)
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

    @server.custom_route("/api/v1/ssot/context", methods=["GET"], include_in_schema=False)
    async def api_ssot_context(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            question = request.query_params.get("question", "")
            if not question.strip():
                raise ValueError("question must not be empty")
            mode = _optional_query(request, "mode", "implementation") or "implementation"
            payload = await asyncio.to_thread(
                tools.ssot_context,
                question=question,
                mode=mode,
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
    """Build the FastMCP ASGI app for tests or external ASGI servers."""
    server = create_http_server(service, settings)
    return server.http_app(
        path=settings.mcp_http_path,
        host_origin_protection=False,
    )


def main() -> None:
    """Preload the index, then serve FastMCP Streamable HTTP."""
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
        "Starting FastMCP HTTP server on %s:%d%s (authentication=%s)",
        settings.mcp_http_host,
        settings.mcp_http_port,
        settings.mcp_http_path,
        "enabled" if settings.mcp_http_bearer_token else "disabled",
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
