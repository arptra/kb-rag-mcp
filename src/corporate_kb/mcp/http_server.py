"""Authenticated FastMCP Streamable HTTP entry point for remote clients."""

from __future__ import annotations

import logging
import secrets

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from corporate_kb.config import Settings
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.service import KnowledgeService, configure_logging, create_service

logger = logging.getLogger(__name__)
_READ_SCOPE = "kb:read"


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
    server = create_mcp_server(service, auth=ConstantTimeTokenVerifier(token))

    @server.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health_check(_request: Request) -> JSONResponse:
        stats = service.stats()
        return JSONResponse(
            {
                "status": "ok",
                "documents": stats.document_count,
                "chunks": stats.chunk_count,
                "embedding_provider": stats.embedding_provider,
            }
        )

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
