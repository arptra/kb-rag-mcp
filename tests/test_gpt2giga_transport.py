"""Check the real gpt2giga conversion with isolated portal model profiles."""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("gpt2giga")

from gigachat.context import authorization_cvar, chat_url_cvar, custom_headers_cvar

from portal_agents.gpt2giga_model import _LOCAL_URL, _GigaChatBridge


def native_reply(message, reason):
    return httpx.Response(200, json={
        "object": "chat.completion", "model": "upstream", "created": 1,
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        "choices": [{"index": 0, "finish_reason": reason, "message": message}],
    })


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


@pytest.mark.parametrize("status", [400, 403, 429, 502])
async def test_upstream_errors_keep_status_without_private_provider_content(status):
    reply = httpx.Response(status, content=b"internal-model-secret",
                           headers={"X-Trace-Private": "token-detail"})
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
