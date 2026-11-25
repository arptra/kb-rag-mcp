"""A rejected model request must not become a completed answer or a successful probe."""
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from langchain_openai import ChatOpenAI
from test_a2a import indexed

from corporate_kb.mcp.http_server import create_http_app
from portal_agents import http
from portal_agents.model_errors import check_gateway_reply


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


def test_answer_explaining_error_name_is_preserved():
    check_gateway_reply("Ошибка OpenAIInvalidRequestError возникает при несовместимой схеме.")
    check_gateway_reply('{"example": {"type": "invalid_request_error"}}')


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
        assert "PRIVATE_FAILURE" not in caplog.text
        assert (await client.get("/agents/api/chats/" + chat_id, headers=reader)).json() == {
            "messages": [],
        }
