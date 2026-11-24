import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from test_harness_context_regressions import OWNER, finished, record
from test_harness_goal_research import Model, plan, search

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, RunRequest
from agent_harness.runtime import HarnessRuntime


class FlakySource:
    def __init__(self) -> None:
        self.fail = False
        self.empty = False

    async def indexes(self, *_args: Any) -> list[dict[str, str]]:
        return [{"id": x, "name": x} for x in ("a", "b")]

    def document(self, index_id: str, document_id: str) -> Evidence:
        if self.fail:
            raise TimeoutError
        return Evidence(
            id=f"{index_id}:{document_id}", document_id=document_id, index_id=index_id,
            title="Current order stages",
            text="Orders are checked before processing." if not self.empty else "",
        )

    async def read(self, *, index_id: str, document_id: str, **_kw: Any) -> Evidence:
        return self.document(index_id, document_id)

    async def search(self, *, index_id: str, **_kw: Any) -> list[Evidence]:
        return [self.document(index_id, index_id + "-source")]


@pytest.mark.parametrize("tool", ["read_document", "search_knowledge"])
async def test_successful_retry_resolves_only_its_source_and_teaches_cited_route(
    tmp_path: Path, tool: str,
) -> None:
    source = FlakySource()
    app = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), source)
    await app.start()
    try:
        run = record(app, ["a", "b"])
        run.update(mode="autonomous", research_plan={
            "model_updated": True, "ready_to_answer": True, "gaps": [],
        })
        app._semaphores[run["id"]] = asyncio.Semaphore(1)
        event = Event()

        async def check(index: str, document: str = "a-source", *, initial: bool = False) -> str:
            if tool == "read_document":
                args = {"index_id": index, "document_id": document}
                return await app._read(OWNER, run, event, args, model_requested=True)
            return await app._search(OWNER, run, event, index, "order stages", initial=initial)

        source.fail = True
        await check("a", initial=True)
        warning = run["warnings"][0]
        assert "TimeoutError" in warning
        if tool == "read_document":
            # Two failures with the same message remain distinguishable.
            await check("a", "other-doc")
        source.fail = False
        await check("b")
        assert warning in run["warnings"]
        source.empty = True
        await check("a")
        assert warning in run["warnings"]
        if tool == "search_knowledge":
            assert run["coverage"]["a"]["status"] == "failed"
        source.empty = False
        await check("a")
        if tool == "read_document":
            assert warning in run["warnings"]
            await check("a", "other-doc")
        else:
            assert run["coverage"]["a"]["status"] == "searched"
        assert not run["warnings"]
        assert any(step["result"] == warning for step in run["research_trace"])
        assert any("Повторная проверка" in step["result"] for step in run["research_trace"])
        answer = {"content": "Orders are checked before processing [a:a-source].",
                  "finish_reason": "stop"}
        # Preserve all independent limits when the specific source check recovers.
        app._warn(run, "The overall research deadline expired.")
        assert app._answer(run, answer) and run["status"] == "partial"
        run["warnings"].remove("The overall research deadline expired.")
        assert app._answer(run, answer) and run["status"] == "completed"
        case = app.memory.record_outcome(OWNER.owner_id, run, index_ids=["a", "b"])
        assert case and case["failures"]
        choices = case["strategy"]["successful_tools"]
        assert [(route["tool"], route["index_id"]) for route in choices] == [(tool, "a")]
        # A warning restored from an older checkpoint has no verifiable target.
        run["warnings"].append(warning)
        run.pop("_pending_source_checks", None)
        source.fail = True
        await check("a")
        source.fail = False
        await check("a")
        assert warning in run["warnings"]
    finally:
        await app.stop()


async def test_autopilot_finishes_after_first_search_times_out(tmp_path: Path) -> None:
    class OnceUnavailable(FlakySource):
        async def search(self, *, index_id: str, **kwargs: Any) -> list[Evidence]:
            if kwargs["query"] == "Check orders":
                raise TimeoutError
            return await super().search(index_id=index_id, **kwargs)

    model = Model([
        {"tool_calls": [search("order stages")]},
        {"tool_calls": [plan(complete=True)]},
        {"content": "Orders are checked before processing [a:a-source].", "tool_calls": []},
    ])
    app = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
                         OnceUnavailable(), model)
    await app.start()
    try:
        run = await app.submit(OWNER, RunRequest(question="Check orders", mode="autonomous",
                                                 index_ids=["a"]))
        result = await finished(app, run["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["coverage"]["a"]["status"] == "searched"
        experience = app.memory.get(OWNER.owner_id, result["memory_contribution"]["id"])
        routes = experience["strategy"]["successful_tools"]
        assert [r["tool"] for r in routes] == ["search_knowledge"]
        assert any(f["reason"] == "timeout" for f in experience["failures"])
    finally:
        await app.stop()


@pytest.mark.parametrize("outcome", ["failed", "empty"])
async def test_unsuccessful_result_cannot_support_citation_or_fallback(
    tmp_path: Path, outcome: str,
) -> None:
    app = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None))
    await app.start()
    try:
        run = record(app, ["a"])
        run["mode"] = "autonomous"
        bad = {"index_id": "a", "id": "bad", "title": "Command status",
               "text": "Process output placeholder", "tool_outcome": outcome}
        good = {"index_id": "a", "id": "doc", "title": "Checked document",
                "text": "Orders are checked before processing.", "checked_with": "read_document"}
        run["evidence"] = [bad, good]
        assert not app._answer(run, {"content": "The gateway checks orders first [bad]."})
        app._evidence_answer(run)
        assert "[doc]" in run["answer"] and "[bad]" not in run["answer"]
        run["evidence"] = [bad]
        run.pop("_final_quality_issue")
        app._evidence_answer(run)
        assert run["answer"] == "Подтверждённых данных для ответа пока нет."
        run["mode"] = "investigate"
        assert app._answer(run, {"content": "The gateway checks orders first."})
        assert run["status"] == "partial"
    finally:
        await app.stop()
