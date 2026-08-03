"""Authenticated FastMCP Streamable HTTP entry point for remote clients."""

from __future__ import annotations

import asyncio
import logging
import secrets

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from corporate_kb.config import Settings
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService, configure_logging, create_service

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
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    logger.exception("Remote knowledge API request failed", exc_info=exc)
    return JSONResponse({"error": "internal server error"}, status_code=500)


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
    tools = KnowledgeTools(service)
    server = create_mcp_server(service, auth=ConstantTimeTokenVerifier(token))

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
                top_k=_integer_query(request, "top_k", 5, minimum=1, maximum=20),
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
            payload = await asyncio.to_thread(tools.get_document, document_id)
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
    stats = service.load_or_build_index()
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
