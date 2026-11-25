from __future__ import annotations

import asyncio
import json
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import Field

pytest.importorskip("deepagents")

from portal_agents.engine import run_turn
from portal_agents.model import create_model


class Model(BaseChatModel):
    respond: Callable[[list[BaseMessage], list[str]], AIMessage] = Field(exclude=True)

    @property
    def _llm_type(self) -> str:
        return "test-sdk-model"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self.bind(names=[t.name if isinstance(t, BaseTool)
                                else t["function"]["name"] for t in tools])

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(
            message=self.respond(messages, kwargs.get("names", []))
        )])

    async def _agenerate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        return self._generate(messages, **kwargs)


def call(name: str, **args: Any) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": "call_" + name, "name": name, "args": args}])


async def test_native_agent_follows_live_observations_and_reports_only_real_actions() -> None:
    events: list[tuple[str, str]] = []
    queries: list[str] = []

    @tool
    async def search_index(query: str) -> str:
        """Find the relevant record ID."""
        queries.append(query)
        return "PAY-00071"

    @tool
    async def read_document(record: str) -> str:
        """Read a matching record."""
        assert record == "PAY-00071"
        return "invoice accepted"

    def decide(messages: list[BaseMessage], names: list[str]) -> AIMessage:
        if isinstance(messages[-1], HumanMessage) and messages[-1].content == "Hello":
            assert names == []
            return AIMessage(content="Hello!")
        assert {"write_todos", "task", "search_index", "read_document", "read_file"} <= set(names)
        assert "execute" not in names
        last = messages[-1]
        if isinstance(last, HumanMessage):
            return call("search_index", query="invoices")
        assert isinstance(last, ToolMessage)
        if last.name == "search_index":
            return call("read_document", record=last.content)
        return AIMessage(content=f"PAY-00071: {last.content}")

    async def progress(kind: str, name: str) -> None:
        events.append((kind, name))

    model = Model(respond=decide)
    tools = [search_index, read_document]
    assert await run_turn(model, "Hello", tools, on_event=progress) == "Hello!"
    assert events == [("model", "")] and queries == []
    events.clear()
    assert await run_turn(model, "Check invoice", tools, on_event=progress) == (
        "PAY-00071: invoice accepted"
    )
    assert events == [("model", ""), ("tool_start", "search_index"), ("tool_end", "search_index"),
                      ("model", ""), ("tool_start", "read_document"), ("tool_end", "read_document"),
                      ("model", "")]
    assert queries == ["invoices"]


async def test_native_subagent_can_read_authorized_rag_without_environment_access() -> None:
    @tool
    async def read_document(identifier: str) -> str:
        """Read a permitted document."""
        return f"verified {identifier}"

    @tool
    async def call_environment_tool() -> str:
        """Call an external service."""
        pytest.fail("A subagent must not inherit its parent's environment")

    interactions: list[str] = []

    def decide(messages: list[BaseMessage], names: list[str]) -> AIMessage:
        last = messages[-1]
        if "task" in names:
            if isinstance(last, HumanMessage):
                interactions.append("delegate")
                return call("task", subagent_type="general-purpose", description="Inspect R-1")
            assert isinstance(last, ToolMessage) and "verified R-1" in last.text
            return AIMessage(content="Received verified R-1")
        assert "read_document" in names
        assert not ({"call_environment_tool", "execute", "write_file", "task"} & set(names))
        if isinstance(last, HumanMessage):
            interactions.append("read")
            return call("read_document", identifier="R-1")
        return AIMessage(content=last.text)

    answer = await run_turn(Model(respond=decide), "Investigate", [
        read_document, call_environment_tool,
    ])
    assert answer == "Received verified R-1" and interactions == ["delegate", "read"]


