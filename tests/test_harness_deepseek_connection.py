from __future__ import annotations

import asyncio
import json
import sqlite3
import ssl
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from agent_harness.contracts import ModelDecisionError
from agent_harness.model_connection import (
    INCOMPLETE_FINISH_REASONS,
    ModelConnection,
    ModelConnectionError,
    ModelConnectionStore,
    ModelRouter,
    ModelTransientError,
)
from agent_harness.model_profiles import (
    DEEPSEEK_CONTEXT_WINDOW_TOKENS,
    DEEPSEEK_OUTPUT_TOKENS,
    DEEPSEEK_TIMEOUT_SECONDS,
    model_profile_defaults,
)


@pytest.mark.parametrize("model", ["deepseek-flash", "DeepSeek-V4-Flash", "deepseek_flash_v4"])
def test_deepseek_auto_defaults_and_explicit_generic_override(model: str) -> None:
    settings = ModelConnection(provider="openai", base_url="https://models.test", model=model)
    assert settings.resolved_profile == "deepseek_v4_flash"
    assert settings.timeout_seconds == DEEPSEEK_TIMEOUT_SECONDS == 1800
    assert settings.output_tokens == DEEPSEEK_OUTPUT_TOKENS == 32768
    assert model_profile_defaults(settings.resolved_profile)["model_window_tokens"] == (
        DEEPSEEK_CONTEXT_WINDOW_TOKENS
    ) == 256000
    generic = ModelConnection(
        provider="openai", base_url="https://models.test", model=model, profile="generic",
    )
    assert generic.resolved_profile == "generic"
    assert generic.timeout_seconds == 60 and generic.output_tokens == 4096


@pytest.mark.parametrize("model", [
    "deepseek-r1", "DeepSeek-V3", "deepseek-chat", "deepseek-reasoner", "deepseek-v4-pro",
])
def test_older_or_different_deepseek_models_do_not_inherit_flash_profile(model: str) -> None:
    settings = ModelConnection(provider="openai", base_url="https://models.test", model=model)
    assert settings.resolved_profile == "generic"
    assert settings.timeout_seconds == 60 and settings.output_tokens == 4096


def test_profile_supports_model_selected_by_keyless_gateway_and_custom_limits() -> None:
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    )
    assert not settings.model and not settings.api_key_env
    assert settings.timeout_seconds == 1800
    customized = ModelConnection.model_validate({
        **settings.model_dump(), "timeout_seconds": 2400, "output_tokens": 65536,
    })
    assert customized.timeout_seconds == 2400 and customized.output_tokens == 65536
    assert ModelConnection().provider == "gigacode"


@pytest.mark.parametrize(
    "timeout, output, expected_timeout, expected_output",
    [(60, 4096, 1800, 32768), (225, 4096, 225, 32768), (60, 8192, 1800, 8192),
     (225, 8192, 225, 8192)],
)
def test_legacy_deepseek_migration_preserves_custom_values(
    tmp_path: Path, timeout: int, output: int, expected_timeout: int, expected_output: int,
) -> None:
    store = ModelConnectionStore(tmp_path)
    legacy = {
        "provider": "openai", "base_url": "https://models.test", "model": "deepseek-flash",
        "timeout_seconds": timeout, "output_tokens": output,
    }
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO model_connection VALUES(1, ?)", (json.dumps(legacy),))
    settings = store.load()
    assert settings.timeout_seconds == expected_timeout
    assert settings.output_tokens == expected_output
    store.save(settings)
    assert store.load() == settings


def test_new_explicit_small_limits_are_not_remigrated(tmp_path: Path) -> None:
    store = ModelConnectionStore(tmp_path)
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", model="deepseek-flash",
        timeout_seconds=60, output_tokens=4096,
    )
    store.save(settings)
    assert store.load().timeout_seconds == 60 and store.load().output_tokens == 4096


@pytest.mark.parametrize("probe", [False, True])
async def test_deepseek_uses_non_thinking_protocol_with_long_read_timeout(
    tmp_path: Path, probe: bool,
) -> None:
    router = ModelRouter(tmp_path, None)
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    )
    router.store.save(settings)

    async def upstream(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == (128 if probe else 32768)
        assert "model" not in payload and "authorization" not in request.headers
        assert request.extensions["timeout"] == {
            "connect": 30, "read": 1800, "write": 60, "pool": 30,
        }
        # An asynchronously delayed response and DeepSeek keep-alive whitespace
        # remain valid; neither is mistaken for an empty model response.
        await asyncio.sleep(0.02)
        data = json.dumps({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})
        return httpx.Response(200, content="\n\r\n" + data)

    router.transport = httpx.MockTransport(upstream)
    if probe:
        assert (await router.probe())["available"] is True
    else:
        result = await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000)
        assert result == {"content": "OK", "tool_calls": []}


async def test_generic_api_receives_no_deepseek_extension(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)

    def upstream(request: httpx.Request) -> httpx.Response:
        assert "thinking" not in json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

    router.transport = httpx.MockTransport(upstream)
    result = await router.probe(ModelConnection(provider="openai", base_url="https://models.test"))
    assert result["available"] is True


