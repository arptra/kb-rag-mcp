"""Manager-only upload of CA/client TLS material and metadata listings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness.tls_credentials import TLSCredentialStore, TLSCredentialUpload


def create_tls_routes(
    store: TLSCredentialStore,
    *,
    authorize: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
    in_use: Callable[[str], list[str]],
) -> list[Route]:
    """Host authorization includes fresh authentication and mutation-origin checks."""

    def reply(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(
            payload,
            status_code=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def access(request: Request) -> None:
        if not await authorize(request):
            raise HTTPException(403, "Harness administrator access required")

    async def certificates(request: Request) -> Response:
        await access(request)
        if request.method == "GET":
            return reply({"can_manage": True, "credentials": await asyncio.to_thread(store.list)})
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Content-Type must be application/json")
        try:
            upload = TLSCredentialUpload.model_validate_json(await read_body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Проверьте ID, название и TLS-сертификаты.") from None
        try:
            metadata = await asyncio.to_thread(store.save, upload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return reply({"credential": metadata}, 201)

    async def delete(request: Request) -> Response:
        await access(request)
        key_id = request.path_params["credential_id"]
        if (await read_body(request)).strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Delete does not accept a request body")
        if await asyncio.to_thread(in_use, key_id):
            raise HTTPException(
                409, "Сертификат используется. Сначала измените настройки подключения."
            )
        try:
            await asyncio.to_thread(store.delete, key_id)
        except KeyError:
            raise HTTPException(404, "TLS credential not found") from None
        except ValueError:
            raise HTTPException(400, "Invalid TLS credential ID") from None
        return reply({"deleted": True})

    return [
        Route("/api/credentials/tls", certificates, methods=["GET", "POST"]),
        Route("/api/credentials/tls/{credential_id}", delete, methods=["DELETE"]),
    ]
