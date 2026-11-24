"""Manager-owned model selection, including a certificate-verified API alternative."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness.model_connection import ModelConnection, ModelRouter
from agent_harness.model_profiles import model_profile_defaults, model_profile_options
from agent_harness.tls_credentials import TLSCredentialStore


def create_model_routes(
    router: ModelRouter,
    *,
    authorize: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
    busy: Callable[[], bool],
) -> list[Route]:
    certificates = TLSCredentialStore(router.state_dir)

    async def access(request: Request) -> None:
        if not await authorize(request):
            raise HTTPException(403, "Harness administrator access required")

    async def settings(request: Request) -> Response:
        await access(request)
        if request.method == "PUT":
            if busy():
                raise HTTPException(409, "Дождитесь завершения исследований перед сменой модели.")
            try:
                parsed = ModelConnection.model_validate_json(await read_body(request))
            except (ValueError, ValidationError):
                raise HTTPException(
                    400, "Проверьте HTTPS-адрес API и настройки подключения."
                ) from None
            if parsed.tls_bundle_id:
                try:
                    await asyncio.to_thread(certificates.ssl_context, parsed.tls_bundle_id)
                except KeyError:
                    raise HTTPException(400, "Выбранный сертификат не найден.") from None
                except (ValueError, OSError):
                    raise HTTPException(
                        400, "Ошибка загрузки выбранных TLS-сертификатов. Проверьте набор."
                    ) from None
            # Keep final busy check and config commit in one event-loop turn. A run
            # must not enter between the check and publication of a new endpoint.
            if busy():
                raise HTTPException(409, "Дождитесь завершения исследований перед сменой модели.")
            router.store.save(parsed)
        saved, bundles = await asyncio.gather(
            asyncio.to_thread(router.store.load), asyncio.to_thread(certificates.list)
        )
        return JSONResponse(
            {
                "can_manage": True,
                "settings": saved.model_dump(),
                "tls_bundles": bundles,
                "resolved_profile": saved.resolved_profile,
                "profile_defaults": model_profile_defaults(saved.resolved_profile),
                "profiles": model_profile_options(),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def probe(request: Request) -> Response:
        await access(request)
        body = (await read_body(request)).strip()
        candidate = None
        if body not in {b"", b"{}"}:
            try:
                candidate = ModelConnection.model_validate_json(body)
            except (ValueError, ValidationError):
                raise HTTPException(
                    400, "Проверьте HTTPS-адрес API и настройки подключения."
                ) from None
        # Testing a candidate must neither replace the active model nor interrupt
        # investigations. Empty bodies retain the previous saved-settings contract.
        return JSONResponse(await router.probe(candidate), headers={"Cache-Control": "no-store"})

    return [
        Route("/api/model/connection", settings, methods=["GET", "PUT"]),
        Route("/api/model/connection/probe", probe, methods=["POST"]),
    ]
