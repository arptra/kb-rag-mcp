"""Administrator-only Docker image inventory, including unsaved connection settings."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import DockerEngineConfig
from agent_harness.sandbox import SandboxError


class _InventoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine: DockerEngineConfig


def create_sandbox_image_route(
    integrations: IntegrationHub | None,
    *,
    authorize: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
) -> Route:
    async def images(request: Request) -> JSONResponse:
        if not await authorize(request) or integrations is None:
            raise HTTPException(403, "Harness administrator access required")
        try:
            payload = _InventoryRequest.model_validate_json(await read_body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Проверьте адрес, порт и настройки Docker Engine.") from None
        try:
            inventory = await integrations.sandbox.list_images(payload.engine)
        except SandboxError as exc:
            raise HTTPException(502, str(exc)) from None
        except (TimeoutError, OSError):
            raise HTTPException(503, "Docker Engine недоступен или не ответил вовремя.") from None
        return JSONResponse(inventory, headers={"Cache-Control": "no-store"})

    return Route("/api/settings/sandbox/images", images, methods=["POST"])
