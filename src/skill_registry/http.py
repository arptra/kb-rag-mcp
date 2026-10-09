"""Authenticated dashboard API for the separate skills registry."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from corporate_kb.dev_debug.capture import emit_failure
from skill_registry.delivery import (
    MAX_SELECTION_TOKEN,
    bootstrap_prompt,
    build_extension_zip,
    client_skill,
    decode_selection,
    encode_selection,
    prepare_install,
)

logger = logging.getLogger(__name__)
Authorize = Callable[[Request], Awaitable[bool]]
PREFIX = "/admin/api/skills"
MAX_BODY_BYTES = 64 * 1024


class RequestError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _error(exc: Exception) -> JSONResponse:
    # Git and filesystem errors can contain credential-bearing URLs or local paths.
    if isinstance(exc, RequestError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    if isinstance(exc, KeyError):
        return JSONResponse(
            {"error": "Requested skill registry item was not found"}, status_code=404
        )
    if isinstance(exc, (ValueError, TypeError)):
        return JSONResponse(
            {"error": "Invalid skills request or package; check the supplied fields"},
            status_code=400,
        )
    if isinstance(exc, PermissionError):
        return JSONResponse({"error": "Skills operation is not permitted"}, status_code=403)
    if isinstance(exc, RuntimeError):
        return JSONResponse({"error": "Skills operation is currently unavailable"}, status_code=409)
    logger.warning("Skills API operation failed (%s)", type(exc).__name__)
    return JSONResponse({"error": "Skill registry operation failed"}, status_code=500)


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        raise ValueError("Invalid origin")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


async def _body(request: Request) -> dict[str, Any]:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise RequestError("Cross-site requests are not permitted", 403)
    origin = request.headers.get("origin")
    if origin:
        try:
            same_origin = _origin(origin) == _origin(str(request.url))
        except ValueError:
            same_origin = False
        if not same_origin:
            raise RequestError("Cross-origin requests are not permitted", 403)
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise RequestError("Content-Type must be application/json", 415)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BODY_BYTES:
            raise RequestError("Request body is too large", 413)
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise RequestError("Request body must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise RequestError("Request body must be a JSON object")
    return parsed


def _required(data: Any, field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise RequestError(f"{field} must be a nonempty string")
    return value


def _integer(request: Request, field: str, default: int, maximum: int) -> int:
    try:
        value = int(request.query_params.get(field, str(default)))
    except ValueError as exc:
        raise RequestError(f"{field} must be an integer") from exc
    if value < 0 or value > maximum or (field == "limit" and value == 0):
        raise RequestError(f"{field} is out of range")
    return value


def _absolute_url(request: Request, path: str) -> str:
    # Deliberately do not read arbitrary forwarded headers or propagate query tokens.
    _origin(str(request.url))
    root = request.scope.get("root_path", "").rstrip("/")
    return str(request.url.replace(path=root + path, query="", fragment=""))


def register_skills_routes(
    server: FastMCP,
    registry: Any,
    *,
    reader_authorized: Authorize,
    manager_authorized: Authorize,
    mcp_path: str = "/skills/mcp",
) -> None:
    """Register dashboard routes; the caller owns the separate MCP mount and worker lifespan."""

    def route(path: str, *, method: str = "GET", manager: bool = False) -> Callable[..., Any]:
        def decorate(handler: Callable[..., Awaitable[Response]]) -> Callable[..., Any]:
            async def authorized(request: Request) -> Response:
                if not await reader_authorized(request):
                    return JSONResponse(
                        {"error": "unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                if manager and not await manager_authorized(request):
                    return JSONResponse(
                        {"error": "Skills manager access is required"}, status_code=403
                    )
                try:
                    response = await handler(request)
                    response.headers["Cache-Control"] = "no-store"
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    return response
                except Exception as exc:
                    emit_failure(
                        "skills", "http", exc, path=request.url.path, method=request.method
                    )
                    return _error(exc)

            return server.custom_route(
                PREFIX + path, methods=[method], name="skills_" + handler.__name__
            )(authorized)

        return decorate

    @route("/status")
    async def status(request: Request) -> Response:
        sources, skills, jobs = await asyncio.gather(
            asyncio.to_thread(registry.list_sources),
            asyncio.to_thread(registry.list_skills, limit=1),
            asyncio.to_thread(registry.list_jobs, limit=10),
        )
        return JSONResponse(
            {
                "enabled": True,
                "can_manage": await manager_authorized(request),
                "source_count": len(sources),
                "skill_count": skills["total"],
                "jobs": jobs,
                "mcp_url": _absolute_url(request, mcp_path),
            }
        )

    @route("/sources")
    async def sources(_request: Request) -> Response:
        return JSONResponse({"sources": await asyncio.to_thread(registry.list_sources)})

    @route("/sources", method="POST", manager=True)
    async def save_source(request: Request) -> Response:
        body = await _body(request)
        return JSONResponse({"source": await asyncio.to_thread(registry.save_source, body)})

    @route("/sources/validate", method="POST", manager=True)
    async def validate_source(request: Request) -> Response:
        body = await _body(request)
        return JSONResponse(await asyncio.to_thread(registry.validate_source, body))

    @route("/sources/delete", method="POST", manager=True)
    async def delete_source(request: Request) -> Response:
        body = await _body(request)
        result = await asyncio.to_thread(registry.delete_source, _required(body, "source_id"))
        return JSONResponse({"deleted": True, "result": result})

    @route("/sync", method="POST", manager=True)
    async def sync(request: Request) -> Response:
        body = await _body(request)
        source_id = body.get("source_id")
        if source_id is not None:
            jobs = [await asyncio.to_thread(registry.queue_sync, _required(body, "source_id"))]
        else:
            sources = await asyncio.to_thread(registry.list_sources)
            jobs = []
            for source in sources:
                if source.get("enabled", True) and not source.get("archived", False):
                    jobs.append(await asyncio.to_thread(registry.queue_sync, source["id"]))
        return JSONResponse({"jobs": jobs}, status_code=202)

    @route("/jobs")
    async def jobs(request: Request) -> Response:
        job_id = request.query_params.get("job_id")
        if job_id:
            return JSONResponse({"job": await asyncio.to_thread(registry.get_job, job_id)})
        return JSONResponse(
            {
                "jobs": await asyncio.to_thread(
                    registry.list_jobs, limit=_integer(request, "limit", 50, 200)
                )
            }
        )

    @route("/registry")
    async def listing(request: Request) -> Response:
        query = request.query_params.get("query", "")
        if len(query) > 500:
            raise RequestError("query is too long")
        return JSONResponse(
            await asyncio.to_thread(
                registry.list_skills,
                query=query,
                source_id=request.query_params.get("source_id"),
                limit=_integer(request, "limit", 50, 100),
                offset=_integer(request, "offset", 0, 1000000),
            )
        )

    @route("/detail")
    async def detail(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(
                registry.skill_detail, _required(request.query_params, "skill_id")
            )
        )

    @route("/release")
    async def release(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(
                registry.get_release,
                _required(request.query_params, "skill_id"),
                request.query_params.get("revision"),
            )
        )

    @route("/file")
    async def file(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(
                registry.read_file,
                _required(request.query_params, "skill_id"),
                _required(request.query_params, "revision"),
                _required(request.query_params, "path"),
            )
        )

    @route("/diff")
    async def diff(request: Request) -> Response:
        return JSONResponse(
            await asyncio.to_thread(
                registry.diff,
                _required(request.query_params, "skill_id"),
                _required(request.query_params, "from_revision"),
                _required(request.query_params, "to_revision"),
            )
        )

    @route("/publish", method="POST", manager=True)
    async def publish(request: Request) -> Response:
        body = await _body(request)
        return JSONResponse(
            {
                "skill": await asyncio.to_thread(
                    registry.publish, _required(body, "skill_id"), _required(body, "revision")
                )
            }
        )

    @route("/connect")
    async def connect(request: Request) -> Response:
        return JSONResponse(
            {
                "mcp_url": _absolute_url(request, mcp_path),
                "mcp_config": {
                    "mcpServers": {
                        "corporate-skills": {
                            "httpUrl": _absolute_url(request, mcp_path),
                            "timeout": 30000,
                        }
                    }
                },
                "credentials_included": False,
                "authentication": "Reuse the existing approved MCP credentials and certificate "
                "configuration for this endpoint. No credentials are included here.",
                "client_skill_url": _absolute_url(request, PREFIX + "/client-skill"),
                "bootstrap_prompt": bootstrap_prompt(),
                "prompt_name": "corporate_skills_sync",
            "native_archive_install_verified": False,
            "extension_auto_update": "unverified",
            "automatic_client_updates": False,
                "restart_required": True,
            }
        )

    @route("/prepare-install", method="POST")
    async def prepare(request: Request) -> Response:
        body = await _body(request)
        if not isinstance(body.get("skills"), list):
            raise RequestError("skills must be a list of skill_id/revision selections")
        result = await asyncio.to_thread(
            prepare_install, registry, body["skills"], body.get("scope", "user")
        )
        token = encode_selection(result["manifest"])
        result["download_url"] = _absolute_url(request, PREFIX + "/bundle") + "?selection=" + token
        return JSONResponse(result)

    @route("/client-skill")
    async def download_client_skill(_request: Request) -> Response:
        return Response(
            client_skill(),
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="SKILL.md"'},
        )

    @route("/bundle")
    async def bundle(request: Request) -> Response:
        token = request.query_params.get("selection", "")
        if len(token) > MAX_SELECTION_TOKEN:
            raise RequestError("Installation selection is too large", 413)
        selected, scope = decode_selection(token)
        prepared = await asyncio.to_thread(prepare_install, registry, selected, scope)
        content = await asyncio.to_thread(build_extension_zip, registry, prepared["manifest"])
        digest = prepared["manifest"]["bundle_revision"][:16]
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="corporate-skills-{digest}.zip"'
            },
        )
