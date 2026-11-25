"""Check the real gpt2giga conversion with isolated portal model profiles."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

pytest.importorskip("gpt2giga")

from gigachat.context import authorization_cvar, chat_url_cvar, custom_headers_cvar
from langchain_openai import ChatOpenAI
from openai import UnprocessableEntityError

from portal_agents.gpt2giga_model import _LOCAL_URL, _GigaChatBridge
from portal_agents.model_errors import gateway_exception


def native_reply(message, reason):
    return httpx.Response(200, json={
        "object": "chat.completion", "model": "upstream", "created": 1,
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        "choices": [{"index": 0, "finish_reason": reason, "message": message}],
    })


async def test_corporate_v1_preserves_tool_constraints_when_adapting_optional_fields():
    nullable = {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
                "default": None, "description": "Maximum matches"}
    multiple = {"anyOf": [{"type": "integer"}, {"type": "string"}, {"type": "null"}],
                "default": None}
    nullable_choice = {"anyOf": [{"type": "array", "items": multiple}, {"type": "null"}],
                       "default": None}
    parameters = {
        "type": "object", "required": ["pattern", "required_nullable"],
        "properties": {
            "pattern": {"type": "string"}, "limit": nullable,
            "required_nullable": nullable, "mode": multiple, "choice": nullable_choice,
            "nested": {"type": "array", "items": {"type": "object",
                                                  "properties": {"limit": nullable}}},
            "with_default": {**nullable, "default": 3},
        },
    }
    definitions = []

    def serve(request):
        definitions.extend(json.loads(request.content)["functions"])
        return native_reply({"role": "assistant", "content": "done"}, "stop")

    async with (
        httpx.AsyncClient(base_url="https://corporate.test/v1",
                          transport=httpx.MockTransport(serve)) as native,
        httpx.AsyncClient(base_url=_LOCAL_URL,
                          transport=_GigaChatBridge(native, "DeepSeek-V4-Flash", None)) as client,
    ):
        reply = await client.post("chat/completions", json={
            "messages": [{"role": "user", "content": "Look up an index"}],
            "tools": [{"type": "function",
                       "function": {"name": "lookup", "parameters": parameters}}],
        })
        reply.raise_for_status()

    received = definitions[0]["parameters"]
    assert received["required"] == parameters["required"]
    expected = {"type": "integer", "minimum": 1, "description": "Maximum matches"}
    assert received["properties"]["limit"] == expected
    assert received["properties"]["nested"]["items"]["properties"]["limit"] == expected
    for name in ("pattern", "required_nullable", "mode", "choice", "with_default"):
        assert received["properties"][name] == parameters["properties"][name]


async def test_bridge_preserves_tool_round_trip_and_profile_boundaries(monkeypatch):
    monkeypatch.setenv("GIGACHAT_ACCESS_TOKEN", "unrelated")
    monkeypatch.setenv("GIGACHAT_MODEL", "unrelated")
    monkeypatch.setenv("GPT2GIGA_LOG_LEVEL", "DEBUG")
    context = [
        (chat_url_cvar, chat_url_cvar.set("https://other.test/should-not-contact")),
        (authorization_cvar, authorization_cvar.set("unrelated")),
        (custom_headers_cvar, custom_headers_cvar.set({"X-Private": "unrelated"})),
    ]

    async def exchange(name, token):
        seen = []

        def serve(req):
            assert req.url == f"https://{name}.test/private/v1/chat/completions"
            assert "X-Private" not in req.headers
            assert req.headers.get("Authorization") == ("Bearer " + token if token else None)
            value = json.loads(req.content)
            seen.append(value)
            assert "tools" not in value and value["functions"][0]["name"] == "lookup"
            if token:
                assert value["model"] == "custom-name"
            else:
                assert "model" not in value
            if value["messages"][-1]["role"] == "user":
                return native_reply({"role": "assistant", "content": "",
                                     "functions_state_id": "fc_" + name, "function_call": {
                    "name": "lookup", "arguments": {"index": name},
                }}, "function_call")
            assistant = value["messages"][-2]
            assert assistant.get("functions_state_id") == "fc_" + name
            assert value["messages"][-1]["role"] == "function"
            assert value["messages"][-1]["name"] == "lookup"
            assert name in value["messages"][-1]["content"]
            return native_reply({"role": "assistant", "content": "done"}, "stop")

        async with httpx.AsyncClient(base_url=f"https://{name}.test/private/v1",
                                     transport=httpx.MockTransport(serve)) as native:
            bridge = _GigaChatBridge(native, "custom-name" if token else None, token)
            async with httpx.AsyncClient(base_url=_LOCAL_URL, transport=bridge) as client:
                body = {"messages": [{"role": "user", "content": "Question?"}], "model": "ignored",
                        "tools": [{"type": "function", "function": {
                            "name": "lookup", "description": "Read index",
                            "parameters": {"type": "object", "properties": {
                                "index": {"type": "string"},
                            }},
                        }}]}
                first = (await client.post("chat/completions", json=body)).raise_for_status().json()
                choice = first["choices"][0]
                assert choice["finish_reason"] == "tool_calls"
                msg = choice["message"]
                call = msg["tool_calls"][0]
                assert json.loads(call["function"]["arguments"]) == {"index": name}
                # LangChain checkpoints retain id/name/args but strip extra tool-call fields.
                msg["tool_calls"][0].pop("tools_state_id", None)
                body["messages"].extend([msg, {"role": "tool", "tool_call_id": call["id"],
                                               "content": json.dumps({"source": name})}])
                final = (await client.post("chat/completions", json=body)).raise_for_status().json()
                assert final["choices"][0]["message"]["content"] == "done"
                assert len(seen) == 2

    try:
        await asyncio.gather(exchange("alpha", None), exchange("beta", "beta-token"))
    finally:
        for var, handle in context:
            var.reset(handle)


@pytest.mark.parametrize("status", [400, 403, 429, 500, 502, 503])
async def test_upstream_errors_keep_status_without_private_provider_content(status):
    request_id = str(uuid4())
    reply = httpx.Response(status, content=b"internal-model-secret",
                           headers={"X-Trace-Private": "token-detail", "X-Request-ID": request_id,
                                    "Retry-After": "7", "X-Should-Retry": "false",
                                    "X-Portal-Model-Error": "fake"})
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None),
                          base_url=_LOCAL_URL) as client,
    ):
        response = await client.post("chat/completions", json={
            "messages": [{"role": "user", "content": "Quick test"}],
        })
    assert response.status_code == status
    assert "internal-model-secret" not in response.text
    assert "X-Trace-Private" not in response.headers
    assert "X-Portal-Model-Error" not in response.headers
    assert response.headers["Retry-After"] == "7"
    assert response.headers["X-Should-Retry"] == "false"
    assert response.headers["X-Request-ID"] == request_id


@pytest.mark.parametrize(("payload", "field", "category", "blocked"), [
    ({"detail": [{"loc": ["body", "functions", 0, "parameters", "PRIVATE"],
                  "input": "PRIVATE", "msg": "PRIVATE"}]}, "tools", "upstream-http", False),
    ({"error": {"param": "messages[1].function_call", "message": "PRIVATE"}},
     "messages", "upstream-http", False),
    ({"error": {"param": "functions", "code": "content_filter", "message": "PRIVATE"}},
     "tools", "policy", True),
    ({"message": "messages Tool X failed PRIVATE"}, None, "upstream-http", False),
])
async def test_native_validation_through_both_sdks_is_sanitized(payload, field, category, blocked):
    upstream = httpx.Response(422, json=payload,
                              headers={"X-Portal-Model-Error": "invalid-request"})
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: upstream),
                          base_url="https://private.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None)) as client,
    ):
        model = ChatOpenAI(model="model", base_url=_LOCAL_URL, api_key="local",
                           http_async_client=client, use_responses_api=False, max_retries=0)
        with pytest.raises(UnprocessableEntityError) as raised:
            await model.ainvoke("Hello")
    error = gateway_exception(raised.value)
    assert (error.status, error.category, error.validation_field) == (422, category, field)
    assert error.diagnostics["wire"] == "gigachat-v1"
    assert error.diagnostics["endpoint"] == {"kind": "chat/completions",
                                             "chat_completions_count": 1}
    assert error.diagnostics["request"]["roles"] == {"user": 1}
    assert error.diagnostics["request"]["text_chars"] == 5
    assert "PRIVATE" not in json.dumps(error.diagnostics)
    assert not error.retryable and error.retry_forbidden is blocked
    assert "PRIVATE" not in raised.value.response.text and "PRIVATE" not in str(raised.value)
    assert "private.invalid" not in str(error)


async def test_local_native_request_validation_reports_the_field_without_sending_data(monkeypatch):
    async def invalid(_):
        return {"messages": "PRIVATE"}

    def unexpected(_):
        pytest.fail("local validation should finish before any native call")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as native:
        bridge = _GigaChatBridge(native, "model", None)
        monkeypatch.setattr(bridge.encoder, "prepare_chat", invalid)
        async with httpx.AsyncClient(transport=bridge) as client:
            model = ChatOpenAI(model="model", base_url=_LOCAL_URL, api_key="local",
                               http_async_client=client, use_responses_api=False, max_retries=0)
            with pytest.raises(UnprocessableEntityError) as raised:
                await model.ainvoke("Hello")
    error = gateway_exception(raised.value)
    assert (error.status, error.category) == (422, "invalid-request")
    assert error.diagnostics["wire"] == "adapter-input" and "endpoint" not in error.diagnostics
    assert error.diagnostics["request"]["messages"] == "invalid"
    assert error.diagnostics["validation"]["issues"] == [{"type": "list_type",
                                                         "path": ["messages"]}]
    assert "PRIVATE" not in json.dumps(error.diagnostics)
    assert error.validation_field == "messages"
    assert "Адаптер" in str(error) and "messages" in str(error) and not error.retryable
    assert "PRIVATE" not in raised.value.response.text and "PRIVATE" not in str(raised.value)


@pytest.mark.parametrize("delay", ["Fri, 12 Dec 2031 10:00:00 GMT", "secret"])
async def test_bridge_forwards_http_date_and_strips_untyped_diagnostics(delay):
    reply = httpx.Response(503, headers={"Retry-After": delay, "X-Request-ID": "secret"})
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None),
                          base_url=_LOCAL_URL) as client,
    ):
        response = await client.post("chat/completions", json={
            "messages": [{"role": "user", "content": "Quick test"}],
        })
    assert response.headers.get("Retry-After") == (None if delay == "secret" else delay)
    assert "X-Request-ID" not in response.headers


@pytest.mark.parametrize("body", [b"<html>secret", b'["secret"]', b"{}"])
async def test_bridge_identifies_malformed_replies_without_misreporting_network_failure(body):
    reply = httpx.Response(200, content=body)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None),
                          base_url=_LOCAL_URL) as client,
    ):
        response = await client.post("chat/completions", json={
            "messages": [{"role": "user", "content": "Quick test"}],
        })
    assert response.status_code == 502
    assert response.headers["X-Portal-Model-Error"] == "invalid-reply"
    assert "secret" not in response.text


async def test_native_http_200_rejection_is_not_retried_as_malformed_reply():
    reply = httpx.Response(200, json={"error": {"type": "invalid_request_error",
                                                "message": "private details"}})
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None)) as client,
    ):
        model = ChatOpenAI(model="model", base_url=_LOCAL_URL, api_key="local",
                           http_async_client=client, use_responses_api=False, max_retries=0)
        with pytest.raises(ValueError) as raised:
            await model.ainvoke("Hello")
    failure = gateway_exception(raised.value)
    assert failure is not None and not failure.retryable
    assert "private details" not in str(raised.value)


async def test_parallel_history_from_previous_model_preserves_both_tool_results():
    seen = []

    def serve(request):
        seen.append(json.loads(request.content)["messages"])
        return native_reply({"role": "assistant", "content": "done"}, "stop")

    calls = [{"type": "function", "id": "call_" + name,
              "function": {"name": name, "arguments": "{}"}} for name in ("source_a", "source_b")]
    messages = [
        {"role": "user", "content": "Compare sources"},
        {"role": "assistant", "content": "Comparing", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_source_b", "content": '{"evidence": "B"}'},
        {"role": "tool", "tool_call_id": "call_source_a", "content": '{"evidence": "A"}'},
        {"role": "assistant", "content": "Both found"},
        {"role": "user", "content": "Which was published later?"},
    ]
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(serve),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None),
                          base_url=_LOCAL_URL) as client,
    ):
        response = await client.post("chat/completions", json={"messages": messages})
        response.raise_for_status()
        # Missing results cannot be reconstructed or replayed by a protocol converter.
        del messages[3]
        invalid = await client.post("chat/completions", json={"messages": messages})
        assert invalid.status_code == 422
        assert invalid.json()["error"]["param"] == "messages"
    assert len(seen) == 1
    user, ask_a, result_a, ask_b, result_b, answer, followup = seen[0]
    assert user["content"] == "Compare sources" and followup["content"].startswith("Which")
    assert ask_a["content"] == "Comparing" and ask_b["content"] == ""
    for call, result, name, evidence in ((ask_a, result_a, "source_a", "A"),
                                         (ask_b, result_b, "source_b", "B")):
        assert call["function_call"]["name"] == result["name"] == name
        assert result["role"] == "function"
        assert json.loads(result["content"]) == {"evidence": evidence}
    assert answer["content"] == "Both found"


async def test_422_uses_sanitized_native_wire_shape_and_provider_validation_path():
    req_id = str(uuid4())
    payload = {"detail": [{"loc": ["body", "functions", 0, "parameters", "properties",
                                   "SECRET_FIELD", "type"],
                           "type": "extra_forbidden", "msg": "SECRET_MSG", "input": "SECRET_ARG"}]}
    observed = []

    def reject(req):
        observed.append(json.loads(req.content))
        return httpx.Response(422, json=payload, headers={"x-request-id": req_id})

    async with (
        httpx.AsyncClient(base_url="https://SECRET.test/private/chat/completions",
                          transport=httpx.MockTransport(reject)) as native,
        httpx.AsyncClient(base_url=_LOCAL_URL,
                          transport=_GigaChatBridge(native, "SECRET_MODEL", None)) as client,
    ):
        body = {"tools": [{"type": "function", "function": {"name": "SECRET_FN",
                 "parameters": {"type": "object", "properties": {}}}}], "max_tokens": 120,
                "messages": [{"role": "user", "content": "SECRET_QUESTION"},
                             {"role": "assistant", "content": "", "tool_calls": [{
                                 "id": "SECRET_ID", "type": "function",
                                 "function": {"name": "SECRET_FN", "arguments": "{}"}}]},
                             {"role": "tool", "tool_call_id": "SECRET_ID",
                              "content": "SECRET_OUT"}]}
        response = await client.post("chat/completions", json=body)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            response.raise_for_status()
    error = gateway_exception(caught.value)
    [sent] = observed
    assert len(sent["functions"]) == 1 and "tools" not in sent
    shape = error.diagnostics["request"]
    assert (shape["tools"], shape["functions"]) == (0, 1)
    assert (shape["model"], shape["max_tokens"]) == ("set", 120)
    assert shape["roles"] == {"user": 1, "assistant": 1, "function": 1}
    assert shape["tool_calls"] == shape["tool_results"] == 1
    assert error.diagnostics["endpoint"] == {"kind": "chat/completions",
                                             "chat_completions_count": 2}
    assert error.diagnostics["validation"]["issues"] == [{
        "type": "extra_forbidden",
        "path": ["body", "functions", 0, "parameters", "properties", "*", "type"],
    }]
    assert error.request_id == req_id
    assert "secret" not in json.dumps(error.diagnostics).lower()


async def test_native_blacklist_reaches_sdk_as_content_filter():
    reply = native_reply({"role": "assistant", "content": "restricted"}, "blacklist")
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: reply),
                          base_url="https://test.invalid/v1") as native,
        httpx.AsyncClient(transport=_GigaChatBridge(native, "model", None),
                          base_url=_LOCAL_URL) as client,
    ):
        response = await client.post("chat/completions", json={
            "messages": [{"role": "user", "content": "Quick test"}],
        })
    assert response.json()["choices"][0]["finish_reason"] == "content_filter"
