"""Small authenticated HTTP boundary for the detachable harness runtime."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from agent_harness.contracts import Principal, RunRequest
from agent_harness.runtime import HarnessRuntime, HarnessUnavailableError, QueueFullError

Authenticate = Callable[[Request], Awaitable[Principal | None]]
_MAX_BODY_BYTES = 65_536
_STATIC = Path(__file__).with_name("static")
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _json(payload: Any, *, status: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=status, headers=_HEADERS)


def _check_origin(request: Request) -> None:
    """Reject cross-origin mutations; a host still owns cookie/session CSRF rules."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    try:
        supplied = urlsplit(origin)
        actual = urlsplit(str(request.url))

        def authority(url: Any) -> tuple[str, str | None, int | None]:
            port = url.port or {"http": 80, "https": 443}.get(url.scheme)
            return url.scheme.lower(), url.hostname, port

        valid = (
            supplied.scheme in {"http", "https"}
            and not supplied.username
            and not supplied.password
            and not supplied.path
            and not supplied.query
            and not supplied.fragment
            and authority(supplied) == authority(actual)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(403, "Cross-origin requests are not allowed")


async def _body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            parsed = int(length)
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length") from exc
        if parsed < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if parsed > _MAX_BODY_BYTES:
            raise HTTPException(413, "Request body is too large")
    data = bytearray()
    async for part in request.stream():
        if len(data) + len(part) > _MAX_BODY_BYTES:
            raise HTTPException(413, "Request body is too large")
        data.extend(part)
    return bytes(data)


async def _run_request(request: Request) -> RunRequest:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(415, "Content-Type must be application/json")
    try:
        payload = json.loads(await _body(request))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(400, "Request must contain a valid JSON object") from exc
    try:
        return RunRequest.model_validate(payload)
    except ValidationError as exc:
        # Do not echo questions, tool inputs or other potentially sensitive values.
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})
        detail = "Invalid request" + (": " + ", ".join(fields) if fields else "")
        raise HTTPException(400, detail) from exc


def create_app(
    runtime: HarnessRuntime,
    authenticate: Authenticate,
    *,
    manage_lifespan: bool = True,
) -> Starlette:
    """Create an app usable alone or mounted under a host-owned authenticated path.

    Every data endpoint invokes the host's authentication callback. Public static
    files contain no runtime state, credentials or source contents. A mounted host
    enters this app's lifespan explicitly, or uses ``manage_lifespan=False`` when
    it starts/stops the runtime itself.
    """

    async def principal(request: Request, *, mutate: bool = False) -> Principal:
        identity = await authenticate(request)
        if not isinstance(identity, Principal) or not identity.owner_id:
            raise HTTPException(401, "Authentication required")
        if mutate:
            _check_origin(request)
        return identity

    async def status(request: Request) -> Response:
        identity = await principal(request)
        return _json(
            {
                **await runtime.status(identity),
                "identity_scope": "shared" if identity.shared else "personal",
            }
        )

    async def runs(request: Request) -> Response:
        identity = await principal(request, mutate=request.method == "POST")
        if request.method == "GET":
            return _json({"runs": await runtime.list_runs(identity)})
        run = await runtime.submit(identity, await _run_request(request))
        return _json(run, status=202)

    async def get_run(request: Request) -> Response:
        identity = await principal(request)
        return _json(await runtime.get(identity, _run_id(request)))

    async def cancel_run(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        data = await _body(request)
        if data.strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Cancel does not accept a request body")
        return _json(await runtime.cancel(identity, _run_id(request)))

    async def index(_: Request) -> Response:
        return _static("index.html", "text/html")

    async def javascript(_: Request) -> Response:
        return _static("app.js", "text/javascript")

    async def stylesheet(_: Request) -> Response:
        return _static("styles.css", "text/css")

    async def error(_: Request, exc: Exception) -> Response:
        if isinstance(exc, HTTPException):
            return _json({"error": str(exc.detail)}, status=exc.status_code)
        if isinstance(exc, QueueFullError):
            return _json({"error": str(exc)}, status=429)
        if isinstance(exc, HarnessUnavailableError):
            return _json({"error": str(exc)}, status=503)
        if isinstance(exc, KeyError):
            return _json({"error": "Run not found"}, status=404)
        if isinstance(exc, ValueError):
            return _json({"error": str(exc)}, status=400)
        return _json({"error": "Internal server error"}, status=500)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        if manage_lifespan:
            await runtime.start()
        try:
            yield
        finally:
            if manage_lifespan:
                await runtime.stop()

    app = Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/", index),
            Route("/assets/app.js", javascript),
            Route("/assets/styles.css", stylesheet),
            Route("/api/status", status),
            Route("/api/runs", runs, methods=["GET", "POST"]),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        ],
        exception_handlers={
            HTTPException: error,
            QueueFullError: error,
            HarnessUnavailableError: error,
            KeyError: error,
            ValueError: error,
            Exception: error,
        },
    )
    app.state.harness_runtime = runtime
    return app


def _run_id(request: Request) -> str:
    run_id = str(request.path_params["run_id"])
    if len(run_id) > 80:
        raise HTTPException(404, "Run not found")
    return run_id


def _static(filename: str, media_type: str) -> Response:
    # Callers pass only literal allowlisted filenames, never request path parts.
    path = _STATIC / filename
    if not path.is_file():
        raise HTTPException(404, "Interface is not installed")
    headers = dict(_HEADERS)
    headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'self'"
    )
    return FileResponse(path, media_type=media_type, headers=headers)
