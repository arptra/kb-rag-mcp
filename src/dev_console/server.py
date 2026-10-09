"""Loopback-only HTTP surface; native GigaCode remains on the owner's terminal."""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp

from corporate_kb.dev_debug.interactive import probe
from corporate_kb.dev_debug.recording import redact_text
from dev_console.controller import DevConsoleController

_STATIC = Path(__file__).parent / "static"
_MAX_BODY = 32 * 1024
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
}


class LocalAccess(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        super().__init__(app)
        self.token = token
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        mutation = request.method not in {"GET", "HEAD", "OPTIONS"}
        if (
            host not in self.hosts
            or (origin and origin != f"http://{host}")
            or (mutation and not origin)
        ):
            response: Response = JSONResponse({"error": "Local origin required"}, status_code=403)
        elif request.url.path.startswith("/api/") and not secrets.compare_digest(
            request.headers.get("authorization", "").encode(), f"Bearer {self.token}".encode()
        ):
            response = JSONResponse(
                {"error": "Open the current URL from the launch terminal"}, status_code=401
            )
        else:
            response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response


async def body(request: Request, allowed: set[str]) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise ValueError("Expected application/json")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > _MAX_BODY:
            raise ValueError("Request is too large")
    value = json.loads(raw or b"{}")
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Invalid request fields")
    return value


def string(data: dict[str, Any], key: str, *, limit: int = 8000, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"Invalid field: {key}")
    return value


def log_paths(data: dict[str, Any]) -> list[str]:
    paths = data.get("log_paths", [])
    if (
        not isinstance(paths, list)
        or len(paths) > 20
        or any(
            not isinstance(path, str) or not path or len(path) > 1000 or "\0" in path
            for path in paths
        )
    ):
        raise ValueError("Invalid log paths")
    return paths


def create_app(controller: DevConsoleController, *, token: str, port: int = 8788) -> Starlette:
    if len(token) < 32 or not 1 <= port <= 65535:
        raise ValueError("A private token and valid loopback port are required")

    def endpoint(
        function: Callable[[Request], Awaitable[Any]],
    ) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            try:
                result = await function(request)
                return result if isinstance(result, Response) else JSONResponse(result)
            except FileNotFoundError:
                return JSONResponse({"error": "Session or artifact not found"}, status_code=404)
            except (ValueError, KeyError) as exc:
                return JSONResponse({"error": redact_text(str(exc))}, status_code=400)
            except RuntimeError as exc:
                return JSONResponse({"error": redact_text(str(exc))}, status_code=409)
            except OSError:
                return JSONResponse(
                    {"error": "Cannot access development artifacts"}, status_code=500
                )

        return wrapped

    async def state(_request: Request) -> Any:
        return await run_in_threadpool(controller.overview)

    async def doctor(_request: Request) -> Any:
        return await run_in_threadpool(probe, controller.command)

    async def sessions(request: Request) -> Any:
        data = await body(request, {"label", "log_paths"})
        return await run_in_threadpool(
            controller.start_recording,
            label=string(data, "label", limit=500),
            log_paths=log_paths(data),
        )

    async def detail(request: Request) -> Any:
        return await run_in_threadpool(controller.session_detail, request.path_params["session_id"])

    async def action(request: Request) -> Any:
        session_id = request.path_params["session_id"]
        operation = request.path_params["action"]
        fields = {
            "recording/start": {"log_paths"},
            "recording/stop": set(),
            "note": {"text"},
            "bundle": set(),
            "fix": {"goal", "max_rounds"},
            "cancel": set(),
        }
        if operation not in fields:
            return JSONResponse({"error": "Unknown action"}, status_code=404)
        data = await body(request, fields[operation])
        if operation == "recording/start":
            return await run_in_threadpool(
                controller.start_recording,
                session_id,
                log_paths=log_paths(data) if "log_paths" in data else None,
            )
        if operation == "recording/stop":
            return await run_in_threadpool(controller.stop_recording, session_id)
        if operation == "note":
            return await run_in_threadpool(controller.add_note, session_id, string(data, "text"))
        if operation == "bundle":
            return await run_in_threadpool(controller.prepare_bundle, session_id)
        if operation == "cancel":
            return await run_in_threadpool(controller.cancel_repair, session_id)
        rounds = data.get("max_rounds", 3)
        if type(rounds) is not int or not 1 <= rounds <= 10:
            raise ValueError("max_rounds must be an integer between 1 and 10")
        return await run_in_threadpool(
            controller.queue_repair, session_id, goal=string(data, "goal"), max_rounds=rounds
        )

    async def approval(request: Request) -> Any:
        data = await body(request, {"session_id", "approved"})
        if type(data.get("approved")) is not bool:
            raise ValueError("approved must be a boolean")
        return await run_in_threadpool(
            controller.approval,
            string(data, "session_id", limit=100),
            request.path_params["request_id"],
            data["approved"],
        )

    async def artifact(request: Request) -> Any:
        return await run_in_threadpool(
            controller.read_artifact,
            request.path_params["session_id"],
            request.path_params["artifact_path"],
        )

    async def static(request: Request) -> Response:
        name = request.path_params.get("filename", "index.html")
        if name not in {"index.html", "app.js", "style.css"}:
            return Response(status_code=404)
        return FileResponse(_STATIC / name)

    return Starlette(
        routes=[
            Route("/", static),
            Route("/api/state", endpoint(state)),
            Route("/api/doctor", endpoint(doctor)),
            Route("/api/sessions", endpoint(sessions), methods=["POST"]),
            Route("/api/sessions/{session_id}", endpoint(detail)),
            Route("/api/sessions/{session_id}/artifacts/{artifact_path:path}", endpoint(artifact)),
            Route("/api/sessions/{session_id}/{action:path}", endpoint(action), methods=["POST"]),
            Route("/api/approvals/{request_id}", endpoint(approval), methods=["POST"]),
            Route("/{filename}", static),
        ],
        middleware=[Middleware(LocalAccess, token=token, port=port)],
    )
