"""A rejected model request must not become a completed answer or a successful probe."""
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from langchain_core.exceptions import ModelAPIError
from langchain_openai import ChatOpenAI
from test_a2a import indexed

from corporate_kb.agents.activation import GigaCodeDecisionError, GigaCodeRecoveryPending
from corporate_kb.gigacode_runner import (
    GigaCodeAccessError,
    GigaCodeProcessCrashed,
    GigaCodeRequestFailed,
    GigaCodeTransientError,
)
from corporate_kb.mcp.http_server import create_http_app
from portal_agents import http
from portal_agents.model_errors import check_gateway_reply, gateway_exception
from portal_agents.wire_diagnostics import validation_details


@pytest.mark.parametrize("form", ["http", "envelope", "text", "bare", "text-envelope"])
async def test_rejected_chat_and_probe_show_safe_error_and_do_not_publish_transcript(
    settings_factory, monkeypatch, form, caplog,
):
    password = "test-admin-password-local"
    token = "chat-reader-of-sufficient-length"
    service, settings = indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        agents_model_base_url="https://platform.example/v1", agents_model_name="test",
        admin_password=password, mcp_http_bearer_token=token,
    )
    secret = "PRIVATE_UPSTREAM_BODY"
    payload = {"error": {"type": "invalid_request_error", "message": secret}}
    calls = []

    def gateway(request):
        calls.append(request.url.path)
        if form in {"http", "envelope"}:
            return httpx.Response(400 if form == "http" else 200, json=payload)
        text = {"text": "OpenAIInvalidRequestError: HTTP 400 " + secret,
                "bare": "openAIInvalidRequestError", "text-envelope": json.dumps(payload)}[form]
        return httpx.Response(200, json={
            "id": "test", "created": 1, "model": "test", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
        })

    @asynccontextmanager
    async def model(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
            yield ChatOpenAI(model="test", base_url="https://platform.example/v1",
                             api_key="dummy", http_async_client=client, use_responses_api=False)

    monkeypatch.setattr(http, "create_model", model)
    app = create_http_app(service, settings)
    chat_id = str(uuid4())
    reader = {"Authorization": f"Bearer {token}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://portal") as client,
    ):
        turn = await client.post("/agents/api/chat", headers=reader,
                                 json={"chat": chat_id, "question": "Look up the release"})
        events = [json.loads(line) for line in turn.text.splitlines()]
        assert [item["event"] for item in events] == ["start", "model", "error"]
        assert "Шлюз модели" not in turn.text
        assert events[-1]["reference"] in caplog.text
        if form == "http":
            assert "HTTP 400" in events[-1]["text"]
        assert secret not in turn.text and "InvalidRequestError" not in turn.text
        history = await client.get("/agents/api/chats/" + chat_id, headers=reader)
        assert history.json()["messages"] == []
        probe = await client.post(
            "/agents/api/model/test", headers={"X-KB-Admin-Password": password},
            json={"provider": "api"},
        )
        assert probe.status_code == 502 and "Номер ошибки" in probe.text
        assert secret not in probe.text
    assert calls == ["/v1/chat/completions"] * 2
    assert secret not in caplog.text
    assert "on_llm_error callback" not in caplog.text


def test_answer_explaining_error_name_is_preserved():
    check_gateway_reply("Ошибка OpenAIInvalidRequestError возникает при несовместимой схеме.")
    check_gateway_reply('{"example": {"type": "invalid_request_error"}}')


