"""Trace a chat through the mounted portal, native SDK, real index and checkpoint DB."""

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
