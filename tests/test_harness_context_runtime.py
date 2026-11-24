"""Conversation summaries participate in the actual queued model execution."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.policy import SYSTEM_PROMPT
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import timestamp

OWNER = Principal("alice")
CONSTRAINT = "EARLY_REQUIREMENT: inspect staging only; preserve invoice id."


class Source:
    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "a", "name": "A"}]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        return [Evidence(id="doc", title="State", text="Current staging state", index_id="a")]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def __init__(self, fail_summary: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []
        self.fail_summary = fail_summary

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        messages = kwargs["messages"]
        if messages[0]["content"] != SYSTEM_PROMPT:
            self.summaries.append(kwargs)
            if self.fail_summary:
                return {"content": "", "tool_calls": []}
            assert CONSTRAINT in str(messages)
            return {"content": CONSTRAINT + " Current goal: explain invoice retry.",
                    "tool_calls": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        self.requests.append(kwargs)
        return {"content": "Staging invoice retry verified [doc].", "tool_calls": [],
                "usage": {"prompt_tokens": 120, "completion_tokens": 12}}


def seed(app: HarnessRuntime) -> str:
    parent = None
    for i in range(24):
        key = f"prior-{i}"
        app.store.save(OWNER.owner_id, {
            "id": key, "status": "completed", "created_at": timestamp(),
            "updated_at": timestamp(), "mode": "quick", "effort": "medium",
            "question": CONSTRAINT if i == 0 else f"Investigate invoice detail {i}",
            "answer": "Verified prior observation about the invoice. " * 25,
            "index_ids": ["a"], "evidence": [], "parent_run_id": parent,
            "metrics": {"model_calls": 1}, "warnings": [], "phase": "completed",
        })
        parent = key
    assert parent is not None
    return parent


async def finish(app: HarnessRuntime, parent: str) -> dict[str, Any]:
    submitted = await app.submit(OWNER, RunRequest(
        question="Continue the invoice investigation", parent_run_id=parent, index_ids=["a"],
    ))
    async with asyncio.timeout(5):
        while True:
            result = await app.get(OWNER, submitted["id"])
            if result["status"] not in {"queued", "running", "cancelling"}:
                return result
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_summary", [False, True])
async def test_long_chat_compaction_uses_same_model_and_preserves_originals(
    tmp_path: Path, fail_summary: bool,
) -> None:
    model = Model(fail_summary)
    app = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
                         Source(), model)
    controls = app.control_store.load()
    controls.profiles["quick"]["medium"].context_tokens = 10_000
    app.control_store.save(controls, expected_revision=0)
    await app.start()
    try:
        parent = seed(app)
        first = await finish(app, parent)
        assert model.summaries
        assert app.store.get(OWNER.owner_id, "prior-0")["question"] == CONSTRAINT
        assert first["metrics"]["summary_calls"] == len(model.summaries)
        if fail_summary:
            assert first["status"] == "partial"
            assert first["stop_reason"] == "context_limit"
            assert not model.requests  # Do not answer after silently losing the user's constraints.
            with app.store._connect() as db:
                assert db.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0
            return
        assert first["status"] == "completed"
        assert CONSTRAINT in str(model.requests[-1]["messages"])
        assert first["context_summary"]["history_turns"] == 24
        assert first["metrics"]["compactions"] == len(model.summaries)
        assert first["metrics"]["decision_calls"] == 1
        assert first["metrics"]["model_calls"] == len(model.summaries) + 1
        assert first["metrics"]["input_tokens_reported"] == 100 * len(model.summaries) + 120
        assert first["metrics"]["output_tokens_reported"] == 20 * len(model.summaries) + 12
        count = len(model.summaries)
        second = await finish(app, first["id"])
        assert second["status"] == "completed"
        assert second["context_summary"]["reused_summary"]
        assert len(model.summaries) == count
        assert second["metrics"]["summary_calls"] == 0
        assert CONSTRAINT in str(model.requests[-1]["messages"])
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_access_revoked_during_summary_stops_the_next_model_call(tmp_path: Path) -> None:
    class RevocableSource(Source):
        allowed = True

        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return await super().indexes(principal) if self.allowed else []

    source = RevocableSource()

    class RevokingModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            result = await super().decide(**kwargs)
            if kwargs["messages"][0]["content"] != SYSTEM_PROMPT:
                source.allowed = False
            return result

    model = RevokingModel()
    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), source, model,
    )
    controls = app.control_store.load()
    controls.profiles["quick"]["medium"].context_tokens = 10_000
    app.control_store.save(controls, expected_revision=0)
    await app.start()
    try:
        seed(app)
        # These ten turns need one summary; revocation happens after that call
        # was legitimately started, before the final answer request is sent.
        result = await finish(app, "prior-9")
        assert len(model.summaries) == 1
        assert not model.requests
        assert result["status"] == "partial"
        assert not result["evidence"]
        assert app.store.get(OWNER.owner_id, "prior-0")["question"] == CONSTRAINT
        # A second request cannot revive the cached privileged summary either.
        from threading import Event

        from agent_harness.context import ContextPolicy

        assert app._conversation_context is not None
        prepared = await app._conversation_context.prepare(
            owner=OWNER.owner_id, run={"id": "fresh", "parent_run_id": "prior-9"},
            allowed=set(), selected={"a"}, policy=ContextPolicy(),
            invoke_summary=lambda messages: asyncio.sleep(0, result="unused"), cancel=Event(),
        )
        assert not prepared["messages"] and not prepared["summary"]
        assert prepared["history_truncated"]
    finally:
        await app.stop()
