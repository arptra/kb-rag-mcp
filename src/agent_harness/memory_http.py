"""Authenticated private-memory routes, mountable by the small Harness HTTP app."""

from __future__ import annotations

from typing import Protocol

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from agent_harness.contracts import Principal
from agent_harness.memory import MemoryFeedback, MemoryInput, MemoryPreferences
from agent_harness.runtime import HarnessRuntime


class PrincipalResolver(Protocol):
    async def __call__(self, request: Request, *, mutate: bool = False) -> Principal: ...


def create_memory_routes(runtime: HarnessRuntime, principal: PrincipalResolver) -> list[Route]:
    # Delayed import avoids a cycle while reusing the host boundary's size/origin rules.
    from agent_harness.http import _body, _json

    async def memories(request: Request) -> Response:
        identity = await principal(request, mutate=request.method != "GET")
        if request.method == "GET":
            return _json(await runtime.list_memory(identity))
        try:
            payload = MemoryInput.model_validate_json(await _body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Проверьте текст, подтверждение и область памяти.") from None
        return _json(await runtime.save_memory(identity, payload), status=201)

    async def memory(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        memory_id = str(request.path_params["memory_id"])
        if len(memory_id) > 80:
            raise HTTPException(404, "Memory not found")
        if request.method == "DELETE":
            return _json(await runtime.delete_memory(identity, memory_id))
        try:
            payload = MemoryInput.model_validate_json(await _body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Проверьте текст, подтверждение и область памяти.") from None
        return _json(await runtime.save_memory(identity, payload, memory_id))

    async def preferences(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        try:
            payload = MemoryPreferences.model_validate_json(await _body(request))
        except (ValidationError, ValueError):
            raise HTTPException(400, "Укажите, включена ли память.") from None
        return _json(await runtime.set_memory_enabled(identity, payload.enabled))

    async def feedback(request: Request) -> Response:
        identity = await principal(request, mutate=True)
        run_id = str(request.path_params["run_id"])
        if len(run_id) > 80:
            raise HTTPException(404, "Run not found")
        try:
            payload = MemoryFeedback.model_validate_json(await _body(request))
        except (ValidationError, ValueError):
            raise HTTPException(
                400, "Укажите оценку ответа и, при необходимости, комментарий."
            ) from None
        return _json(await runtime.set_run_feedback(identity, run_id, payload))

    return [
        Route("/api/memory", memories, methods=["GET", "POST"]),
        Route("/api/memory/preferences", preferences, methods=["PUT"]),
        Route("/api/memory/{memory_id}", memory, methods=["PUT", "DELETE"]),
        Route("/api/runs/{run_id}/feedback", feedback, methods=["PUT"]),
    ]
