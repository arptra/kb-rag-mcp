"""Small authenticated HTTP boundary for the detachable harness runtime."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from agent_harness.contracts import Principal, RunRequest
from agent_harness.control.http import create_control_routes
from agent_harness.credentials_http import create_credential_routes
from agent_harness.discovery import DiscoveryStore
from agent_harness.discovery_http import create_discovery_routes
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings
from agent_harness.memory_http import create_memory_routes
from agent_harness.model_connection import ModelRouter
from agent_harness.model_http import create_model_routes
from agent_harness.runtime import HarnessRuntime, HarnessUnavailableError, QueueFullError
from agent_harness.tls_credentials import TLSCredentialStore
from agent_harness.tls_http import create_tls_routes

Authenticate = Callable[[Request], Awaitable[Principal | None]]
AuthorizeModelManager = Callable[[Request], Awaitable[bool]]
_MAX_BODY_BYTES = 65_536
_STATIC = Path(__file__).with_name("static")
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


class ModelControl(Protocol):
    """Optional host-owned controls; status may include private browser login data."""

    def status(self) -> dict[str, Any]: ...

    def enable(self) -> dict[str, Any]: ...

    def disable(self) -> dict[str, Any]: ...


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
    model_control: ModelControl | None = None,
    authorize_model_manager: AuthorizeModelManager | None = None,
    integrations: IntegrationHub | None = None,
) -> Starlette:
    """Create an app usable alone or mounted under a host-owned authenticated path.

    Every data endpoint invokes the host's authentication callback. Public static
    files contain no runtime state, credentials or source contents. A mounted host
    enters this app's lifespan explicitly, or uses ``manage_lifespan=False`` when
    it starts/stops the runtime itself. Optional model controls require separate
    host authorization; their private state is never returned to ordinary readers.
    """

    discoveries = DiscoveryStore(runtime.settings.state_dir, runtime.settings.retention_days)
    model_router = ModelRouter(runtime.settings.state_dir, runtime.model)
    runtime.model = model_router
    tls_credentials = TLSCredentialStore(runtime.settings.state_dir)

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

    async def sandbox_profiles(request: Request) -> Response:
        identity = await principal(request)
        profiles = await integrations.profiles(identity) if integrations else []
        return _json({"profiles": profiles, "default_id": ""})

    async def runs(request: Request) -> Response:
        identity = await principal(request, mutate=request.method == "POST")
        if request.method == "GET":
            return _json({"runs": await runtime.list_runs(identity)})
        parsed = await _run_request(request)
        if parsed.mode != "quick" and parsed.sandbox_profile_id:
            available_profiles = await integrations.profiles(identity) if integrations else []
            if parsed.sandbox_profile_id not in {item["id"] for item in available_profiles}:
                raise HTTPException(403, "Выбранная среда выполнения недоступна.")
        run = await runtime.submit(identity, parsed)
        return _json(run, status=202)

    def conversation_page(request: Request) -> dict[str, int]:
        if set(request.query_params) - {"offset", "limit"}:
            raise HTTPException(400, "Invalid conversation page")
        try:
            offset = int(request.query_params.get("offset", "0"))
            limit = int(request.query_params.get("limit", "200"))
        except ValueError:
            raise HTTPException(400, "Invalid conversation page") from None
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 200:
            raise HTTPException(400, "Invalid conversation page")
        return {"offset": offset, "limit": limit}

    async def conversations(request: Request) -> Response:
        identity = await principal(request)
        return _json(await runtime.list_conversations(identity, **conversation_page(request)))

    async def conversation(request: Request) -> Response:
        identity = await principal(request, mutate=request.method == "DELETE")
        conversation_id = str(request.path_params["conversation_id"])
        if not conversation_id or len(conversation_id) > 80:
            raise HTTPException(404, "Conversation not found")
        if request.method == "DELETE":
            if (await _body(request)).strip() not in {b"", b"{}"}:
                raise HTTPException(400, "Delete does not accept a request body")
            try:
                deleted = await runtime.delete_conversation(identity, conversation_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            for run_id in deleted["deleted_ids"]:
                discoveries.remove_run(identity.owner_id, run_id)
            return _json(deleted)
        return _json(
            await runtime.get_conversation(identity, conversation_id, **conversation_page(request))
        )

    async def get_run(request: Request) -> Response:
        identity = await principal(request, mutate=request.method == "DELETE")
        run_id = _run_id(request)
        if request.method == "DELETE":
            if (await _body(request)).strip() not in {b"", b"{}"}:
                raise HTTPException(400, "Delete does not accept a request body")
            try:
                deleted = await runtime.delete_run(identity, run_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
            for deleted_id in deleted["deleted_ids"]:
                discoveries.remove_run(identity.owner_id, deleted_id)
            return _json(deleted)
        run = await runtime.get(identity, run_id)
        discoveries.scan(identity.owner_id, run)
        available = await runtime._indexes(identity)
        run["discoveries"] = discoveries.list(
            identity.owner_id,
            {item["id"] for item in available},
            integrations.store.load() if integrations else IntegrationSettings(),
            run_id=run_id,
        )
        return _json(run)

    async def cancel_run(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        data = await _body(request)
        if data.strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Cancel does not accept a request body")
        return _json(await runtime.cancel(identity, _run_id(request)))

    async def resume_run(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        try:
            payload = json.loads(await _body(request))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid resume request") from None
        if (
            not isinstance(payload, dict)
            or set(payload) - {"index_ids"}
            or (
                "index_ids" in payload
                and (
                    not isinstance(payload["index_ids"], list)
                    or len(payload["index_ids"]) > 1000
                    or any(
                        not isinstance(item, str) or len(item) > 200
                        for item in payload["index_ids"]
                    )
                )
            )
        ):
            raise HTTPException(400, "Invalid resume scope")
        try:
            run = await runtime.resume(identity, _run_id(request), payload.get("index_ids"))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return _json(run, status=202)

    async def model_status(request: Request) -> Response:
        await principal(request)
        if model_router.store.load().provider != "gigacode":
            return _json({"can_manage": False, "provider": "openai"})
        if (
            model_control is None
            or authorize_model_manager is None
            or not await authorize_model_manager(request)
        ):
            return _json({"can_manage": False})
        state = await asyncio.to_thread(model_control.status)
        return _json({**state, "can_manage": True})

    async def model_action(request: Request) -> Response:
        await principal(request, mutate=True)
        if model_router.store.load().provider != "gigacode":
            raise HTTPException(
                409, "Выбран API модели. Управление GigaCode доступно после переключения."
            )
        if model_control is None:
            raise HTTPException(503, "Model controls are not configured")
        if authorize_model_manager is None or not await authorize_model_manager(request):
            raise HTTPException(403, "Model administrator access required")
        data = await _body(request)
        if data.strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Model controls do not accept a request body")
        operation = (
            model_control.enable if request.url.path.endswith("/enable") else model_control.disable
        )
        state = await asyncio.to_thread(operation)
        return _json({**state, "can_manage": True})

    async def settings_access(request: Request) -> bool:
        await principal(request, mutate=request.method != "GET")
        return bool(
            integrations is not None
            and authorize_model_manager is not None
            and await authorize_model_manager(request)
        )

    async def manager_access(request: Request) -> bool:
        await principal(request, mutate=request.method != "GET")
        return bool(authorize_model_manager and await authorize_model_manager(request))

    async def integration_settings(request: Request) -> Response:
        permitted = await settings_access(request)
        if not permitted:
            if request.method == "GET":
                return _json({"can_manage": False})
            raise HTTPException(403, "Harness administrator access required")
        assert integrations is not None
        if request.method == "PUT":
            try:
                parsed = IntegrationSettings.model_validate_json(await _body(request))
            except (ValidationError, ValueError):
                raise HTTPException(
                    400, "Проверьте адреса, ID и ограничения подключений."
                ) from None
            key_ids = {item["id"] for item in integrations.credentials.list()}
            if any(
                repository.ssh_key_id and repository.ssh_key_id not in key_ids
                for repository in parsed.repositories
            ):
                raise HTTPException(400, "Выбранный SSH-ключ не найден.")
            await asyncio.to_thread(integrations.store.save, parsed)
        settings = await asyncio.to_thread(integrations.store.load)
        return _json({"can_manage": True, "settings": settings.model_dump()})

    async def probe_integration(request: Request) -> Response:
        if not await settings_access(request):
            raise HTTPException(403, "Harness administrator access required")
        assert integrations is not None
        try:
            payload = json.loads(await _body(request))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid probe request") from None
        if (
            not isinstance(payload, dict)
            or set(payload) - {"kind", "id"}
            or not isinstance(payload.get("kind"), str)
            or payload.get("kind") not in {"repository", "connection", "sandbox", "sandbox_profile"}
            or not isinstance(payload.get("id", ""), str)
        ):
            raise HTTPException(400, "Invalid probe request")
        try:
            result = await asyncio.wait_for(
                integrations.probe(payload["kind"], payload.get("id", "")),
                65,
            )
        except TimeoutError:
            result = {"available": False, "message": "Подключение не ответило вовремя."}
        return _json(result)

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
            Route("/api/sandbox/profiles", sandbox_profiles),
            Route("/api/model", model_status),
            Route("/api/model/enable", model_action, methods=["POST"]),
            Route("/api/model/disable", model_action, methods=["POST"]),
            Route("/api/settings", integration_settings, methods=["GET", "PUT"]),
            Route("/api/settings/probe", probe_integration, methods=["POST"]),
            Route("/api/runs", runs, methods=["GET", "POST"]),
            Route("/api/conversations", conversations),
            Route("/api/conversations/{conversation_id}", conversation, methods=["GET", "DELETE"]),
            Route("/api/runs/{run_id}", get_run, methods=["GET", "DELETE"]),
            Route("/api/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
            Route("/api/runs/{run_id}/resume", resume_run, methods=["POST"]),
            *(create_control_routes(runtime, authorize=manager_access, read_body=_body)
              if getattr(runtime.settings, "control_enabled", True) else []),
            *create_memory_routes(runtime, principal),
            *create_discovery_routes(
                runtime,
                discoveries,
                principal,
                manager_access,
                _body,
                integrations,
            ),
            *create_model_routes(
                model_router,
                authorize=manager_access,
                read_body=_body,
                busy=lambda: bool(runtime._signals),
            ),
            *create_tls_routes(
                tls_credentials,
                authorize=manager_access,
                read_body=_body,
                in_use=lambda key_id: (
                    ["model"] if model_router.store.load().tls_bundle_id == key_id else []
                ),
            ),
            *(
                create_credential_routes(
                    integrations.credentials,
                    authorize=settings_access,
                    read_body=_body,
                    in_use=lambda key_id: [
                        item.id
                        for item in integrations.store.load().repositories
                        if item.ssh_key_id == key_id
                    ],
                )
                if integrations
                else []
            ),
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