@pytest.mark.parametrize("reason", sorted(INCOMPLETE_FINISH_REASONS))
async def test_unfinished_tool_calls_are_not_parsed_or_executable(
    tmp_path: Path, reason: str,
) -> None:
    router = ModelRouter(tmp_path, None)
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    )
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {
            "content": "Проверяю документы",
            "tool_calls": [{"function": {"name": "read_document", "arguments": '{"id":'}}],
        }, "finish_reason": reason}],
    }))
    result = await router._completion(settings, [], [{"function": {"name": "read_document"}}])
    assert result == {
        "content": "Проверяю документы", "tool_calls": [], "finish_reason": reason,
        "incomplete_tool_calls": True,
    }


@pytest.mark.parametrize("reason", ["aborted", "insufficient_system_resource", "length"])
async def test_interrupted_answer_is_never_a_successful_probe(tmp_path: Path, reason: str) -> None:
    router = ModelRouter(tmp_path, None)
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "O"}, "finish_reason": reason}],
    }))
    result = await router.probe(ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    ))
    assert result["available"] is False and "не завершил" in result["message"]


async def test_private_reasoning_is_never_returned_as_user_answer(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": None, "reasoning_content": "PRIVATE_REASONING"},
                     "finish_reason": "length"}],
    }))
    result = await router._completion(ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    ), [], [])
    assert result == {"content": "", "tool_calls": [], "finish_reason": "length"}
    assert "PRIVATE_REASONING" not in json.dumps(result)


@pytest.mark.parametrize("status, temporary", [
    (429, True), (500, True), (502, True), (503, True), (504, True),
    (400, False), (401, False), (403, False), (404, False), (422, False),
])
async def test_only_temporary_http_failures_are_retryable(
    tmp_path: Path, status: int, temporary: bool,
) -> None:
    router = ModelRouter(tmp_path, None)
    router.transport = httpx.MockTransport(lambda request: httpx.Response(status, text="SECRET"))
    with pytest.raises(ModelConnectionError) as error:
        await router._completion(ModelConnection(
            provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
        ), [], [])
    assert isinstance(error.value, ModelTransientError) is temporary
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("failure", [
    httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ReadError,
    httpx.ConnectError, httpx.RemoteProtocolError,
])
async def test_network_and_generation_timeouts_are_retryable(
    tmp_path: Path, failure: type[httpx.HTTPError],
) -> None:
    router = ModelRouter(tmp_path, None)

    def upstream(request: httpx.Request) -> httpx.Response:
        raise failure("SECRET upstream details")

    router.transport = httpx.MockTransport(upstream)
    with pytest.raises(ModelTransientError) as error:
        await router._completion(ModelConnection(
            provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
        ), [], [])
    assert "SECRET" not in str(error.value)


async def test_tls_certificate_failure_is_permanent(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)

    def upstream(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("SECRET certificate details") from ssl.SSLCertVerificationError()

    router.transport = httpx.MockTransport(upstream)
    with pytest.raises(ModelConnectionError) as error:
        await router._completion(ModelConnection(
            provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
        ), [], [])
    assert not isinstance(error.value, ModelTransientError)
    assert "сертификат" in str(error.value) and "SECRET" not in str(error.value)


async def test_total_request_timeout_is_retryable_and_closes_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = ModelRouter(tmp_path, None)
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    )
    # Accelerate the already-validated setting to avoid a 30-minute timeout test.
    monkeypatch.setattr(router.store, "load", lambda: settings.model_copy(
        update={"timeout_seconds": 0.02},
    ))
    closed = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
        return httpx.Response(200)

    router.transport = httpx.MockTransport(upstream)
    with pytest.raises(ModelTransientError, match="вовремя"):
        await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=10000)
    assert closed.is_set()


@pytest.mark.parametrize("calls", [
    [{"function": {"name": "read_document", "arguments": '{"id":"SECRET"'}}],
    [{"function": {"name": "SECRET_unknown_tool", "arguments": "{}"}}],
    [{"function": {"name": "read_document", "arguments": "[]"}}],
    [{"function": {"name": "read_document", "arguments": "null"}}],
    [{"function": {"name": "read_document", "arguments": {"id": "SECRET"}}}],
    [{"SECRET": "missing_function"}],
    {},
])
async def test_malformed_model_actions_are_repairable_and_probe_safe(
    tmp_path: Path, calls: Any,
) -> None:
    router = ModelRouter(tmp_path, None)
    settings = ModelConnection(
        provider="openai", base_url="https://models.test", profile="deepseek_v4_flash",
    )
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": None, "tool_calls": calls},
                     "finish_reason": "tool_calls"}],
    }))
    with pytest.raises(ModelDecisionError) as error:
        await router._completion(settings, [], [{"function": {"name": "read_document"}}])
    assert "SECRET" not in str(error.value)
    check = await router.probe(settings)
    assert not check["available"] and "SECRET" not in json.dumps(check)


async def test_invalid_http_response_envelope_is_not_a_model_action_repair(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)
    router.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "error": "SECRET gateway does not support chat completions",
    }))
    with pytest.raises(ModelConnectionError) as error:
        await router._completion(ModelConnection(
            provider="openai", base_url="https://models.test",
        ), [], [])
    assert "SECRET" not in str(error.value)
