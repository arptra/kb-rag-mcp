"""Dashboard management and authenticated image evidence endpoints."""

from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from corporate_kb.a2a.http import body
from corporate_kb.confluence.registry import ConfluenceRegistry
from corporate_kb.gigacode_runner import GigaCodeRunner


def register_confluence_routes(
    server: FastMCP,
    registry: ConfluenceRegistry,
    *,
    reader_authorized: Callable[[Request], Awaitable[bool]],
    manager_authorized: Callable[[Request], Awaitable[bool]],
    indexes: Callable[[], list[dict[str, Any]]],
    gigacode: GigaCodeRunner,
) -> None:
    async def allowed(request: Request, *, manage: bool = False) -> bool:
        return await manager_authorized(request) or (
            not manage and await reader_authorized(request)
        )

    def route(
        path: str, methods: list[str], *, manage: bool = False
    ) -> Callable[[Callable[[Request], Awaitable[Any]]], Callable[[Request], Awaitable[Response]]]:
        def decorator(
            handler: Callable[[Request], Awaitable[Any]],
        ) -> Callable[[Request], Awaitable[Response]]:
            async def guarded(request: Request) -> Response:
                if not await allowed(request, manage=manage):
                    return JSONResponse(
                        {
                            "error": "Administrator authentication required"
                            if manage
                            else "unauthorized"
                        },
                        status_code=401,
                    )
                try:
                    result = await handler(request)
                    if isinstance(result, Response):
                        return result
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
                except KeyError as exc:
                    return JSONResponse({"error": str(exc.args[0])}, status_code=404)
                except PermissionError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=401)
                except (ValueError, TypeError) as exc:
                    return JSONResponse({"error": str(exc)}, status_code=400)
                except RuntimeError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=409)

            guarded.__name__ = "confluence_" + handler.__name__
            server.custom_route(
                "/admin/api/confluence" + path, methods=methods, include_in_schema=False
            )(guarded)
            return guarded

        return decorator

    @route("", ["GET"])
    async def overview(request: Request) -> dict[str, Any]:
        return {
            "sources": await asyncio.to_thread(registry.list_sources),
            "jobs": await asyncio.to_thread(registry.jobs),
            "can_manage": await manager_authorized(request),
            "indexes": indexes(),
            "gigacode": await asyncio.to_thread(gigacode.status),
        }

    @route("/sources", ["GET"])
    async def sources(request: Request) -> dict[str, Any]:
        return {
            "sources": await asyncio.to_thread(registry.list_sources),
            "can_manage": await manager_authorized(request),
        }

    @route("/sources", ["POST"], manage=True)
    async def create(request: Request) -> dict[str, Any]:
        data = await body(request, 65536)
        if not await manager_authorized(request):
            raise PermissionError("Administrator session expired")
        token = data.pop("token", None)
        if token is not None and not isinstance(token, str):
            raise ValueError("token must be a string")
        source = await asyncio.to_thread(registry.upsert_source, data, token)
        job = await asyncio.to_thread(registry.queue_sync, source["id"])
        return {"source": source, "job": job}

    @route("/test", ["POST"], manage=True)
    async def test(request: Request) -> dict[str, Any]:
        data = await body(request, 65536)
        if not await manager_authorized(request):
            raise PermissionError("Administrator session expired")
        token, source_id = data.pop("token", None), data.pop("source_id", None)
        if token is not None and not isinstance(token, str):
            raise ValueError("token must be a string")
        if source_id is not None and not isinstance(source_id, str):
            raise ValueError("source_id must be a string")
        return await asyncio.to_thread(
            registry.test_connection, data, token=token, source_id=source_id
        )

    @route("/sources/{source_id}", ["PATCH"], manage=True)
    async def update(request: Request) -> dict[str, Any]:
        data = await body(request, 65536)
        if not await manager_authorized(request):
            raise PermissionError("Administrator session expired")
        token = data.pop("token", None)
        if token is not None and not isinstance(token, str):
            raise ValueError("token must be a string")
        return {
            "source": await asyncio.to_thread(
                registry.upsert_source, data, token, request.path_params["source_id"]
            )
        }

    @route("/sources/{source_id}/sync", ["POST"], manage=True)
    async def sync(request: Request) -> dict[str, Any]:
        return {
            "job": await asyncio.to_thread(registry.queue_sync, request.path_params["source_id"])
        }

    @route("/sources/{source_id}/pause", ["POST"], manage=True)
    async def pause(request: Request) -> dict[str, Any]:
        data = await body(request, 8192)
        if not await manager_authorized(request):
            raise PermissionError("Administrator session expired")
        if not isinstance(data.get("paused"), bool):
            raise ValueError("paused must be a boolean")
        return {
            "source": await asyncio.to_thread(
                registry.set_paused, request.path_params["source_id"], data["paused"]
            )
        }

    @route("/sources/{source_id}/delete", ["POST"], manage=True)
    async def delete(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(registry.delete_source, request.path_params["source_id"])

    @route("/sources/{source_id}/pages", ["GET"])
    async def pages(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(
            registry.browse_pages,
            request.path_params["source_id"],
            offset=int(request.query_params.get("offset", "0")),
            limit=int(request.query_params.get("limit", "100")),
            query=request.query_params.get("query", ""),
        )

    @route("/jobs", ["GET"])
    async def jobs(request: Request) -> dict[str, Any]:
        return {
            "jobs": await asyncio.to_thread(
                registry.jobs, source_id=request.query_params.get("source_id")
            )
        }

    @route("/jobs/{job_id}", ["GET"])
    async def job(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(registry.job_status, request.path_params["job_id"])

    @route("/jobs/{job_id}/log", ["GET"])
    async def log(request: Request) -> dict[str, Any]:
        return await asyncio.to_thread(
            registry.job_log,
            request.path_params["job_id"],
            int(request.query_params.get("cursor", "0")),
            int(request.query_params.get("limit", "200")),
        )

    @route("/jobs/{job_id}/cancel", ["POST"], manage=True)
    async def cancel(request: Request) -> dict[str, Any]:
        return {"job": await asyncio.to_thread(registry.cancel_job, request.path_params["job_id"])}

    @route("/assets/{asset_id}", ["GET"])
    async def asset(request: Request) -> Response:
        version = request.query_params.get("version")
        path = await asyncio.to_thread(
            registry.asset_path,
            request.path_params["asset_id"],
            source_id=request.query_params.get("source_id", ""),
            version=int(version) if version else None,
        )
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Uploaded SVG is source data; never let it execute in our UI origin.
        return FileResponse(
            path,
            media_type=media,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )
