"""Admin and personal credential routes. Responses only expose credential metadata."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness.contracts import Principal
from agent_harness.credentials import (
    CredentialStore,
    PersonalCredentials,
    PersonalUpload,
    SSHKeyUpload,
)
from agent_harness.integrations_config import IntegrationStore


def create_credential_routes(
    store: CredentialStore,
    *,
    authorize: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
    in_use: Callable[[str], list[str]],
) -> list[Route]:
    """authorize must authenticate each call and check same-origin for mutations."""

    def reply(payload: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(
            payload,
            status_code=status,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def access(request: Request) -> None:
        if not await authorize(request):
            raise HTTPException(403, "Harness administrator access required")

    async def keys(request: Request) -> Response:
        await access(request)
        if request.method == "GET":
            return reply({"can_manage": True, "credentials": await asyncio.to_thread(store.list)})
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Content-Type must be application/json")
        try:
            upload = SSHKeyUpload.model_validate_json(await read_body(request))
        except (ValidationError, ValueError):
            # ValidationError includes the submitted private key; never return it.
            raise HTTPException(
                400, "Проверьте ID, название, приватный ключ и known_hosts."
            ) from None
        try:
            saved = await asyncio.to_thread(store.save, upload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return reply({"credential": saved}, 201)

    async def delete(request: Request) -> Response:
        await access(request)
        key_id = request.path_params["credential_id"]
        if (await read_body(request)).strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Delete does not accept a request body")
        if await asyncio.to_thread(in_use, key_id):
            raise HTTPException(
                409, "Ключ используется репозиторием. Сначала измените настройки репозитория."
            )
        try:
            await asyncio.to_thread(store.delete, key_id)
        except KeyError:
            raise HTTPException(404, "SSH key not found") from None
        except ValueError:
            raise HTTPException(400, "Invalid SSH key ID") from None
        return reply({"deleted": True})

    return [
        Route("/api/credentials/ssh", keys, methods=["GET", "POST"]),
        Route("/api/credentials/ssh/{credential_id}", delete, methods=["DELETE"]),
    ]


def create_personal_credential_routes(
    store: PersonalCredentials, integrations: IntegrationStore, *,
    identify: Callable[[Request], Awaitable[Principal]],
    read_body: Callable[[Request], Awaitable[bytes]],
) -> list[Route]:
    async def personal(request: Request) -> Response:
        identity = await identify(request)
        if identity.shared:
            raise HTTPException(403, "Для личного доступа войдите под своей учётной записью.")
        owner = identity.owner_id
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if request.method == "DELETE":
            removed = await asyncio.to_thread(store.delete, owner, request.path_params["id"])
            if not removed:
                raise HTTPException(404, "Личный доступ не найден.")
            return JSONResponse({"ok": True}, headers=headers)
        settings = await asyncio.to_thread(integrations.load)
        repos = {r.id: r for r in settings.repositories if r.allows(owner)}
        if request.method == "GET":
            return JSONResponse({
                "credentials": await asyncio.to_thread(store.list, owner),
                "repositories": [{"id": r.id, "name": r.name, "url": r.url,
                                  "ssh": not r.url.startswith("https://")} for r in repos.values()],
            }, headers=headers)
        if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
            raise HTTPException(415, "Content-Type must be application/json")
        try:
            upload = PersonalUpload.validate_json(await read_body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Проверьте поля доступа и адрес репозитория.") from None
        repo = repos.get(upload.repository_id)
        if not repo:
            raise HTTPException(403, "Репозиторий недоступен.")
        try:
            saved = await asyncio.to_thread(store.save, owner, repo.url, upload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return JSONResponse({"credential": saved}, status_code=201, headers=headers)

    return [
        Route("/api/credentials/personal", personal, methods=["GET", "POST"]),
        Route("/api/credentials/personal/{id}", personal, methods=["DELETE"]),
    ]