@pytest.mark.parametrize(("payload", "field"), [
    ({"error": {"param": "messages[3].tool_calls.0.function.arguments"}}, "messages"),
    ({"detail": [{"loc": ["body", "functions", 1, "name"], "input": "private_key"},
                 {"loc": ["body", "functions", 2, "parameters", "private_key"]}]}, "tools"),
    ({"error": {"param": "model"}}, "model"),
    ({"detail": [{"loc": ["body", "max_completion_tokens"]}]}, "max_tokens"),
    ({"detail": [{"loc": ["header", "private_key", "functions"]}]}, None),
    ({"detail": [{"loc": ["body", "tools", {"private": "value"}]}]}, None),
    ({"detail": [{"loc": ["body", "messages"]}, {"loc": ["body", "private_key"]}]}, None),
    ({"detail": [{"loc": ["body", "tools"]}, {"loc": ["body", "max_tokens"]}]}, None),
    ({"error": {"param": "tools/private_key"}}, None),
    ({"error": {"param": "private_key.tools"}}, None),
    ({"error": {"message": "tools validation failed private_key"}}, None),
])
def test_validation_location_includes_only_the_known_common_request_field(payload, field):
    response = httpx.Response(
        422, json=payload, headers={"X-Portal-Model-Error": "invalid-request"},
        request=httpx.Request("POST", "https://private.invalid"),
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    error = gateway_exception(raised.value)
    assert error.validation_field == field
    assert error.category == "upstream-http" and not error.retryable and not error.retry_forbidden
    assert "API модели" in str(error) and "422" in str(error)
    if field:
        assert field in str(error)
    assert "private" not in str(error) and "сертификат" not in str(error)


@pytest.mark.parametrize(("body", "hint"), [
    (b"Field required: SECRET model name", "field_required"),
    (b'{"message":"Extra inputs are not permitted: SECRET"}', "extra_forbidden"),
    (b'{"error":{"message":"This model\'s maximum context length is 12345 SECRET"}}',
     "context_length"),
    (b'{"detail":"Unsupported parameter SECRET"}', "unsupported_parameter"),
    (b'{"input":"Field required","msg":"SECRET"}', "unknown"),
])
def test_free_text_yields_safe_hint_without_changing_recovery_decisions(body, hint):
    details = validation_details(body)
    assert details["message_hint"] == hint and "SECRET" not in json.dumps(details)
    response = httpx.Response(422, content=body, request=httpx.Request("POST", "https://test/v1"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    error = gateway_exception(raised.value)
    assert error.diagnostics["validation"] == details
    assert error.validation_field is None and not error.retryable


@pytest.mark.parametrize(("payload", "headers", "category"), [
    ({"error": {"param": "tools", "code": "invalid_api_key"}}, {}, "access"),
    ({"error": {"param": "tools", "innererror": {"code": "ResponsibleAIPolicyViolation"}}},
     {}, "policy"),
    ({"error": {"param": "tools", "refusal": "SECRET"}}, {}, "policy"),
    ({"error": {"param": "tools"}}, {"X-Should-Retry": "False"}, "upstream-http"),
])
@pytest.mark.parametrize("status", [422, 500])
def test_provider_no_retry_is_respected_across_compatibility_strategies(
    payload, headers, category, status,
):
    response = httpx.Response(status, json=payload, headers=headers,
                              request=httpx.Request("POST", "https://private.invalid"))
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    error = gateway_exception(raised.value)
    assert (error.category, error.retryable, error.retry_forbidden) == (category, False, True)
    assert "private" not in str(error) and "SECRET" not in str(error)


@pytest.mark.parametrize(("body", "kind", "count"), [
    (b"", "empty", 0), (b"SECRET_BODY", "text", 0), (b'"SECRET"', "string", 0),
    (b'{"detail":"SECRET"}', "detail_string", 0), (b"{" * 70000, "oversized", 0),
    (json.dumps({"detail": [{"loc": ["body", "functions", 0, "parameters", "properties",
                                     "SECRET_PROPERTY", "name"],
                             "type": "SECRET_TYPE", "msg": "SECRET_MSG", "input": "SECRET_INPUT"}]
                            * 7}).encode(), "detail_array", 7),
])
def test_direct_422_wire_shape_and_opaque_responses_are_safe(body, kind, count):
    request = httpx.Request("POST", "https://SECRET.invalid/v1/private/chat/completions",
                            json={"model": "SECRET_MODEL", "messages": [
                                {"role": "SECRET_ROLE", "content": "SECRET_TEXT"},
                                {"role": "assistant", "tools_state_id": "SECRET_STATE",
                                 "tool_calls": [{"id": "SECRET_ID"}]},
                                {"role": "tool", "content": [{"text": "SECRET_RESULT"}]}],
                                "tools": [{"name": "SECRET_TOOL"}], "stream": False,
                                "thinking": {"type": "SECRET_CONFIG"}, "max_tokens": "SECRET"})
    response = httpx.Response(422, content=body, request=request,
                              headers={"X-Request-ID": "SECRET_ID"},
                              extensions={"portal_wire_diagnostics": {"SECRET": "injected"}})
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    failure = gateway_exception(raised.value)
    assert failure.request_id is None
    diagnostics = failure.diagnostics
    assert diagnostics["wire"] == "openai" and diagnostics["endpoint"]["kind"] == "chat/completions"
    assert diagnostics["request"] == {
        "body_type": "object", "body_bytes": len(request.content), "model": "set",
        "tools": 1, "functions": 0, "stream": False, "thinking_present": True,
        "function_choice_present": False, "max_tokens": "invalid", "messages": 3,
        "roles": {"other": 1, "assistant": 1, "tool": 1}, "tool_calls": 1,
        "tool_results": 1, "state_ids": 1, "text_chars": len("SECRET_TEXTSECRET_RESULT"),
    }
    validation = diagnostics["validation"]
    assert (validation["body_type"], validation["error_count"]) == (kind, count)
    assert len(validation["issues"]) == min(5, count)
    if count:
        assert validation["issues"][0] == {
            "type": "other",
            "path": ["body", "functions", 0, "parameters", "properties", "*", "name"],
        }
    assert "secret" not in json.dumps(diagnostics).lower()


@pytest.mark.parametrize(("exc", "category", "status", "retryable"), [
    (ModelAPIError("PRIVATE"), "provider", None, True),
    (GigaCodeTransientError(reason="rate-limit"), "rate-limit", None, True),
    (GigaCodeTransientError(status=429, reason="server-error"), "rate-limit", 429, True),
    (GigaCodeAccessError(status=403), "access", 403, False),
    (GigaCodeProcessCrashed(-9), "process-crash", None, True),
    (GigaCodeRequestFailed(), "runner-request", None, False),
    (GigaCodeRecoveryPending("PRIVATE"), "restart-pending", None, False),
    (GigaCodeDecisionError("PRIVATE"), "incomplete-reply", None, False),
])
def test_model_failure_reports_only_the_provider_evidence(exc, category, status, retryable):
    failure = gateway_exception(exc)
    assert (failure.category, failure.status, failure.retryable) == (category, status, retryable)
    assert "недоступен" not in str(failure) and "PRIVATE" not in str(failure)


async def test_unexpected_chat_failure_has_a_reference_and_safe_origin(
    settings_factory, monkeypatch, caplog,
):
    token = "runtime-error-test-token-of-sufficient-length"
    app = create_http_app(*indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        agents_model_base_url="https://model.test/v1", agents_model_adapter="openai",
        mcp_http_bearer_token=token,
    ))

    async def interrupted(*args, **kwargs):
        raise RuntimeError("PRIVATE_EXCEPTION_TEXT")

    monkeypatch.setattr(http, "run_turn", interrupted)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://portal") as client,
    ):
        response = await client.post(
            "/agents/api/chat", headers={"Authorization": f"Bearer {token}"},
            json={"chat": str(uuid4()), "question": "PRIVATE_QUESTION"},
        )
    response.raise_for_status()
    last = json.loads(response.text.splitlines()[-1])
    assert last["event"] == "error"
    assert f"agent_turn_stopped ref={last['reference']}" in caplog.text
    assert "stage=research exception=RuntimeError" in caplog.text
    assert "(interrupted)" in caplog.text
    assert "PRIVATE_" not in response.text + caplog.text


async def test_persistent_model_outage_is_a_status_with_traceable_reference(
    settings_factory, monkeypatch, caplog,
):
    token = "persistent-outage-test-token-of-sufficient-length"
    service, settings = indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        agents_model_base_url="https://model.test/v1", agents_model_adapter="openai",
        mcp_http_bearer_token=token,
    )
    calls = 0

    def offline(request):
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": {"message": "PRIVATE_FAILURE"}})

    @asynccontextmanager
    async def model(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(offline)) as upstream:
            yield ChatOpenAI(model="test", api_key="test", base_url="https://model.test/v1",
                             http_async_client=upstream, max_retries=0, use_responses_api=False)

    monkeypatch.setattr(http, "create_model", model)
    app = create_http_app(service, settings)
    chat_id = str(uuid4())
    reader = {"Authorization": f"Bearer {token}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://portal") as client,
    ):
        turn = await client.post("/agents/api/chat", headers=reader,
                                 json={"chat": chat_id, "question": "Hello"})
        events = [json.loads(line) for line in turn.text.splitlines()]
        final = events[-1]
        assert final["event"] == "error" and "Шлюз" not in final["text"]
        assert "500" not in final["text"] and "PRIVATE_FAILURE" not in turn.text
        assert [event["event"] for event in events].count("model_retry") == calls - 1
        assert calls == 4
        assert final["reference"] in caplog.text and "status=500 attempts=4" in caplog.text
        assert "stage=native tools=0" in caplog.text
        assert "on_llm_error callback" not in caplog.text
        assert "PRIVATE_FAILURE" not in caplog.text
        assert (await client.get("/agents/api/chats/" + chat_id, headers=reader)).json() == {
            "messages": [],
        }
