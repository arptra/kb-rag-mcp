"""Trace a chat through the mounted portal, native SDK, real index and checkpoint DB."""

import asyncio
import json
import logging
import sqlite3
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from test_a2a import indexed
from test_portal_agents_engine import Model, call

from corporate_kb.catalog import RagCatalog
from corporate_kb.mcp.http_server import create_http_app
from portal_agents import http


async def test_chat_stream_real_retrieval_checkpoints_and_failed_turn_isolation(
    settings_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password, token = "test-admin-password-long", "test-read-token-of-sufficient-length"
    service, settings = indexed(
        settings_factory,
        agents_enabled=True,
        a2a_enabled=False,
        agents_model_base_url="https://model.example/v1",
        agents_model_name="test",
        mcp_http_bearer_token=token,
        admin_password=password,
    )
    app = create_http_app(service, settings)

    def decide(messages: list[BaseMessage], names: list[str]) -> AIMessage:
        recent = messages[-1]
        if isinstance(recent, HumanMessage):
            if recent.text == "Привет":
                return AIMessage(content="Привет!")
            if recent.text == "broken":
                return AIMessage(
                    content="PRIVATE INCOMPLETE",
                    response_metadata={
                        "finish_reason": "length",
                    },
                )
            if recent.text == "Repeat":
                assert all("PRIVATE INCOMPLETE" not in message.text for message in messages)
                assert any("500" in message.text for message in messages)
                return AIMessage(content="500")
            return call("list_indexes")
        assert isinstance(recent, ToolMessage)
        value = json.loads(recent.text)
        if recent.name == "list_indexes":
            return call("search_index", index_id=value[0]["id"], query="daily limit")
        if recent.name == "search_index":
            return call(
                "read_document",
                index_id=value["index_id"],
                document_id=value["results"][0]["document_id"],
            )
        assert recent.name == "read_document" and "500" in value["content"]
        return AIMessage(content="**500** from limits.md")

    @asynccontextmanager
    async def model(*args: Any, **kwargs: Any) -> AsyncIterator[Model]:
        yield Model(respond=decide)

    monkeypatch.setattr(http, "create_model", model)
    chat = str(uuid4())
    auth = {"Authorization": f"Bearer {token}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost"
        ) as client,
    ):

        async def send(question: str) -> list[dict[str, str]]:
            reply = await client.post(
                "/agents/api/chat", headers=auth, json={"chat": chat, "question": question}
            )
            reply.raise_for_status()
            return [json.loads(line) for line in reply.text.splitlines()]

        path = f"/agents/api/chats/{chat}"
        events = await send("Привет")
        assert [e["event"] for e in events] == ["start", "model", "answer"]
        events = await send("Verify our current daily limit.")
        assert [e["text"] for e in events if e["event"] == "tool_start"] == [
            "list_indexes",
            "search_index",
            "read_document",
        ]
        assert events[-1]["event"] == "answer" and "<strong>500</strong>" in events[-1]["html"]
        assert (await send("broken"))[-1]["event"] == "error"
        directory = RagCatalog.index_directory
        monkeypatch.setattr(
            RagCatalog, "index_directory", lambda self: [*directory(self), {"id": "new-index"}]
        )
        assert (await send("Repeat"))[-1]["text"] == "500"
        visible = (await client.get(path, headers=auth)).json()["messages"]
        assert len(visible) == 6 and "INCOMPLETE" not in str(visible)
        monkeypatch.setattr(RagCatalog, "index_directory", directory)
        assert len((await client.get(path, headers=auth)).json()["messages"]) == 4
        other = await client.get(path, headers={"X-KB-Admin-Password": password})
        assert other.json()["messages"] == []
        failed_chat = str(uuid4())
        first_attempt = await client.post(
            "/agents/api/chat", headers=auth, json={"chat": failed_chat, "question": "broken"}
        )
        assert json.loads(first_attempt.text.splitlines()[-1])["event"] == "error"
        for chat_id in (chat, failed_chat):
            response = await client.request(
                "DELETE", f"/agents/api/chats/{chat_id}", headers=auth, json={}
            )
            assert response.status_code == 204
        assert (await client.get(path, headers=auth)).json() == {"messages": []}
        with sqlite3.connect(settings.agents_state_dir / "checkpoints.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone() == (0,)


async def test_admin_probes_and_selects_corporate_api_for_live_chat(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from corporate_kb.access.dev_pki import prepare_local_identity

    password = "admin-test-password-long"
    reader = "shared-token-of-sufficient-length"
    service, settings = indexed(settings_factory, agents_enabled=True, admin_password=password,
                                mcp_http_bearer_token=reader, a2a_enabled=False)
    app = create_http_app(service, settings)
    pytest.importorskip("langchain_openai")
    identity = prepare_local_identity(tmp_path / "certificates")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(identity.server_cert, identity.server_key)
    tls.load_verify_locations(cafile=identity.ca_cert)
    tls.verify_mode = ssl.CERT_REQUIRED
    monkeypatch.setenv("OPENAI_API_KEY", "a-key-for-a-different-service")
    monkeypatch.setenv("OPENAI_ORG_ID", "private-org-of-a-different-service")
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="openai._base_client")
    requests: list[tuple[bytes, bytes, Any]] = []

    async def gateway(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next(v.partition(b":")[2] for v in headers.lower().splitlines()
                      if v.startswith(b"content-length:"))
        request = await reader.readexactly(int(length))
        requests.append((headers.lower(), request, writer.get_extra_info("peercert")))
        body = json.dumps({
            "id": "test", "object": "chat.completion", "model": "corporate", "created": 10,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "Hello!"}}],
        }).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " +
                     str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(gateway, "127.0.0.1", 0, ssl=tls) as server:
        port = server.sockets[0].getsockname()[1]
        draft = {
            "provider": "api", "adapter": "openai",
            "base_url": f"https://127.0.0.1:{port}/v1/chat/completions",
            "ca_pem": identity.ca_cert.read_text(), "cert_pem": identity.client_cert.read_text(),
            "key_pem": identity.client_key.read_text(),
        }
        admin = {"X-KB-Admin-Password": password}
        auth = {"Authorization": f"Bearer {reader}"}
        status = "/agents/api/model"
        config = status + "/settings"
        test = status + "/test"
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client,
        ):
            for headers in ({}, auth, {"X-Role": "admin"}):
                assert (await client.get(config, headers=headers)).status_code == 403
                assert (await client.put(config, headers=headers, json=draft)).status_code == 403
                assert (await client.post(test, headers=headers, json=draft)).status_code == 403
            origin = {**admin, "Origin": "https://another-origin.test"}
            assert (await client.put(config, headers=origin, json=draft)).status_code == 403
            assert not requests
            assert (await client.get(status, headers=auth)).json()["provider"] == "gigacode"
            probe = await client.post(test, headers=admin, json=draft)
            assert probe.json() == {"ok": True}
            assert (await client.get(config, headers=admin)).json()["provider"] == "gigacode"
            draft["model_name"] = "DeepSeek-V4-Flash"
            saved = await client.put(config, headers=admin, json=draft)
            saved.raise_for_status()
            assert "PRIVATE KEY" not in saved.text and "BEGIN CERTIFICATE" not in saved.text
            assert saved.json()["has_key"] and saved.json()["has_cert"] and saved.json()["has_ca"]
            assert (await client.get(status, headers=auth)).json() == {
                "model": "DeepSeek-V4-Flash", "state": "configured", "manager": False,
                "provider": "api", "adapter": "openai",
            }
            response = await client.post("/agents/api/chat", headers=auth, json={
                "chat": str(uuid4()), "question": "Hello",
            })
            assert json.loads(response.text.splitlines()[-1])["text"] == "Hello!"
            assert (await client.put(config, headers=admin, json={
                "provider": "gigacode",
            })).json()["has_key"]
            assert (await client.get(status, headers=auth)).json()["model"] == "GigaCode"
            restored = (await client.put(config, headers=admin, json={"provider": "api"})).json()
            assert restored["model_name"] == "DeepSeek-V4-Flash" and restored["has_cert"]

    assert len(requests) == 2
    probe_body, chat_body = (json.loads(body) for _, body, _ in requests)
    assert probe_body["messages"] == [{"role": "user", "content": "Reply with OK."}]
    assert "model" not in probe_body and probe_body["max_tokens"] == 128
    assert chat_body["model"] == "DeepSeek-V4-Flash"
    assert chat_body["max_tokens"] == 32768 and chat_body["thinking"] == {"type": "disabled"}
    assert chat_body["messages"][-1]["content"] == "Hello"
    for headers, _, certificate in requests:
        assert certificate and b"authorization:" not in headers
        assert b"openai-organization:" not in headers
        assert headers.startswith(b"post /v1/chat/completions ")
    assert draft["base_url"] not in caplog.text


