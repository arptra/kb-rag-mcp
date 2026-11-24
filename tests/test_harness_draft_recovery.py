"""Exercise rejected planner drafts through LangGraph, source reads and SQLite."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_harness_autonomy import OWNER, Model, build, finished

from agent_harness.contracts import Evidence, Principal, RunRequest

QUESTION = "Почему платёж задерживается?"
DISCLAIMER = "К сожалению, иногда генеративные модели могут создавать некорректные ответы."  # noqa: RUF001
OUTCOME = "Платёж ждёт подтверждения журнала до 30 секунд [ledger]."


class Documents:
    def __init__(self, folder: Path, *, include_ledger: bool) -> None:
        self.folder, self.include_ledger = folder, include_ledger
        (folder / "entry").write_text("Платёж поступил в очередь. См. PAY-72.")
        (folder / "ledger").write_text("Платёж ждёт подтверждения журнала до 30 секунд.")
        self.reads: list[str] = []
        self.searches: list[str] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": index, "name": index} for index in ["a", "not-selected"]]

    async def search(self, *, query: str, index_id: str, **kwargs: Any) -> list[Evidence]:
        assert index_id == "a"
        self.searches.append(query)
        docs = [Evidence(id="entry", title="Платёж", index_id=index_id,
                         text=(self.folder / "entry").read_text())]
        if self.include_ledger:
            docs.append(Evidence(id="ledger", title="Журнал", text="Регламент.", index_id=index_id))
        return docs

    async def read(self, *, document_id: str, index_id: str, **kwargs: Any) -> Evidence:
        assert index_id == "a"
        self.reads.append(document_id)
        if document_id == "ledger":
            self.entered.set()
            await self.release.wait()
        return Evidence(id=document_id, title=document_id, index_id=index_id,
                        text=(self.folder / document_id).read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("planner_uses_tool", [True, False])
async def test_rejected_draft_leads_to_a_new_source_read_and_cited_result(
    tmp_path: Path, planner_uses_tool: bool,
) -> None:
    bad = {"content": DISCLAIMER, "tool_calls": []}
    read = {"content": "", "tool_calls": [{"name": "read_document", "args": {
        "index_id": "a", "document_id": "ledger",
    }}]}
    model = Model([bad, read if planner_uses_tool else bad, {"content": OUTCOME, "tool_calls": []}])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, document_reads=1)
    source = Documents(tmp_path, include_ledger=True)
    runtime.source = source
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question=QUESTION, mode="autonomous", index_ids=["a"],
        ))
        await asyncio.wait_for(source.entered.wait(), 3)
        live = await runtime.get(OWNER, submitted["id"])
        assert live["status"] == "running" and not live["answer"]
        assert live["current_action"]["args"] == {"index_id": "a", "document_id": "ledger"}
        assert any(step["name"] == "answer_quality_rejected" for step in live["research_trace"])
        source.release.set()
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == OUTCOME
        assert source.reads == ["entry", "ledger"]
        assert source.searches == [QUESTION]
        assert result["metrics"]["rejected_drafts"] == (1 if planner_uses_tool else 2)
        assert model.requests[1]["tools"]
        context = json.loads(model.requests[-1]["messages"][-1]["content"])
        assert any(item["id"] == "ledger" and "30 секунд" in item["text"]
                   for item in context["evidence"])
        assert any(step["name"] == "research_recovery" for step in context["prior_tool_attempts"])
        assert DISCLAIMER not in json.dumps(result, ensure_ascii=False)
    finally:
        source.release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_repeated_nonanswer_follows_a_lead_once_then_reports_no_verified_result(
    tmp_path: Path,
) -> None:
    model = Model([{"content": DISCLAIMER, "tool_calls": []}])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, document_reads=1)
    source = Documents(tmp_path, include_ledger=False)
    runtime.source = source
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question=QUESTION, mode="autonomous", index_ids=["a"],
        ))
        result = await finished(runtime, submitted["id"])
        assert source.searches == [QUESTION, "PAY-72"]
        assert source.reads == ["entry"]
        assert result["answer"] and DISCLAIMER not in result["answer"]
        assert result["status"] == "partial"
        assert not any(item.get("checked_with") for item in result["evidence"])
        assert len(model.requests) <= result["budget"]["current"]["model_calls"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stalling_on_plan_updates_triggers_a_concrete_source_check(tmp_path: Path) -> None:
    ready = {"content": "", "tool_calls": [{"name": "update_research_plan", "args": {
        "goal": QUESTION, "ready_to_answer": True,
    }}]}
    model = Model([ready, ready, ready, {"content": OUTCOME, "tool_calls": []}])
    # The recovery read is exactly the last allowed tool call. A verified
    # closed goal can still finish normally within the answer reserve.
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, tool_calls=4, document_reads=1)
    source = Documents(tmp_path, include_ledger=True)
    source.release.set()
    runtime.source = source
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question=QUESTION, mode="autonomous", index_ids=["a"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == OUTCOME
        assert result["metrics"]["tool_calls"] == result["budget"]["current"]["tool_calls"]
        assert source.reads == ["entry", "ledger"]
        assert any(step["name"] == "research_recovery" for step in result["research_trace"])
    finally:
        await runtime.stop()