async def test_native_checkpoints_keep_history_in_only_the_owners_thread(tmp_path: Path) -> None:
    def respond(messages: list[BaseMessage], _: list[str]) -> AIMessage:
        turns = [m.text for m in messages if isinstance(m, HumanMessage)]
        return AIMessage(content=" / ".join(turns))

    model = Model(respond=respond)
    database = str(tmp_path / "agents.sqlite3")
    async with AsyncSqliteSaver.from_conn_string(database) as saver:
        assert await run_turn(model, "alpha", [], checkpointer=saver, thread_id="one") == "alpha"
        published = await saver.aget_tuple({"configurable": {"thread_id": "one"}})
        assert published is not None
    async with AsyncSqliteSaver.from_conn_string(database) as reopened:
        assert await run_turn(model, "beta", [], checkpointer=reopened,
                              thread_id="one") == "alpha / beta"
        assert await run_turn(model, "beta", [], checkpointer=reopened, thread_id="two") == "beta"
        assert await run_turn(
            model, "gamma", [], checkpointer=reopened, thread_id="one",
            checkpoint_id=published.config["configurable"]["checkpoint_id"],
        ) == "alpha / gamma"


async def test_permission_revoked_after_tool_prevents_delivering_its_result_to_model() -> None:
    permitted = True
    calls = 0

    async def authorize() -> bool:
        return permitted

    @tool
    async def read_document() -> str:
        """Read a document before the grant is revoked."""
        nonlocal permitted
        permitted = False
        return "classified"

    def decide(messages: list[BaseMessage], _: list[str]) -> AIMessage:
        nonlocal calls
        calls += 1
        assert all("classified" not in m.text for m in messages)
        return call("read_document")

    with pytest.raises(PermissionError):
        await run_turn(Model(respond=decide), "Read", [read_document], authorize=authorize)
    assert calls == 1


async def test_cancel_stops_the_running_sdk_tool() -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    @tool
    async def read_document() -> str:
        """Read a slow document."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return ""

    task = asyncio.create_task(run_turn(Model(respond=lambda *_: call("read_document")),
                                        "Read", [read_document]))
    async with asyncio.timeout(5):
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await stopped.wait()


async def test_activated_portal_controller_receives_real_sdk_tools(settings_factory: Any) -> None:
    from test_agent_activation import activated

    controller, runner = activated(settings_factory)
    try:
        async with create_model(settings_factory(), controller=controller) as model:
            assert await run_turn(model, "Which materials should we verify?", []) == "Answer [1]"
        assert len(runner.calls) == 2
        prompt = runner.calls[-1]["prompt"]
        assert "Which materials should we verify?" in prompt and "write_todos" in prompt
        assert runner.calls[-1]["decision_only"] is True
    finally:
        controller.stop()


async def test_corporate_api_authenticates_with_real_tls_certificate_without_bearer(
    settings_factory: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from corporate_kb.access.dev_pki import prepare_local_identity

    pytest.importorskip("langchain_openai")
    identity = prepare_local_identity(tmp_path / "certificates")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(identity.server_cert, identity.server_key)
    tls.load_verify_locations(cafile=identity.ca_cert)
    tls.verify_mode = ssl.CERT_REQUIRED
    monkeypatch.setenv("OPENAI_API_KEY", "a-key-for-a-different-service")
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
        async with create_model(
            settings_factory(), base_url=f"https://127.0.0.1:{port}/v1", model_name="corporate",
            ca_file=identity.ca_cert, cert_file=identity.client_cert, key_file=identity.client_key,
        ) as model:
            async with asyncio.timeout(8):
                assert await run_turn(model, "Hello", []) == "Hello!"
    assert len(requests) == 1
    headers, body, certificate = requests[0]
    assert certificate and b"authorization:" not in headers
    assert headers.startswith(b"post /v1/chat/completions ")
    assert json.loads(body)["messages"][-1]["content"] == "Hello"


@pytest.mark.parametrize("reason", ["content_filter", "length"])
async def test_rejected_or_truncated_message_stops_before_a_tool_call(reason: str) -> None:
    calls: list[str] = []

    @tool
    async def read_document() -> str:
        """Should never run from a rejected or incomplete response."""
        calls.append("tool")
        return "result"

    def decide(*_: Any) -> AIMessage:
        calls.append("model")
        msg = call("read_document")
        msg.response_metadata["finish_reason"] = reason
        return msg

    with pytest.raises((PermissionError, ValueError)):
        await run_turn(Model(respond=decide), "Read", [read_document])
    assert calls == ["model"]
