"""Exercise the real gpt2giga converters through model settings, mTLS, and the agent loop."""
import asyncio
import json
import ssl
from contextlib import suppress
from uuid import uuid4

import httpx
import pytest
from test_a2a import indexed

from corporate_kb.access.dev_pki import prepare_local_identity
from corporate_kb.mcp.http_server import create_http_app


@pytest.mark.parametrize("model_name", ["DeepSeek-V4-Flash", "internal-model"])
async def test_native_model_probe_and_tool_round_trip(settings_factory, tmp_path, monkeypatch,
                                                      model_name):
    password, reader = "test-admin-password", "test-user-token-for-chat-integration"
    service, settings = indexed(settings_factory, agents_enabled=True, admin_password=password,
                                mcp_http_bearer_token=reader, a2a_enabled=False)
    app = create_http_app(service, settings)
    identity = prepare_local_identity(tmp_path / "pki")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(identity.server_cert, identity.server_key)
    tls.load_verify_locations(cafile=identity.ca_cert)
    tls.verify_mode = ssl.CERT_REQUIRED
    monkeypatch.setenv("OPENAI_API_KEY", "unused-token-from-another-provider")
    requests = []
    state_id = str(uuid4())
    answer = "Доступен индекс default."
    question = "Какие индексы доступны?"
    paraphrase = "Перечисли индексы, к которым есть доступ."
    followup_question = "Напомни результат предыдущей проверки."

    async def native_api(stream, writer):
        headers = await stream.readuntil(b"\r\n\r\n")
        length = next(line.split(b":", 1)[1] for line in headers.lower().splitlines()
                      if line.startswith(b"content-length:"))
        payload = json.loads(await stream.readexactly(int(length)))
        requests.append((headers.lower(), payload, writer.get_extra_info("peercert")))
        messages = payload["messages"]
        # This deployment fails on the solving call until it receives a paraphrase.
        if (model_name == "DeepSeek-V4-Flash" and payload.get("functions")
                and any(m["role"] == "function" for m in messages)
                and not any(followup_question == m.get("content") for m in messages)
                and not any(paraphrase in (m.get("content") or "") for m in messages)):
            writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n"
                         b"Connection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
            return
        msg = {"role": "assistant", "content": "OK"}
        reason = "stop"
        if len(requests) == 2:
            msg = {**msg, "content": "", "functions_state_id": state_id,
                   "function_call": {"name": "list_indexes", "arguments": {}}}
            reason = "function_call"
        elif len(requests) > 2 and not payload.get("functions"):
            msg["content"] = paraphrase
        elif len(requests) > 2:
            msg["content"] = answer
        response = json.dumps({
            "object": "chat.completion", "created": 42, "model": model_name,
            "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
            "choices": [{"index": 0, "message": msg, "finish_reason": reason}],
        }).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                     + str(len(response)).encode() + b"\r\nConnection: close\r\n\r\n" + response)
        await writer.drain()
        writer.close()
        with suppress(ConnectionResetError):
            await writer.wait_closed()

    async with (
        await asyncio.start_server(native_api, "127.0.0.1", 0, ssl=tls) as server,
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://portal") as client,
    ):
        port = server.sockets[0].getsockname()[1]
        config = {
            "provider": "api",
            "base_url": f"https://127.0.0.1:{port}/company/api/v1",
            "model_name": model_name, "ca_pem": identity.ca_cert.read_text(),
            "cert_pem": identity.client_cert.read_text(),
            "key_pem": identity.client_key.read_text(),
        }
        admin = {"X-KB-Admin-Password": password}
        probe = await client.post("/agents/api/model/test", headers=admin, json=config)
        assert probe.is_success, probe.text
        saved = await client.put("/agents/api/model/settings", headers=admin, json=config)
        assert saved.is_success and saved.json()["adapter"] == "gpt2giga"
        chat_id = str(uuid4())
        user = {"Authorization": f"Bearer {reader}"}
        reply = await client.post(
            "/agents/api/chat", headers=user,
            json={"chat": chat_id, "question": question},
        )
        events = [json.loads(line) for line in reply.text.splitlines()]
        assert events[-1]["event"] == "answer" and events[-1]["text"] == answer, events
        assert [e["text"] for e in events if e["event"] == "tool_start"] == ["list_indexes"]
        retries = [e for e in events if e["event"] == "model_retry"]
        assert len(retries) == (3 if model_name == "DeepSeek-V4-Flash" else 0)
        if retries:
            assert any("переформул" in retry["text"].casefold() for retry in retries)
            assert all(paraphrase not in event["text"] for event in events)
        history = (await client.get("/agents/api/chats/" + chat_id, headers=user)).json()
        assert [(m["role"], m["text"]) for m in history["messages"]] == [
            ("user", question), ("assistant", answer),
        ]
        followup = await client.post("/agents/api/chat", headers=user, json={
            "chat": chat_id, "question": followup_question,
        })
        assert json.loads(followup.text.splitlines()[-1])["event"] == "answer"

    assert len(requests) == (7 if model_name == "DeepSeek-V4-Flash" else 4)
    for headers, body, certificate in requests:
        assert certificate and b"authorization:" not in headers
        assert headers.startswith(b"post /company/api/v1/chat/completions ")
        assert body["model"] == model_name and "tools" not in body
        assert "thinking" not in body
    if model_name == "DeepSeek-V4-Flash":
        assert requests[1][1]["max_tokens"] == 32768
        original = requests[2][1]["messages"]
        assert requests[3][1]["messages"] == original
        rewrite = requests[4][1]
        assert not rewrite.get("functions")
        assert rewrite["max_tokens"] == 256
        assert [m["role"] for m in rewrite["messages"]] == ["system", "user"]
        assert question in rewrite["messages"][-1]["content"]
        assert all(state_id not in m["content"] for m in rewrite["messages"])
        solving = requests[5][1]
        assert solving["max_tokens"] == 32768
        assert solving["functions"] == requests[2][1]["functions"]
        assert any(question in m["content"] for m in solving["messages"] if m["role"] == "user")
        assert any(paraphrase in (m.get("content") or "") for m in solving["messages"]
                   if m["role"] == "assistant")
        later = requests[-1][1]["messages"]
        assert all(paraphrase not in (m.get("content") or "") for m in later)
        assert [m["content"] for m in later if m["role"] == "user"] == [question, followup_question]
    assert "list_indexes" in {f["name"] for f in requests[1][1]["functions"]}
    for _, body, _ in requests[2:]:
        if not body.get("functions"):
            continue  # The tool-free rewrite sees only the user's question.
        messages = body["messages"]
        assert any(m.get("functions_state_id") == state_id for m in messages)
        tool_result = next(m for m in messages if m["role"] == "function")
        assert tool_result["name"] == "list_indexes" and "default" in tool_result["content"]
