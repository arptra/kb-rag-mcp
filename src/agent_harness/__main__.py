"""Run the detachable harness with one explicitly configured shared account."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationStore
from agent_harness.runtime import HarnessRuntime


def _providers(specification: str) -> tuple[Any, Any]:
    if not specification:
        return None, None
    module_name, separator, factory_name = specification.partition(":")
    if not separator or not module_name or not factory_name or ":" in factory_name:
        raise ValueError("HARNESS_PROVIDER_FACTORY must be module:function")
    factory = getattr(importlib.import_module(module_name), factory_name)
    if not callable(factory):
        raise ValueError("HARNESS_PROVIDER_FACTORY must point to a callable")
    value = factory()
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("HARNESS_PROVIDER_FACTORY must return (source, model)")
    source, model = value
    if source is not None and any(
        not callable(getattr(source, name, None)) for name in ("indexes", "search", "read")
    ):
        raise ValueError("Source provider must implement indexes, search and read")
    if model is not None and any(
        not callable(getattr(model, name, None)) for name in ("status", "decide")
    ):
        raise ValueError("Model provider must implement status and decide")
    return source, model


def build_app(environ: Mapping[str, str] | None = None) -> Starlette:
    """Build an isolated single-account app; shared tokens share history and access."""
    environment = os.environ if environ is None else environ
    token = environment.get("HARNESS_API_TOKEN", "")
    if len(token) < 32 or token != token.strip():
        raise ValueError("HARNESS_API_TOKEN must contain at least 32 non-padded characters")
    token_bytes = token.encode("utf-8")
    identity = Principal(
        owner_id="standalone:" + hashlib.sha256(token_bytes).hexdigest(),
        shared=True,
    )

    async def authenticate(request: Request) -> Principal | None:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return None
        scheme, separator, supplied = values[0].partition(" ")
        if not separator or scheme.lower() != "bearer":
            return None
        if hmac.compare_digest(supplied.encode("utf-8"), token_bytes):
            return identity
        return None

    state_dir = environment.get("HARNESS_STATE_DIR")
    settings = (
        HarnessSettings(enabled=True, state_dir=Path(state_dir))
        if state_dir
        else HarnessSettings(enabled=True)
    )
    source, model = _providers(environment.get("HARNESS_PROVIDER_FACTORY", ""))
    control = getattr(model, "model_control", None)

    async def manager(request: Request) -> bool:
        # This entrypoint has one explicitly configured operator account.
        return await authenticate(request) is not None

    async def authorized(principal: Principal) -> bool:
        return principal == identity

    integrations = IntegrationHub(IntegrationStore(settings.state_dir), authorized)

    app = create_app(
        HarnessRuntime(settings, source=source, model=model, tools=integrations),
        authenticate,
        model_control=control,
        authorize_model_manager=manager,
        integrations=integrations,
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: Starlette) -> AsyncIterator[None]:
        try:
            async with original_lifespan(application):
                yield
        finally:
            close = getattr(model, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("HARNESS_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("HARNESS_PORT", "8020"))
        if not 1 <= port <= 65535:
            raise ValueError("HARNESS_PORT must be between 1 and 65535")
        app = build_app()
    except (ValueError, ImportError, AttributeError) as exc:
        raise SystemExit(str(exc)) from exc
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
