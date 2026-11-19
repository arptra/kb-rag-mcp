import asyncio
import json
import ssl
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.model_connection import ModelConnection, ModelConnectionError, ModelRouter
from agent_harness.runtime import HarnessRuntime


class Runner:
    def status(self) -> dict[str, Any]:
        return {"available": True, "name": "GigaCode"}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": "Runner answer", "tool_calls": []}


async def test_default_runner_and_persisted_direct_model_switch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    router = ModelRouter(tmp_path, Runner())
    assert router.status()["provider"] == "gigacode"
    assert (await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000))[
        "content"
    ] == "Runner answer"
    router.store.save(
        ModelConnection(
            provider="openai",
            base_url="https://models.test/v1",
            model="local-model",
            api_key_env="TEST_MODEL_KEY",
        )
    )
    assert ModelRouter(tmp_path, None).store.load().provider == "openai"
    assert not router.status()["available"]
    monkeypatch.setenv("TEST_MODEL_KEY", "private-key-value")
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer private-key-value"
        assert request.url == "https://models.test/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == "local-model" and payload["tools"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "private-key-value",
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "read_document",
                                        "arguments": '{"document_id":"doc"}',
                                    }
                                }
                            ],
                        }
                    }
                ]
            },
        )

    router.transport = httpx.MockTransport(upstream)
    result = await router.decide(
        messages=[{"role": "user", "content": "Question"}],
        tools=[{"function": {"name": "read_document"}}],
        cancel=Event(),
        max_input_chars=10000,
    )
    assert result == {
        "content": "[redacted]",
        "tool_calls": [{"name": "read_document", "args": {"document_id": "doc"}}],
    }
    assert len(seen) == 1
    assert router._ssl(router.store.load()).verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
async def test_remote_errors_do_not_leak_body_and_redirects_not_followed(
    tmp_path: Path, status: int
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(
        ModelConnection(provider="openai", base_url="https://models.test/v1", model="model")
    )
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text="SECRET", headers={"Location": "https://other.test"})

    router.transport = httpx.MockTransport(upstream)
    with pytest.raises(ModelConnectionError) as error:
        await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000)
    assert "SECRET" not in str(error.value) and len(calls) == 1


async def test_model_cancellation_closes_pending_request(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(
        ModelConnection(provider="openai", base_url="https://models.test/v1", model="model")
    )
    started, closed, cancel = asyncio.Event(), asyncio.Event(), Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return httpx.Response(200)

    router.transport = httpx.MockTransport(upstream)
    task = asyncio.create_task(
        router.decide(messages=[], tools=[], cancel=cancel, max_input_chars=10000)
    )
    await asyncio.wait_for(started.wait(), 2)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert closed.is_set()


def test_model_http_manager_origin_and_busy_guard(tmp_path: Path) -> None:
    async def authenticate(request: Request) -> Principal | None:
        return Principal(request.headers["x-user"]) if request.headers.get("x-user") else None

    async def manager(request: Request) -> bool:
        return request.headers.get("x-user") == "admin"

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), model=Runner())
    app = create_app(runtime, authenticate, authorize_model_manager=manager)
    with TestClient(app) as client:
        endpoint, admin = "/api/model/connection", {"x-user": "admin"}
        assert client.get(endpoint).status_code == 401
        assert client.get(endpoint, headers={"x-user": "reader"}).status_code == 403
        assert client.get(endpoint, headers=admin).json()["settings"]["provider"] == "gigacode"
        payload = {"provider": "openai", "base_url": "https://models.test/v1", "model": "model"}
        assert (
            client.put(
                endpoint, headers={**admin, "origin": "https://evil.test"}, json=payload
            ).status_code
            == 403
        )
        runtime._signals["busy-run"] = Event()
        assert client.put(endpoint, headers=admin, json=payload).status_code == 409
        runtime._signals.clear()
        assert client.put(endpoint, headers=admin, json=payload).status_code == 200
        assert client.get("/api/status", headers=admin).json()["model"]["provider"] == "openai"


@pytest.mark.parametrize(
    "url",
    [
        "http://model.test",
        "https://secret@model.test",
        "https://model.test?token=x",
        "file:///tmp/model",
    ],
)
def test_model_endpoint_rejects_insecure_or_secret_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        ModelConnection(provider="openai", base_url=url, model="model")


async def test_model_switch_rechecks_runs_started_while_request_body_arrives(
    tmp_path: Path,
) -> None:
    async def authenticate(request: Request) -> Principal:
        return Principal("admin")

    async def manager(request: Request) -> bool:
        return True

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), model=Runner())
    app = create_app(runtime, authenticate, authorize_model_manager=manager)

    async def streaming_body():
        # Simulate a run starting while the settings request is still being received.
        runtime._signals["concurrent-run"] = Event()
        yield json.dumps(
            {"provider": "openai", "base_url": "https://model.test/v1", "model": "m"}
        ).encode()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.put("/api/model/connection", content=streaming_body())
        assert response.status_code == 409
    assert isinstance(runtime.model, ModelRouter)
    assert runtime.model.store.load().provider == "gigacode"


async def test_api_token_echo_in_tool_argument_keys_is_redacted(
    tmp_path: Path, monkeypatch: Any
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(
        ModelConnection(
            provider="openai",
            base_url="https://model.test/v1",
            model="m",
            api_key_env="TEST_MODEL_KEY",
        )
    )
    monkeypatch.setenv("TEST_MODEL_KEY", "secret-value")
    router.transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "tool",
                                        "arguments": '{"secret-value":"safe"}',
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    result = await router.decide(
        messages=[], tools=[{"function": {"name": "tool"}}], cancel=Event(), max_input_chars=10000
    )
    assert "secret-value" not in json.dumps(result)
    assert result["tool_calls"][0]["args"] == {"[redacted]": "safe"}
