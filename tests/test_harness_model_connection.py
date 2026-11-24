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


@pytest.mark.parametrize("reserve,expected", [(None, 8192), (512, 512), (16384, 8192)])
async def test_api_respects_output_budget_and_preserves_usage_and_long_answers(
    tmp_path: Path, reserve: int | None, expected: int,
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(
        provider="openai", base_url="https://models.test/v1", output_tokens=8192,
    ))
    content = "Long answer " * 5000

    def respond(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["max_tokens"] == expected
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 456, "irrelevant": "ignore"},
        })

    router.transport = httpx.MockTransport(respond)
    result = await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000,
                                 max_output_tokens=reserve)
    assert result["content"] == content
    assert "finish_reason" not in result
    assert result["usage"] == {"prompt_tokens": 123, "completion_tokens": 456}


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens", "content_filter"])
async def test_api_preserves_incomplete_generation_marker(
    tmp_path: Path, finish_reason: str,
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(
        provider="openai", base_url="https://models.test/v1", model="model",
    ))
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "Unfinished answer"},
                     "finish_reason": finish_reason}],
    }))
    result = await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000)
    assert result["finish_reason"] == finish_reason
    assert result["content"] == "Unfinished answer"


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
        configured = client.put(endpoint, headers=admin, json={
            "provider": "openai", "base_url": "https://models.test/v1",
            "profile": "deepseek_v4_flash",
        }).json()
        assert configured["settings"]["model"] == ""
        assert configured["settings"]["timeout_seconds"] == 1800
        assert configured["settings"]["output_tokens"] == 32768
        assert configured["resolved_profile"] == "deepseek_v4_flash"
        assert configured["profile_defaults"]["model_window_tokens"] == 256000
        assert {item["id"] for item in configured["profiles"]} == {
            "auto", "generic", "deepseek_v4_flash",
        }


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


@pytest.mark.parametrize(
    "url",
    [
        "https://models.test/v1",
        "https://models.test/v1/",
        "https://models.test/v1/chat/completions",
        "https://models.test/v1/chat/completions/",
    ],
)
async def test_keyless_gateway_accepts_base_or_complete_endpoint_without_model_name(
    tmp_path: Path, url: str
) -> None:
    router = ModelRouter(tmp_path, None)
    settings = ModelConnection(provider="openai", base_url=" " + url + " ")
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url == "https://models.test/v1/chat/completions"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert "model" not in payload
        assert payload["messages"] == [{"role": "user", "content": "Reply with OK."}]
        assert payload["stream"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    router.transport = httpx.MockTransport(upstream)
    assert (await router.probe(settings))["available"] is True
    assert router.store.load().provider == "gigacode"
    assert len(seen) == 1


def test_model_probe_uses_unsaved_settings_without_switching_active_connection(
    tmp_path: Path,
) -> None:
    async def authenticate(request: Request) -> Principal:
        return Principal(request.headers.get("x-user", "reader"))

    async def manager(request: Request) -> bool:
        return request.headers.get("x-user") == "admin"

    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), model=Runner())
    app = create_app(runtime, authenticate, authorize_model_manager=manager)
    assert isinstance(runtime.model, ModelRouter)
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url == "https://models.test/v1/chat/completions"
        assert "authorization" not in request.headers
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    runtime.model.transport = httpx.MockTransport(upstream)
    payload = {"provider": "openai", "base_url": "https://models.test/v1/chat/completions"}
    endpoint = "/api/model/connection/probe"
    with TestClient(app) as client:
        assert client.post(endpoint, json=payload).status_code == 403
        admin = {"x-user": "admin"}
        assert (
            client.post(endpoint, json=payload, headers={**admin, "origin": "https://evil.test"})
        ).status_code == 403
        runtime._signals["busy-run"] = Event()
        response = client.post(endpoint, json=payload, headers=admin)
        runtime._signals.clear()
        assert response.status_code == 200 and response.json()["available"] is True
        assert runtime.model.store.load().provider == "gigacode"
        assert len(seen) == 1
        assert client.put("/api/model/connection", json=payload, headers=admin).status_code == 200
        assert client.post(endpoint, json={}, headers=admin).json()["available"] is True
        assert len(seen) == 2
        assert (
            client.post(endpoint, json={"base_url": "file:///private"}, headers=admin).status_code
            == 400
        )


@pytest.mark.parametrize(
    "error, message",
    [
        (httpx.ConnectError("SECRET network credentials"), "Соединение не установлено"),
        (httpx.ProxyError("SECRET proxy credentials"), "HTTPS_PROXY"),
        (httpx.ReadTimeout("SECRET upstream"), "не ответил вовремя"),
    ],
)
async def test_probe_reports_safe_actionable_network_errors(
    tmp_path: Path, error: httpx.HTTPError, message: str
) -> None:
    router = ModelRouter(tmp_path, None)

    def upstream(request: httpx.Request) -> httpx.Response:
        raise error

    router.transport = httpx.MockTransport(upstream)
    result = await router.probe(
        ModelConnection(provider="openai", base_url="https://model.test/v1")
    )
    assert result["available"] is False
    assert message in result["message"]
    assert "SECRET" not in json.dumps(result)


async def test_missing_tls_identity_is_not_reported_as_invalid_model_response(
    tmp_path: Path,
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(
        ModelConnection(
            provider="openai", base_url="https://model.test/v1", tls_bundle_id="missing"
        )
    )
    result = await router.probe()
    assert not result["available"] and "сертификат" in result["message"]
    with pytest.raises(ModelConnectionError, match="сертификат"):
        await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000)


async def test_model_uses_environment_proxy_configuration(tmp_path: Path, monkeypatch: Any) -> None:
    router = ModelRouter(tmp_path, None)
    captured = []
    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        captured.append(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("agent_harness.model_connection.httpx.AsyncClient", client)
    router.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    )
    assert (
        await router.probe(ModelConnection(provider="openai", base_url="https://model.test/v1"))
    )["available"]
    assert captured[0]["trust_env"] is True
    assert captured[0]["verify"].verify_mode == ssl.CERT_REQUIRED
