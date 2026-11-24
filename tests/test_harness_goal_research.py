"""Goal-driven follow-up searches, live progress, durable plans and complete answers."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.research import initialize_plan
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("alice")


class Source:
    def __init__(self) -> None:
        self.allowed = ["a"]
        self.queries: list[str] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": index, "name": index} for index in self.allowed]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        query = kwargs["query"]
        self.queries.append(query)
        if query == "payment-service retry dependency":
            self.entered.set()
            await self.release.wait()
            key, text = "payment", "Retries wait for ledger confirmation."
        elif query == "ledger confirmation timeout":
            key, text = "ledger", "Confirmation timeout is configured at 30 seconds."
        else:
            key, text = "entry", "Requests arrive through payment-service."
        return [Evidence(id=key, title=key, text=text, index_id="a")]

    async def read(self, **kwargs: Any) -> Evidence:
        return Evidence(id=kwargs["document_id"], title="Entry detail",
                        text="payment-service retries require investigation.", index_id="a")


class Model:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = decisions
        self.contexts: list[dict[str, Any]] = []
        self.messages: list[list[dict[str, Any]]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.messages.append(kwargs["messages"])
        self.contexts.append(json.loads(kwargs["messages"][-1]["content"]))
        return self.decisions[min(len(self.contexts) - 1, len(self.decisions) - 1)]


def plan(*, complete: bool = False) -> dict[str, Any]:
    return {"name": "update_research_plan", "args": {
        "goal": "Explain payment retry delays",
        "questions": ["Find the dependency", "Check timeout"],
        "completed": ["Ledger confirmation timeout verified [ledger]"] if complete else [],
        "gaps": [] if complete else ["Check payment dependency and timeout"],
        "next_action": "Prepare the answer" if complete else "Follow the dependency",
        "ready_to_answer": complete,
    }}


def search(query: str) -> dict[str, Any]:
    return {"name": "search_knowledge", "args": {"index_id": "a", "query": query}}


def app(tmp_path: Path, model: Model, source: Source | None = None,
        **settings: Any) -> HarnessRuntime:
    return HarnessRuntime(HarnessSettings(
        _env_file=None, enabled=True, state_dir=tmp_path, **settings,
    ), source or Source(), model)


async def finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            run = await runtime.get(OWNER, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_recursive_findings_drive_next_search_and_persist_progress(tmp_path: Path) -> None:
    source = Source()
    model = Model([
        {"content": "", "tool_calls": [plan(), search("payment-service retry dependency")]},
        {"content": "", "tool_calls": [search("ledger confirmation timeout")]},
        {"content": "", "tool_calls": [plan(complete=True)]},
        {"content": "Payment retries wait 30 seconds for ledger confirmation [ledger].",
         "tool_calls": []},
    ])
    runtime = app(tmp_path, model, source)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain payment retry delays", mode="investigate", index_ids=["a"],
        ))
        await asyncio.wait_for(source.entered.wait(), 2)
        live = await runtime.get(OWNER, submitted["id"])
        assert live["phase"] == "refining"
        assert live["current_action"]["name"] == "search_knowledge"
        assert live["current_action"]["started_at"]
        assert live["research_plan"]["gaps"]
        source.release.set()
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed"
        assert result["research_plan"]["round"] == 3
        assert result["research_plan"]["ready_to_answer"] is True
        assert result["current_action"] is None
        assert "ledger" in {item["id"] for item in model.contexts[2]["evidence"]}
        assert "payment" in {item["id"] for item in model.contexts[1]["evidence"]}
        assert source.queries[-2:] == [
            "payment-service retry dependency", "ledger confirmation timeout",
        ]
        assert all(item["timestamp"] for item in result["research_trace"])
        assert runtime._graph is not None
        snapshots = [item async for item in runtime._graph.graph.aget_state_history(
            runtime._graph.config(OWNER, submitted["id"]),
        )]
        assert ("reflect",) in {item.next for item in snapshots}
        assert snapshots[0].values["run"]["research_plan"]["ready_to_answer"]
    finally:
        source.release.set()
        await runtime.stop()
    restored = app(tmp_path, model, source)
    await restored.start()
    try:
        assert (await restored.get(OWNER, submitted["id"]))["research_plan"]["round"] == 3
        source.allowed = []
        hidden = await restored.get(OWNER, submitted["id"])
        assert not hidden["research_plan"] and not hidden["research_trace"]
        assert hidden["memory_contribution"] is None
    finally:
        await restored.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_count", [4, 15])
async def test_model_receives_multiple_previous_turns_without_trusting_them(
    tmp_path: Path, turn_count: int,
) -> None:
    model = Model([{"content": "Answer from current source [entry]", "tool_calls": []}])
    runtime = app(tmp_path, model)
    await runtime.start()
    try:
        parent = None
        questions = [f"Original goal detail {number}" for number in range(turn_count)]
        for question in questions:
            run = await runtime.submit(OWNER, RunRequest(
                question=question, index_ids=["a"], parent_run_id=parent,
            ))
            result = await finished(runtime, run["id"])
            parent = run["id"]
        previous_users = [item["content"] for item in model.messages[-1][1:-2]
                          if item["role"] == "user"]
        assert previous_users == questions[:-1]
        assert result["context_summary"]["conversation_turns"] == turn_count - 1
        assert result["context_summary"]["conversation_truncated"] is False
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recover", [True, False])
async def test_empty_final_answer_gets_one_bounded_repair(tmp_path: Path, recover: bool) -> None:
    empty = {"content": "", "tool_calls": []}
    model = Model([empty, {"content": "Complete answer [entry]", "tool_calls": []}
                   if recover else empty])
    runtime = app(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Explain", index_ids=["a"]))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == ("completed" if recover else "partial")
        assert result["answer"]
        assert len(model.contexts) == 2
        assert result["metrics"]["answer_retries"] == 1
        assert any(item["name"] == "answer_recovery"
                   for item in model.contexts[-1]["prior_tool_attempts"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,finish_reason", [(None, "stop"), (1000, "stop"), (None, "length")])
async def test_long_or_incomplete_answer_is_never_silently_presented_as_complete(
    tmp_path: Path, limit: int | None, finish_reason: str,
) -> None:
    content = "Payment requests arrive here [entry]. " + "A" * 13000
    model = Model([{"content": content, "tool_calls": [], "finish_reason": finish_reason}])
    runtime = app(tmp_path, model, **({"answer_chars": limit} if limit else {}))
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Explain", index_ids=["a"]))
        result = await finished(runtime, submitted["id"])
        expected = "partial" if limit or finish_reason == "length" else "completed"
        assert result["status"] == expected
        if limit:
            assert len(result["answer"]) <= limit
            assert result["answer"].endswith("[Ответ сокращён по ограничению размера.]")
        else:
            assert result["answer"] == content
    finally:
        await runtime.stop()


def test_reinitialize_plan_after_permission_narrowing() -> None:
    run: dict[str, Any] = {"question": "Current goal", "research_plan": {}}
    initialize_plan(run)
    assert run["research_plan"]["goal"] == "Current goal"
    assert run["research_plan"]["round"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "investigate"])
async def test_reported_truncation_gets_a_complete_replacement(tmp_path: Path, mode: str) -> None:
    model = Model([
        {"content": "The dependency is", "tool_calls": [], "finish_reason": "length"},
        {"content": "Complete answer verified [entry].", "tool_calls": []},
    ])
    runtime = app(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", index_ids=["a"], mode=mode,
        ))
        result = await finished(runtime, submitted["id"])
        assert len(model.contexts) == 2
        assert result["status"] == "completed"
        assert result["answer"] == "Complete answer verified [entry]."
        assert any(item["name"] == "answer_recovery" for item in result["research_trace"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_valid_large_plan_is_fitted_to_serialized_context_budget(tmp_path: Path) -> None:
    large_plan = plan()
    for key in ("questions", "completed", "gaps"):
        large_plan["args"][key] = [str(number) + '"\\' * 249 for number in range(8)]
    large_plan["args"]["ready_to_answer"] = True

    class BoundedModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            envelope = {key: kwargs[key] for key in ("messages", "tools")}
            assert len(json.dumps(envelope, ensure_ascii=False)) + 1500 <= kwargs["max_input_chars"]
            return await super().decide(**kwargs)

    model = BoundedModel([
        {"content": "", "tool_calls": [large_plan]},
        {"content": "Some checks unavailable [entry].", "tool_calls": []},
    ])
    runtime = app(tmp_path, model, investigate_context_chars=12000)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", index_ids=["a"], mode="investigate",
        ))
        result = await finished(runtime, submitted["id"])
        assert model.contexts[-1]["research_plan_truncated"]
        assert len(model.contexts) == 2
        # Ready for a limited answer does not mean unresolved checks are complete.
        assert result["status"] == "partial"
        assert any("непроверенные вопросы" in warning for warning in result["warnings"])
    finally:
        await runtime.stop()