async def test_model_settings_use_portal_administrator_session_and_csrf(
    settings_factory: Any, tmp_path: Path,
) -> None:
    from corporate_kb.access.dev_pki import prepare_local_identity

    identity = prepare_local_identity(tmp_path / "browser-pki")
    password = "test-admin-access-long-enough"
    service, settings = indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False, access_enabled=True,
        access_client_certificate_mode="trusted_ca", access_db_path=tmp_path / "access.sqlite",
        access_client_ca_file=identity.ca_cert, access_bootstrap_admin_password=password,
        mcp_tls_cert_file=identity.server_cert, mcp_tls_key_file=identity.server_key,
    )
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://localhost",
        ) as client,
    ):
        path = "/agents/api/model/settings"
        draft = {"provider": "api", "base_url": "https://model.example/v1", "model_name": "test"}
        spoof = await client.get(path, headers={"X-KB-Admin-Password": password})
        assert spoof.status_code == 403
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": password},
        )
        login.raise_for_status()
        csrf = {"X-CSRF-Token": login.json()["csrf_token"], "Origin": "https://localhost"}
        assert (await client.get(path)).status_code == 200
        assert (await client.put(path, json=draft)).status_code == 403
        assert (await client.put(path, json=draft, headers=csrf)).status_code == 200
