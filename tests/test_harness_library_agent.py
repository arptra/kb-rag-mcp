import asyncio
from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from agent_harness.contracts import ModelDecisionError
from agent_harness.library_agent import run_agent_round

TOOLS = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
         for name in ("lookup", "verify")]


async def test_deep_agent_uses_only_scoped_tools_and_no_extra_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("deepagents", minversion="0.7.39")
    import json

    from agent_harness import library_agent

    bindings = []
    original = library_agent._HarnessChatModel.bind_tools

    def capture(model: Any, tools: Any, **kwargs: Any) -> Any:
        bindings.append({tool.name for tool in tools})
        return original(model, tools, **kwargs)

    monkeypatch.setattr(library_agent._HarnessChatModel, "bind_tools", capture)
    framework, agent = library_agent._agent(json.dumps(TOOLS, ensure_ascii=False, sort_keys=True))
    assert framework == "Deep Agents"
    assert agent.config["metadata"]["ls_integration"] == "deepagents"
    decisions, dispatched = [], []
    large_receipt = "evidence " * 10000

    async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
        decisions.append(len(observations))
        if observations:
            assert observations[0]["result"] == large_receipt
            return {"content": "Checked"}
        return {"tool_calls": [{"name": "lookup", "args": {}}]}

    async def dispatch(calls: list[dict[str, Any]]) -> list[str]:
        dispatched.extend(call["name"] for call in calls)
        return [large_receipt]

    started: list[str] = []
    result = await run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch,
                                   on_start=started.append)
    assert started == [framework]
    assert result["content"] == "Checked" and dispatched == ["lookup"]
    assert decisions == [0, 1] and bindings and all(b == {"lookup", "verify"} for b in bindings)


@pytest.mark.parametrize(("installed", "name"), [(False, "base_lookup"), (True, "grep")])
async def test_base_or_conflicting_tool_uses_langchain(
    monkeypatch: pytest.MonkeyPatch, installed: bool, name: str,
) -> None:
    from agent_harness import library_agent

    monkeypatch.setattr(library_agent, "deep_agents_installed", lambda: installed)
    catalog = [{"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}]
    started: list[str] = []

    async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
        if observations:
            return {"content": observations[0]["result"]}
        return {"tool_calls": [{"name": name, "args": {}}]}

    async def dispatch(batch: list[dict[str, Any]]) -> list[str]:
        assert started == ["LangChain"] and batch[0]["name"] == name
        return ["authorized tool result"]

    result = await run_agent_round(tools=catalog, decide=decide, dispatch=dispatch,
                                   on_start=started.append)
    assert result["content"] == "authorized tool result"


async def test_native_tools_run_in_order_and_feed_the_next_provider_decision() -> None:
    calls = [{"name": name, "args": {"document": "payments"}, "id": str(i)}
             for i, name in enumerate(("lookup", "verify", "lookup"))]
    events: list[str] = []
    followup = {"content": "", "tool_calls": [{"name": "verify", "args": {"document": "ledger"}}]}

    async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
        events.append("model")
        if not observations:
            return {"tool_calls": calls}
        assert observations == [{**call, "result": "result " + call["id"]} for call in calls]
        return followup

    async def dispatch(batch: list[dict[str, Any]]) -> list[str]:
        [call] = batch
        events.append("start " + call["id"])
        await asyncio.sleep(0)
        events.append("finished " + call["id"])
        return ["result " + call["id"]]

    result = await run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch)
    assert result == followup
    assert events == ["model", "start 0", "finished 0", "start 1", "finished 1",
                      "start 2", "finished 2", "model"]


@pytest.mark.parametrize("outcome", ["stop", "exception", "invalid"])
async def test_stop_cancel_or_invalid_batch_does_not_replay_side_effects(outcome: str) -> None:
    calls = []
    decisions = []

    async def decide(_observations: list[dict[str, Any]]) -> dict[str, Any]:
        decisions.append(True)
        return {"tool_calls": [{"name": "lookup", "args": {}},
                               {"name": "outside_scope" if outcome == "invalid" else "verify",
                                "args": {}}]}

    async def dispatch(batch: list[dict[str, Any]]) -> list[str] | None:
        calls.extend(batch)
        if outcome == "exception":
            raise TimeoutError("result of operation unknown")
        return None

    invoke = run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch)
    if outcome == "invalid":
        with pytest.raises(ModelDecisionError):
            await invoke
        assert not calls
    elif outcome == "stop":
        assert (await invoke)["agent_round_stopped"]
    else:
        with pytest.raises(TimeoutError):
            await invoke
    assert decisions == [True]
    assert len(calls) == (0 if outcome == "invalid" else 1)


async def test_multiple_chats_share_compiled_agent_but_keep_observations_private() -> None:
    async def run(owner: str) -> str:
        async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
            await asyncio.sleep(0)
            if observations:
                return {"content": observations[0]["result"], "tool_calls": []}
            return {"tool_calls": [{"name": "lookup", "args": {"question": owner}}]}

        async def dispatch(calls: list[dict[str, Any]]) -> list[str]:
            await asyncio.sleep(0)
            assert calls[0]["args"]["question"] == owner
            return [owner]

        result = await run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch)
        return str(result["content"])

    assert await asyncio.gather(run("alice"), run("bob")) == ["alice", "bob"]


async def test_cancel_propagates_to_inflight_dispatch_without_starting_next_tool() -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    executed = []

    async def decide(_observations: list[dict[str, Any]]) -> dict[str, Any]:
        return {"tool_calls": [{"name": "lookup", "args": {}}, {"name": "verify", "args": {}}]}

    async def dispatch(batch: list[dict[str, Any]]) -> list[str]:
        executed.append(batch[0]["name"])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return ["done"]

    task = asyncio.create_task(run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and executed == ["lookup"]


async def test_nested_agent_keeps_only_the_parent_sqlite_checkpoint(tmp_path: Path) -> None:
    class State(TypedDict):
        answer: str

    async def step(_state: State) -> State:
        async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
            if observations:
                return {"content": observations[0]["result"], "tool_calls": []}
            return {"tool_calls": [{"name": "verify", "args": {"doc": "private"}}]}

        async def dispatch(calls: list[dict[str, Any]]) -> list[str]:
            assert len(calls) == 1
            return ["verified"]

        answer = await run_agent_round(tools=TOOLS, decide=decide, dispatch=dispatch)
        return {"answer": answer["content"]}

    parent = StateGraph(State)
    parent.add_node("step", step)
    parent.add_edge(START, "step")
    parent.add_edge("step", END)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        workflow = parent.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "owner-chat"}}
        assert await workflow.ainvoke({"answer": ""}, config=config) == {"answer": "verified"}
        assert (await workflow.aget_state(config)).values == {"answer": "verified"}
        cursor = await saver.conn.execute("SELECT DISTINCT checkpoint_ns FROM checkpoints")
        assert [row[0] for row in await cursor.fetchall()] == [""]
