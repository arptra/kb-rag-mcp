"""Durable graph recovery never silently replays an uncertain external action."""

import asyncio
import json
import sqlite3
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("alice")
OTHER = Principal("bob")


class Source:
    def __init__(self) -> None:
        self.allowed = ["a"]

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": item, "name": item} for item in self.allowed]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        return [Evidence(id="doc", title="Current", text="Fresh", index_id=kwargs["index_id"])]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def __init__(self, *, tool: bool = False) -> None:
        self.tool = tool
        self.calls = 0

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.tool and self.calls == 1:
            return {"content": "", "tool_calls": [{"name": "external_operation", "args": {}}]}
        return {"content": "Verified answer [doc]", "tool_calls": []}


class External:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.closed = []

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "connection:test", "name": "Test"}]

    async def describe(self, principal: Principal, index_ids: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {"name": "external_operation", "parameters": {"type": "object"}},
            }
        ]

    async def call(
        self,
        name: str,
        args: Any,
        *,
        principal: Principal,
        run_id: str,
        index_ids: list[str],
        cancel: Event,
    ) -> list[Evidence]:
        self.calls += 1  # Represents an external side effect already accepted.
        self.entered.set()
        while not cancel.is_set():
            await asyncio.sleep(0.005)
        raise asyncio.CancelledError

    async def close_run(self, run_id: str) -> None:
        self.closed.append(run_id)


def settings(tmp_path: Path, **kwargs: Any) -> HarnessSettings:
    return HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path, **kwargs)


async def finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            run = await runtime.get(OWNER, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_real_langgraph_nodes_checkpoints_and_delete(tmp_path: Path) -> None:
    runtime = HarnessRuntime(settings(tmp_path), Source(), Model())
    await runtime.start()
    try:
        run = await runtime.submit(
            OWNER, RunRequest(question="Inspect", mode="investigate", index_ids=["a"])
        )
        result = await finished(runtime, run["id"])
        assert result["status"] == "completed"
        assert runtime._graph is not None
        graph = runtime._graph
        assert set(graph.graph.get_graph().nodes) == {
            "__start__",
            "prepare",
            "plan",
            "collect",
            "inspect",
            "decide",
            "tools",
            "reflect",
            "synthesize",
            "__end__",
        }
        snapshots = [
            item async for item in graph.graph.aget_state_history(graph.config(OWNER, run["id"]))
        ]
        assert len(snapshots) >= 4
        assert {item.next for item in snapshots} >= {("prepare",), ("collect",), ("decide",), ()}
        assert not await graph.has_checkpoint(OTHER, run["id"])
        assert all(isinstance(json.dumps(item.values), str) for item in snapshots)
        assert (tmp_path / "runs.sqlite3").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "checkpoints.sqlite3").stat().st_mode & 0o777 == 0o600
        await runtime.delete_run(OWNER, run["id"])
        assert not await graph.has_checkpoint(OWNER, run["id"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_restart_resume_does_not_replay_uncertain_tool_and_preserves_budget(
    tmp_path: Path,
) -> None:
    tools = External()
    first = HarnessRuntime(settings(tmp_path), Source(), Model(tool=True), tools)
    await first.start()
    run = await first.submit(
        OWNER,
        RunRequest(question="Inspect", mode="investigate", index_ids=["a", "connection:test"]),
    )
    await tools.entered.wait()
    await first.stop()
    previous = first.store.get(OWNER.owner_id, run["id"])
    assert previous["status"] == "cancelled" and tools.calls == 1
    second = HarnessRuntime(settings(tmp_path), Source(), Model(), tools)
    await second.start()
    try:
        assert (await second.get(OWNER, run["id"]))["resumable"]
        with pytest.raises(KeyError):
            await second.resume(OTHER, run["id"])
        await second.resume(OWNER, run["id"])
        result = await finished(second, run["id"])
        assert tools.calls == 1
        assert result["resume_count"] == 1
        assert result["metrics"]["model_calls"] > previous["metrics"]["model_calls"]
        assert result["metrics"]["source_calls"] > previous["metrics"]["source_calls"]
        assert any("не повторён" in item for item in result["warnings"])
        assert "Verified answer" in result["answer"]
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_resume_rechecks_access_and_never_resets_exhausted_budget(tmp_path: Path) -> None:
    source, tools = Source(), External()
    first = HarnessRuntime(settings(tmp_path), source, Model(tool=True), tools)
    await first.start()
    run = await first.submit(
        OWNER,
        RunRequest(
            question="Inspect", mode="investigate", effort="low", index_ids=["a", "connection:test"]
        ),
    )
    await tools.entered.wait()
    await first.stop()
    second = HarnessRuntime(settings(tmp_path), source, Model(), tools)
    await second.start()
    try:
        source.allowed = []
        with pytest.raises(ValueError, match="Доступ"):
            await second.resume(OWNER, run["id"])
        record = second.store.get(OWNER.owner_id, run["id"])
        record["metrics"]["model_calls"] = 4
        second.store.save(OWNER.owner_id, record)
        with pytest.raises(ValueError, match="бюджет"):
            await second.resume(OWNER, run["id"], ["connection:test"])
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_checkpoint_retention_tracks_run_retention(tmp_path: Path) -> None:
    runtime = HarnessRuntime(settings(tmp_path, max_runs_per_owner=1), Source(), Model())
    await runtime.start()
    try:
        first = await runtime.submit(OWNER, RunRequest(question="First", index_ids=["a"]))
        await finished(runtime, first["id"])
        second = await runtime.submit(OWNER, RunRequest(question="Second", index_ids=["a"]))
        await finished(runtime, second["id"])
        assert runtime._graph is not None
        await runtime._graph.prune()
        assert not await runtime._graph.has_checkpoint(OWNER, first["id"])
        assert await runtime._graph.has_checkpoint(OWNER, second["id"])
        with sqlite3.connect(tmp_path / "checkpoints.sqlite3") as db:
            threads = db.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
        assert len(threads) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_resume_enqueues_only_one_attempt(tmp_path: Path) -> None:
    tools = External()
    first = HarnessRuntime(settings(tmp_path), Source(), Model(tool=True), tools)
    await first.start()
    run = await first.submit(
        OWNER,
        RunRequest(question="Inspect", mode="investigate", index_ids=["a", "connection:test"]),
    )
    await tools.entered.wait()
    await first.stop()
    second = HarnessRuntime(settings(tmp_path), Source(), Model(), tools)
    await second.start()
    try:
        results = await asyncio.gather(
            second.resume(OWNER, run["id"]), second.resume(OWNER, run["id"]), return_exceptions=True
        )
        assert sum(isinstance(item, dict) for item in results) == 1
        assert sum(isinstance(item, ValueError) for item in results) == 1
        assert (await finished(second, run["id"]))["resume_count"] == 1
    finally:
        await second.stop()
