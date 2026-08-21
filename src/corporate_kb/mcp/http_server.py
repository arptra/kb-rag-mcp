"""Authenticated FastMCP Streamable HTTP entry point for remote clients."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp

from corporate_kb.admin import AdminController
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.feature_context import FeatureContextPlanner
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.servers import (
    McpServerDefinition,
    McpServerRegistry,
    mcp_servers_payload,
)
from corporate_kb.mcp.tool_overrides import (
    BuiltinToolOverride,
    BuiltinToolOverrideRegistry,
)
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


def _body_integer(
    body: dict[str, object],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = body.get(name, default)
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    elif isinstance(raw, str):
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
    else:
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_body_string(body: dict[str, object], name: str) -> str | None:
    raw = body.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a string or null")
    value = raw.strip()
    return value or None


def _optional_body_string_list(body: dict[str, object], name: str) -> list[str] | None:
    raw = body.get(name)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{name} must be an array of strings or null")
    return [item.strip() for item in raw if item.strip()]


def _body_boolean(body: dict[str, object], name: str, default: bool) -> bool:
    raw = body.get(name, default)
    if not isinstance(raw, bool):
        raise ValueError(f"{name} must be a boolean")
    return raw


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
    feature_context = FeatureContextPlanner(catalog)
    managed_tools = ManagedToolRegistry(
        settings.managed_tools_path,
        tools,
        index_tools=catalog.tools_for,
        index_exists=catalog.has_index,
    )
    builtin_tool_overrides = BuiltinToolOverrideRegistry(
        settings.builtin_tool_overrides_path
    )
    mcp_servers = McpServerRegistry(settings.mcp_servers_path)
    admin = AdminController(service, usage)
    server = create_mcp_server(
        service,
        auth=ConstantTimeTokenVerifier(token) if token is not None else None,
        knowledge_tools=tools,
        managed_tools=managed_tools,
        feature_context=feature_context,
        catalog=catalog,
        builtin_tool_overrides=builtin_tool_overrides,
    )

    async def current_tool_catalog() -> dict[str, object]:
        managed = {item.name: item for item in managed_tools.list()}
        overridden = {item.name for item in builtin_tool_overrides.list()}
        runtime_tools = await server.list_tools(run_middleware=False)
        items: list[dict[str, object]] = []
        for tool in sorted(runtime_tools, key=lambda item: item.name):
            definition = managed.get(tool.name)
            kind = "managed" if definition is not None else "built-in"
            item: dict[str, object] = {
                "name": tool.name,
                "title": tool.title,
                "description": tool.description or "",
                "kind": kind,
                "input_schema": tool.parameters,
                "output_schema": tool.output_schema,
                "description_overridden": tool.name in overridden,
                "editable": True,
            }
            if definition is not None:
                item["index_ids"] = definition.index_ids
                item["defaults"] = definition.defaults.model_dump(mode="json")
            else:
                item["index_ids"] = []
                item["defaults"] = {}
            items.append(item)
        return {
            "tool_count": len(items),
            "built_in_count": sum(item["kind"] == "built-in" for item in items),
            "managed_count": sum(item["kind"] == "managed" for item in items),
            "tools": items,
        }

    def current_mcp_servers(request: Request) -> dict[str, object]:
        local_url = f"{str(request.base_url).rstrip('/')}{settings.mcp_http_path}"
        definitions = [item.model_dump(mode="json") for item in managed_tools.list()]
        return mcp_servers_payload(
            mcp_servers,
            local_url=local_url,
            managed_tools=definitions,
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
            payload["tool_catalog"] = await current_tool_catalog()
            payload["mcp_servers"] = current_mcp_servers(request)
            payload["catalog"] = catalog.payload()
            payload["graph"] = await asyncio.to_thread(catalog.graph_overview)
            payload["service_map"] = await asyncio.to_thread(catalog.service_map_overview)
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/mcp-servers", methods=["GET"], include_in_schema=False)
    async def admin_mcp_servers(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        return JSONResponse(current_mcp_servers(request))

    @server.custom_route("/admin/api/mcp-servers", methods=["POST"], include_in_schema=False)
    async def admin_add_mcp_server(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            name = payload.get("name")
            url = payload.get("url")
            if not isinstance(name, str) or not isinstance(url, str):
                raise ValueError("name and url must be strings")
            definition = mcp_servers.add(McpServerDefinition(name=name, url=url))
            checked = await mcp_servers.probe(definition.id)
            return JSONResponse(checked.payload(), status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/mcp-servers/check",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_check_mcp_server(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            server_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(server_id, str) or not server_id:
                raise ValueError("id must be a non-empty string")
            if server_id == "local":
                local = current_mcp_servers(request)["servers"][0]  # type: ignore[index]
                return JSONResponse(local)
            checked = await mcp_servers.probe(server_id)
            return JSONResponse(checked.payload())
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/mcp-servers/delete",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_delete_mcp_server(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            server_id = payload.get("id") if isinstance(payload, dict) else None
            if not isinstance(server_id, str) or not server_id:
                raise ValueError("id must be a non-empty string")
            if server_id == "local":
                raise ValueError("The local MCP server cannot be deleted")
            mcp_servers.delete(server_id)
            return JSONResponse({"status": "deleted", "id": server_id})
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
        "/admin/api/indexes/documents",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_index_documents(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            index_id = _optional_query(request, "index_id")
            if not index_id:
                raise ValueError("index_id query parameter is required")
            payload = await asyncio.to_thread(
                catalog.index_documents,
                index_id,
                query=_optional_query(request, "query", "") or "",
                offset=_integer_query(
                    request,
                    "offset",
                    0,
                    minimum=0,
                    maximum=10_000_000,
                ),
                limit=_integer_query(request, "limit", 50, minimum=1, maximum=200),
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/indexes/document",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_index_document(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            index_id = _optional_query(request, "index_id")
            document_id = _optional_query(request, "document_id")
            if not index_id or not document_id:
                raise ValueError("index_id and document_id query parameters are required")
            payload = await asyncio.to_thread(
                catalog.index_document,
                index_id,
                document_id,
            )
            return JSONResponse(payload)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/indexes/documents",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_upload_index_documents(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            index_id = payload.get("index_id")
            raw_documents = payload.get("documents")
            overwrite = payload.get("overwrite", False)
            if not isinstance(index_id, str) or not index_id:
                raise ValueError("index_id must be a non-empty string")
            if not isinstance(raw_documents, list):
                raise ValueError("documents must be an array")
            if not isinstance(overwrite, bool):
                raise ValueError("overwrite must be a boolean")
            documents: list[dict[str, str]] = []
            for raw in raw_documents:
                if not isinstance(raw, dict):
                    raise ValueError("Each document must be an object")
                path = raw.get("path")
                content = raw.get("content")
                if not isinstance(path, str) or not isinstance(content, str):
                    raise ValueError("Each document must contain string path and content fields")
                documents.append({"path": path, "content": content})
            result = await asyncio.to_thread(
                catalog.upload_documents,
                index_id,
                documents=documents,
                overwrite=overwrite,
            )
            return JSONResponse(result, status_code=202)
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
        "/admin/api/repositories/refresh",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_refresh_repository(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            repository_id = payload.get("repository_id") if isinstance(payload, dict) else None
            if not isinstance(repository_id, str) or not repository_id:
                raise ValueError("repository_id must be a non-empty string")
            job = catalog.start_repository_refresh(repository_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/repositories/delete",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_delete_repository(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            repository_id = payload.get("repository_id") if isinstance(payload, dict) else None
            if not isinstance(repository_id, str) or not repository_id:
                raise ValueError("repository_id must be a non-empty string")
            job = catalog.start_repository_delete(repository_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/services/analyze",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_analyze_service(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            service_id = payload.get("service_id") if isinstance(payload, dict) else None
            if not isinstance(service_id, str) or not service_id:
                raise ValueError("service_id must be a non-empty string")
            job = catalog.start_service_analysis(service_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/services/delete",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_delete_service(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            service_id = payload.get("service_id") if isinstance(payload, dict) else None
            if not isinstance(service_id, str) or not service_id:
                raise ValueError("service_id must be a non-empty string")
            job = catalog.start_service_delete(service_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/jobs/cancel",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_cancel_job(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            job_id = payload.get("job_id") if isinstance(payload, dict) else None
            if not isinstance(job_id, str) or not job_id:
                raise ValueError("job_id must be a non-empty string")
            job = await asyncio.to_thread(catalog.cancel_job, job_id)
            return JSONResponse(job.model_dump(mode="json"), status_code=202)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/jobs/log", methods=["GET"], include_in_schema=False)
    async def admin_job_log(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            job_id = _optional_query(request, "job_id")
            if not job_id:
                raise ValueError("job_id query parameter is required")
            return JSONResponse(await asyncio.to_thread(catalog.job_log, job_id))
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/analysis/ssot-generate",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_generate_system_ssot(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            payload = await asyncio.to_thread(
                catalog.ssot_generation_request,
                action=_optional_body_string(body, "action") or "options",
                index_id=_optional_body_string(body, "index_id"),
                repository_ids=_optional_body_string_list(body, "repository_ids"),
                service_ids=_optional_body_string_list(body, "service_ids"),
                all_services=_body_boolean(body, "all_services", False),
                refresh_analysis=_body_boolean(body, "refresh_analysis", True),
                job_id=_optional_body_string(body, "job_id"),
                repository_name=_optional_body_string(body, "repository_name"),
                git_url=_optional_body_string(body, "git_url"),
                ref=_optional_body_string(body, "ref"),
                service_id=_optional_body_string(body, "service_id"),
                repository_id=_optional_body_string(body, "repository_id"),
                file_path=_optional_body_string(body, "file_path"),
                offset=_body_integer(body, "offset", 0, minimum=0, maximum=100_000_000),
                max_chars=_body_integer(body, "max_chars", 20_000, minimum=1_000, maximum=100_000),
                content=_optional_body_string(body, "content"),
                finalize=_body_boolean(body, "finalize", True),
            )
            status_code = 202 if payload["status"] == "queued" else 200
            return JSONResponse(payload, status_code=status_code)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/analysis/ssot-bundle",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_create_ssot_bundle(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            service_id = payload.get("service_id") if isinstance(payload, dict) else None
            if not isinstance(service_id, str) or not service_id:
                raise ValueError("service_id must be a non-empty string")
            bundle = await asyncio.to_thread(catalog.create_ssot_bundle, service_id)
            return JSONResponse(bundle, status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/analysis/bundles/download",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_download_ssot_bundle(request: Request) -> Response:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            bundle_id = _optional_query(request, "bundle_id")
            if not bundle_id:
                raise ValueError("bundle_id query parameter is required")
            path = await asyncio.to_thread(catalog.ssot_bundle_path, bundle_id)
            return FileResponse(
                path,
                filename=path.name,
                media_type="application/zip",
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/analysis/ssot-import",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_import_ssot(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Request body must be a JSON object")
            service_id = payload.get("service_id")
            index_id = payload.get("index_id")
            content = payload.get("content")
            if not isinstance(service_id, str) or not service_id:
                raise ValueError("service_id must be a non-empty string")
            if not isinstance(index_id, str) or not index_id:
                raise ValueError("index_id must be a non-empty string")
            if not isinstance(content, str) or not content:
                raise ValueError("service_id, index_id and content must be non-empty strings")
            result = await asyncio.to_thread(
                catalog.import_ssot,
                service_id=service_id,
                index_id=index_id,
                content=content,
            )
            return JSONResponse(result, status_code=202)
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

    @server.custom_route(
        "/admin/api/service-map/overview",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_service_map_overview(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            return JSONResponse(await asyncio.to_thread(catalog.service_map_overview))
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/admin/api/service-map", methods=["GET"], include_in_schema=False)
    async def admin_service_map(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            return JSONResponse(await asyncio.to_thread(catalog.service_map))
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
                server.local_provider.remove_tool(definition.name)
            server.add_tool(replacement)
            return JSONResponse(definition.model_dump(mode="json"), status_code=201)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/tools/catalog",
        methods=["GET"],
        include_in_schema=False,
    )
    async def admin_tool_catalog(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            return JSONResponse(await current_tool_catalog())
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route(
        "/admin/api/tools/builtin",
        methods=["POST"],
        include_in_schema=False,
    )
    async def admin_save_builtin_tool(request: Request) -> JSONResponse:
        if not _admin_authorized(request, settings):
            return _admin_denied(settings)
        try:
            payload = await request.json()
            definition = BuiltinToolOverride.model_validate(payload)
            runtime_tool = await server.get_tool(definition.name)
            if runtime_tool is None or definition.name in {
                item.name for item in managed_tools.list()
            }:
                raise KeyError(f"Unknown built-in MCP tool: {definition.name}")
            builtin_tool_overrides.upsert(definition)
            replacement = runtime_tool.model_copy(
                update={"description": definition.description},
                deep=False,
            )
            server.local_provider.remove_tool(definition.name)
            server.add_tool(replacement)
            return JSONResponse(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "status": "updated",
                }
            )
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
            server.local_provider.remove_tool(name)
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
            arguments = payload.get("arguments")
            if arguments is None and isinstance(payload.get("query"), str):
                arguments = {"query": payload["query"]}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("name must be a string and arguments must be an object")
            started_at = time.perf_counter()
            result = await server.call_tool(name, arguments, run_middleware=False)
            elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
            if not hasattr(result, "model_dump"):
                raise RuntimeError("Background MCP tasks are not supported by the playground")
            serialized = result.model_dump(mode="json")
            return JSONResponse(
                {
                    "tool": name,
                    "elapsed_ms": elapsed_ms,
                    **serialized,
                }
            )
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

    @server.custom_route("/api/v1/ssot/generate", methods=["POST"], include_in_schema=False)
    async def api_generate_system_ssot(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            payload = await asyncio.to_thread(
                catalog.ssot_generation_request,
                action=_optional_body_string(body, "action") or "options",
                index_id=_optional_body_string(body, "index_id"),
                repository_ids=_optional_body_string_list(body, "repository_ids"),
                service_ids=_optional_body_string_list(body, "service_ids"),
                all_services=_body_boolean(body, "all_services", False),
                refresh_analysis=_body_boolean(body, "refresh_analysis", True),
                job_id=_optional_body_string(body, "job_id"),
                repository_name=_optional_body_string(body, "repository_name"),
                git_url=_optional_body_string(body, "git_url"),
                ref=_optional_body_string(body, "ref"),
                service_id=_optional_body_string(body, "service_id"),
                repository_id=_optional_body_string(body, "repository_id"),
                file_path=_optional_body_string(body, "file_path"),
                offset=_body_integer(body, "offset", 0, minimum=0, maximum=100_000_000),
                max_chars=_body_integer(body, "max_chars", 20_000, minimum=1_000, maximum=100_000),
                content=_optional_body_string(body, "content"),
                finalize=_body_boolean(body, "finalize", True),
            )
            status_code = 202 if payload["status"] == "queued" else 200
            return JSONResponse(payload, status_code=status_code)
        except Exception as exc:
            return _api_error(exc)

    @server.custom_route("/api/v1/feature-context", methods=["POST"], include_in_schema=False)
    async def api_feature_context(request: Request) -> JSONResponse:
        if not _authorized(request, token):
            return _unauthorized_response()
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object")
            feature = body.get("feature")
            if not isinstance(feature, str) or not feature.strip():
                raise ValueError("feature must be a non-empty string")
            start_service = body.get("start_service")
            if start_service is not None and not isinstance(start_service, str):
                raise ValueError("start_service must be a string or null")
            payload = await asyncio.to_thread(
                feature_context.build,
                feature=feature,
                start_service=start_service,
                max_hops=_body_integer(body, "max_hops", 2, minimum=0, maximum=4),
                top_k_per_service=_body_integer(
                    body,
                    "top_k_per_service",
                    2,
                    minimum=1,
                    maximum=5,
                ),
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
