from __future__ import annotations

import asyncio
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
