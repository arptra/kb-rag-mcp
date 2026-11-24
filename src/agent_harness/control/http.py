"""Administrator-only APIs and a standalone static shell for the Harness control plane."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_harness.contracts import Principal
from agent_harness.control.metrics import collect_metrics
from agent_harness.control.settings import (
    RESTART_FIELDS,
    ControlSettings,
    ControlStore,
    RevisionConflict,
    SettingsUpdate,
)

if TYPE_CHECKING:
    from agent_harness.runtime import HarnessRuntime

_STATIC = Path(__file__).with_name("static")
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def create_control_routes(
    runtime: HarnessRuntime,
    *,
    authorize: Callable[[Request], Awaitable[bool]],
    read_body: Callable[[Request], Awaitable[bytes]],
) -> list[Route]:
    store = ControlStore(runtime.settings)

    async def access(request: Request) -> None:
        if not await authorize(request):
            raise HTTPException(403, "Harness administrator access required")

    def response(settings: ControlSettings) -> JSONResponse:
        data = settings.model_dump(exclude={"revision"})
        restart = [
            field
            for field in RESTART_FIELDS
            if getattr(settings.runtime, field) != getattr(runtime.settings, field)
        ]
        schema = ControlSettings.model_json_schema()
        schema["properties"].pop("revision", None)
        return JSONResponse(
            {
                "can_manage": True,
                "revision": settings.revision,
                "settings": data,
                "schema": schema,
                "activation": {
                    "new_runs": "Профили и ограничения закрепляются при запуске нового запроса",
                    "restart_fields": list(RESTART_FIELDS),
                    "restart_required": restart,
                    "running_unchanged": True,
                },
                "external_limits": [
                    {
                        "scope": "model",
                        "message": "Реальное окно, rate limits и лимиты CLI задаёт провайдер; "
                        "Harness не может повысить их настройкой бюджета.",
                    },
                    {
                        "scope": "connections",
                        "message": "Сертификаты, адрес модели и ограничения песочниц "
                        "настраиваются в разделе подключений Harness.",
                    },
                    {
                        "scope": "access",
                        "message": "Права доступа и изоляция песочниц не являются бюджетом "
                        "и не расширяются агентом.",
                    },
                ],
            },
            headers=_HEADERS,
        )

    async def settings(request: Request) -> Response:
        await access(request)
        if request.method == "PUT":
            try:
                update = SettingsUpdate.model_validate_json(await read_body(request))
                if "revision" in update.settings:
                    raise ValueError("Revision must be supplied outside settings")
                parsed = ControlSettings.model_validate(update.settings)
            except (ValueError, ValidationError):
                raise HTTPException(
                    400, "Проверьте бюджеты, потолки и параметры контекста."
                ) from None
            try:
                saved = await asyncio.to_thread(
                    store.save,
                    parsed,
                    expected_revision=update.revision,
                )
            except RevisionConflict as exc:
                raise HTTPException(409, str(exc)) from None
        else:
            saved = await asyncio.to_thread(store.load)
        return response(saved)

    async def metrics(request: Request) -> Response:
        await access(request)
        if set(request.query_params) - {"days"}:
            raise HTTPException(400, "Unknown metrics query parameter")
        try:
            days = int(request.query_params.get("days", "7"))
            if not 1 <= days <= 90:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Период должен составлять от 1 до 90 дней.") from None
        data = await asyncio.to_thread(collect_metrics, runtime.store, days=days)
        data["queues"] = {
            name: {"queued": queue.qsize(), "workers": getattr(runtime.settings, name + "_workers")}
            for name, queue in runtime._queues.items()
        }
        return JSONResponse(data, headers=_HEADERS)

    async def cancel_run(request: Request) -> Response:
        await access(request)
        if (await read_body(request)).strip() not in {b"", b"{}"}:
            raise HTTPException(400, "Cancel does not accept a request body")
        run_id = str(request.path_params["run_id"])
        if not run_id or len(run_id) > 80:
            raise HTTPException(404, "Run not found")
        # The administrator can stop work, but the caller never supplies an owner
        # and receives no private conversation/source payload from this endpoint.
        with runtime.store._connect() as db:
            row = db.execute("SELECT owner FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        result = await runtime.cancel(Principal(str(row[0])), run_id)
        return JSONResponse({"id": run_id, "status": result["status"]}, headers=_HEADERS)

    async def index(_: Request) -> Response:
        return FileResponse(_STATIC / "index.html", media_type="text/html", headers=_HEADERS)

    async def redirect(request: Request) -> Response:
        return RedirectResponse(
            str(request.url.replace(path=request.url.path + "/")), status_code=307
        )

    async def asset(request: Request) -> Response:
        filename = str(request.path_params["filename"])
        if filename not in {"app.js", "styles.css", "chart.umd.min.js"}:
            raise HTTPException(404, "Asset not found")
        path = _STATIC / filename
        if not path.is_file():
            raise HTTPException(404, "Asset not found")
        return FileResponse(
            path,
            media_type="text/css" if filename.endswith(".css") else "text/javascript",
            headers=_HEADERS,
        )

    return [
        Route("/control", redirect),
        Route("/control/", index),
        Route("/control/assets/{filename}", asset),
        Route("/api/control/settings", settings, methods=["GET", "PUT"]),
        Route("/api/control/metrics", metrics),
        Route("/api/control/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
    ]
