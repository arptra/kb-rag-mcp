"""Exercise runtime boundaries with controlled providers, without a live model."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime, QueueFullError
from agent_harness.store import RunStore

OWNER = Principal("alice")
OTHER = Principal("bob")


class Source:
    def __init__(self) -> None:
        self.allowed = ["a", "b"]
        self.calls: list[dict[str, Any]] = []
        self.revision = "1"
        self.active = 0
        self.peak = 0

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": item, "name": item} for item in self.allowed]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        self.calls.append(kwargs)
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return [
            Evidence(
                id=kwargs["index_id"],
                title="Current document",
                text="source " * 2000,
                index_id=kwargs["index_id"],
                revision=self.revision,
            )
        ]

    async def read(self, **kwargs: Any) -> Evidence:
        self.calls.append(kwargs)
        return Evidence(
            id=kwargs["document_id"],
            title="Read",
            text="Fresh detail",
            index_id=kwargs["index_id"],
            revision=self.revision,
        )


class Model:
    def __init__(self, calls: list[dict[str, Any]] | None = None, block: bool = False) -> None:
        self.tool_calls = calls or []
        self.block = block
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.cleaned = False

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        self.entered.set()
        if self.block:
            while not kwargs["cancel"].is_set():
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.02)
            self.cleaned = True
            return {"content": "", "tool_calls": []}
        return (
            {"content": "", "tool_calls": self.tool_calls}
            if kwargs["tools"]
            else {"content": "Explanation based on [a].", "tool_calls": []}
        )


class BrowserLoginModel(Model):
    def __init__(self, delay: float | None = 0.3, *, repeated: bool = False) -> None:
        super().__init__()
        self.delay = delay
        self.repeated = repeated
        self.authentication_required = False
        self.login_entered = asyncio.Event()

    def status(self) -> dict[str, Any]:
        return {
            "available": not self.authentication_required,
            "state": "authentication_required" if self.authentication_required else "ready",
            "authentication_url": "https://login.example.test/?secret=private-secret",
        }

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        self.entered.set()
        self.authentication_required = True
        self.login_entered.set()
        started = asyncio.get_running_loop().time()
        while self.delay is None or asyncio.get_running_loop().time() - started < self.delay:
            if kwargs["cancel"].is_set():
                self.cleaned = True
                self.authentication_required = False
                return {"content": "", "tool_calls": []}
            await asyncio.sleep(0.005)
        self.authentication_required = False
        # A second model turn proves that the investigation reserve uses the same clock.
        if self.repeated and len(self.requests) == 1:
            return {
                "content": "",
                "tool_calls": [{
                    "name": "read_document", "args": {"index_id": "a", "document_id": "a"},
                }],
            }
        return {"content": "Explanation after browser login [a].", "tool_calls": []}


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
async def test_quick_parallel_search_one_synthesis_and_bounded_evidence(tmp_path: Path) -> None:
    source, model = Source(), Model()
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="How does it work?"))
        assert submitted["status"] == "queued"
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert source.peak == 2
        assert len(model.requests) == 1 and model.requests[0]["tools"] == []
        assert all(len(item["text"]) <= 2000 for item in run["evidence"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "investigate"])
async def test_browser_login_time_is_excluded_from_work_deadlines(tmp_path: Path, mode: str):
    model = BrowserLoginModel(delay=0.3, repeated=mode == "investigate")
    runtime = HarnessRuntime(settings(
        tmp_path, quick_timeout_seconds=0.16, investigate_timeout_seconds=0.22,
        authentication_timeout_seconds=1, final_reserve_seconds=0.03,
    ), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Investigate", index_ids=["a"], mode=mode),
        )
        await model.login_entered.wait()
        await asyncio.sleep(0.1)
        waiting = await runtime.get(OWNER, submitted["id"])
        assert waiting["status"] == "running"
        assert waiting["phase"] == "authentication_required"
        assert "private-secret" not in json.dumps(waiting)
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert "after browser login" in run["answer"]
        assert len(model.requests) == (2 if mode == "investigate" else 1)
        assert run["metrics"]["duration_ms"] >= 290
        assert "private-secret" not in json.dumps(run)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_browser_login_wait_has_a_total_cap_and_actionable_warning(tmp_path: Path):
    model = BrowserLoginModel(delay=None)
    runtime = HarnessRuntime(settings(
        tmp_path, quick_timeout_seconds=0.15, authentication_timeout_seconds=0.12,
    ), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Investigate", index_ids=["a"]))
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "timed_out"
        assert model.cleaned
        assert any("авторизации истекло" in warning for warning in run["warnings"])
        assert any("повторить исследование" in warning for warning in run["warnings"])
        assert "private-secret" not in json.dumps(run)
        assert run["metrics"]["duration_ms"] < 1000
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_repeated_logins_share_one_cap_instead_of_resetting_it(tmp_path: Path):
    model = BrowserLoginModel(delay=0.14, repeated=True)
    runtime = HarnessRuntime(settings(
        tmp_path, investigate_timeout_seconds=0.5, authentication_timeout_seconds=0.2,
        final_reserve_seconds=0.05,
    ), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Investigate", index_ids=["a"], mode="investigate"),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "timed_out"
        assert len(model.requests) == 2
        assert model.cleaned
        assert any("авторизации истекло" in warning for warning in run["warnings"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_cancellation_during_browser_login_still_stops_model(tmp_path: Path):
    model = BrowserLoginModel(delay=None)
    runtime = HarnessRuntime(settings(
        tmp_path, quick_timeout_seconds=0.15, authentication_timeout_seconds=5,
    ), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Investigate", index_ids=["a"]))
        await model.login_entered.wait()
        await runtime.cancel(OWNER, submitted["id"])
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "cancelled"
        assert model.cleaned
        assert run["metrics"]["duration_ms"] < 1000
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_waits_for_existing_browser_login_before_first_model_call(tmp_path: Path):
    model = BrowserLoginModel(delay=0)
    model.authentication_required = True
    runtime = HarnessRuntime(settings(
        tmp_path, quick_timeout_seconds=0.16, authentication_timeout_seconds=1,
    ), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Investigate", index_ids=["a"]))
        await asyncio.sleep(0.25)
        assert not model.requests
        waiting = await runtime.get(OWNER, submitted["id"])
        assert waiting["status"] == "running"
        assert waiting["phase"] == "authentication_required"
        model.authentication_required = False
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert len(model.requests) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_no_model_returns_evidence_and_history_refreshes(tmp_path: Path) -> None:
    source = Source()
    runtime = HarnessRuntime(settings(tmp_path), source)
    await runtime.start()
    try:
        first = await runtime.submit(OWNER, RunRequest(question="Original"))
        initial = await finished(runtime, first["id"])
        assert initial["status"] == "partial" and initial["evidence"]
        source.revision = "2"
        followup = await runtime.submit(
            OWNER, RunRequest(question="More detail", parent_run_id=first["id"])
        )
        current = await finished(runtime, followup["id"])
        assert all(item["revision"] == "2" for item in current["evidence"])
        assert "Original" in source.calls[-1]["query"]
        for method in (runtime.get, runtime.cancel):
            with pytest.raises(KeyError):
                await method(OTHER, first["id"])
        with pytest.raises(KeyError):
            await runtime.submit(OTHER, RunRequest(question="Steal", parent_run_id=first["id"]))
        assert await runtime.list_runs(OTHER) == []
        source.allowed = []
        hidden = await runtime.get(OWNER, first["id"])
        assert hidden["evidence"] == [] and hidden["answer"] == ""
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_investigation_tool_validation_and_reserved_final_call(tmp_path: Path) -> None:
    source = Source()
    model = Model(
        [
            {"name": "shell", "args": {}},
            {"name": "read_document", "args": {"index_id": "private", "document_id": "x"}},
            {
                "name": "search_knowledge",
                "args": {"index_id": "a", "query": "x", "unexpected": True},
            },
        ]
    )
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(question="Investigate", index_ids=["a"], mode="investigate", effort="low"),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial"
        assert len(source.calls) == 2  # Initial search and an allowed document read.
        assert all(call["index_id"] == "a" for call in source.calls)
        assert run["metrics"]["tool_calls"] == 9
        assert model.requests[-1]["tools"] == [] and run["answer"]
        assert len(model.requests) <= 4
        for request in model.requests:
            assert len(json.dumps(request["messages"], ensure_ascii=False)) <= 48_000
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_repeated_tools_have_global_budget(tmp_path: Path) -> None:
    model = Model([{"name": "search_knowledge", "args": {"index_id": "a", "query": "again"}}])
    runtime = HarnessRuntime(settings(tmp_path), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Loop", mode="investigate", effort="low", index_ids=["a"])
        )
        run = await finished(runtime, submitted["id"])
        assert run["metrics"]["model_calls"] == 4
        assert run["metrics"]["source_calls"] == 5
        assert model.requests[-1]["tools"] == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [False, True])
async def test_cancel_and_deadline_await_provider_cleanup(tmp_path: Path, deadline: bool) -> None:
    model = Model(block=True)
    runtime = HarnessRuntime(
        settings(tmp_path, quick_timeout_seconds=0.1 if deadline else 10), Source(), model
    )
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Wait"))
        await asyncio.wait_for(model.entered.wait(), 1)
        assert (await runtime.get(OWNER, submitted["id"]))["phase"] == "synthesizing"
        if not deadline:
            await runtime.cancel(OWNER, submitted["id"])
        run = await finished(runtime, submitted["id"])
        assert run["status"] == ("timed_out" if deadline else "cancelled")
        assert model.cleaned
        if not deadline:
            model.block = False
            second = await runtime.submit(OWNER, RunRequest(question="Worker still usable"))
            assert (await finished(runtime, second["id"]))["status"] == "completed"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_backlog_bound_does_not_block_other_lane(tmp_path: Path) -> None:
    model = Model(block=True)
    runtime = HarnessRuntime(settings(tmp_path, quick_workers=1, queue_max=1), Source(), model)
    await runtime.start()
    try:
        active = await runtime.submit(OWNER, RunRequest(question="Active"))
        await asyncio.wait_for(model.entered.wait(), 1)
        await runtime.submit(OWNER, RunRequest(question="Queued"))
        with pytest.raises(QueueFullError):
            await runtime.submit(OWNER, RunRequest(question="Overflow"))
        other = await runtime.submit(OWNER, RunRequest(question="Other lane", mode="investigate"))
        async with asyncio.timeout(1):
            while (await runtime.get(OWNER, other["id"]))["status"] == "queued":
                await asyncio.sleep(0.01)
        await runtime.cancel(OWNER, active["id"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_restart_and_retention(tmp_path: Path) -> None:
    config = settings(tmp_path, max_runs_per_owner=2)
    store = RunStore(config)
    store.save(
        "alice",
        {
            "id": "interrupted",
            "status": "running",
            "created_at": "9999",
            "warnings": [],
            "evidence": [],
            "answer": "",
        },
    )
    runtime = HarnessRuntime(config, Source())
    await runtime.start()
    try:
        assert (await runtime.get(OWNER, "interrupted"))["status"] == "interrupted"
        for i in range(3):
            submitted = await runtime.submit(OWNER, RunRequest(question=str(i)))
            await finished(runtime, submitted["id"])
        assert len(await runtime.list_runs(OWNER)) == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "expected", "parallelism"), [("low", 8, 8), ("medium", 8, 8), ("high", 8, 8)]
)
async def test_quick_effort_searches_all_selected_sources(
    tmp_path: Path, effort: str, expected: int, parallelism: int
) -> None:
    source = Source()
    source.allowed = list("abcdefgh")
    runtime = HarnessRuntime(settings(tmp_path), source)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest.model_validate({"question": "Scope", "effort": effort})
        )
        run = await finished(runtime, submitted["id"])
        assert run["metrics"]["source_calls"] == expected
        assert source.peak == parallelism
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_fast_source_visible_before_slow_source_finishes(tmp_path: Path) -> None:
    release = asyncio.Event()

    class SlowSource(Source):
        async def search(self, **kwargs: Any) -> list[Evidence]:
            if kwargs["index_id"] == "b":
                await release.wait()
            return await super().search(**kwargs)

    runtime = HarnessRuntime(settings(tmp_path), SlowSource())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Progress"))
        async with asyncio.timeout(1):
            while not (run := await runtime.get(OWNER, submitted["id"]))["evidence"]:
                await asyncio.sleep(0.01)
        assert run["status"] == "running"
        assert run["evidence"][0]["index_id"] == "a"
        release.set()
        await finished(runtime, submitted["id"])
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_provider_cancelled_error_does_not_kill_worker(tmp_path: Path) -> None:
    class CancelledModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            if not self.block:
                return await super().decide(**kwargs)
            await super().decide(**kwargs)
            raise asyncio.CancelledError

    model = CancelledModel(block=True)
    runtime = HarnessRuntime(settings(tmp_path, quick_workers=1), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Cancel"))
        await asyncio.wait_for(model.entered.wait(), 1)
        cancellation = await runtime.cancel(OWNER, submitted["id"])
        assert cancellation["status"] == "cancelling"
        assert (await finished(runtime, submitted["id"]))["status"] == "cancelled"
        model.block = False
        second = await runtime.submit(OWNER, RunRequest(question="Next"))
        assert (await finished(runtime, second["id"]))["status"] == "completed"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_forbidden_final_tools_discard_model_content(tmp_path: Path) -> None:
    class InvalidModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            return {"content": "Unsupported final claim", "tool_calls": [{"name": "shell"}]}

    runtime = HarnessRuntime(settings(tmp_path), Source(), InvalidModel())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Explain"))
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial" and "Unsupported" not in run["answer"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_bootstrap_does_not_consume_followup_budget(tmp_path: Path) -> None:
    source = Source()
    source.allowed = list("abcdefgh")
    model = Model([{"name": "search_knowledge", "args": {"index_id": "a", "query": "loop"}}])
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="All sources", mode="investigate", effort="low")
        )
        run = await finished(runtime, submitted["id"])
        assert run["metrics"]["bootstrap_calls"] == 8
        assert run["metrics"]["source_calls"] > 8
        assert len(model.requests) == 4 and model.requests[0]["tools"]
        assert source.peak == 8
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_investigation_reserves_time_for_final_synthesis(tmp_path: Path) -> None:
    class SlowDecision(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.block = bool(kwargs["tools"])
            return await super().decide(**kwargs)

    model = SlowDecision()
    runtime = HarnessRuntime(settings(tmp_path, investigate_timeout_seconds=0.6), Source(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Slow planning", mode="investigate")
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial" and "Explanation" in run["answer"]
        assert model.cleaned and len(model.requests) == 2
        assert model.requests[-1]["tools"] == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effort", "count", "chars", "context"),
    [("low", 1, 1000, 16000), ("medium", 2, 1500, 20000), ("high", 3, 2000, 24000)],
)
async def test_single_index_effort_changes_evidence_and_context(
    tmp_path: Path, effort: str, count: int, chars: int, context: int
) -> None:
    class ManyDocuments(Source):
        async def search(self, **kwargs: Any) -> list[Evidence]:
            return [
                Evidence(id=str(i), title=str(i), text="x" * 5000, index_id="a") for i in range(10)
            ]

    model = Model()
    runtime = HarnessRuntime(settings(tmp_path), ManyDocuments(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest.model_validate(
                {"question": "One index", "index_ids": ["a"], "effort": effort}
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert len(run["evidence"]) == count
        assert all(len(item["text"]) == chars for item in run["evidence"])
        assert model.requests[0]["max_input_chars"] == context
        history = await runtime.list_runs(OWNER)
        assert history[0]["id"] == submitted["id"] and history[0]["question"] == "One index"
        assert (
            not {"answer", "evidence", "warnings", "parent_question", "metrics"} & history[0].keys()
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_failed_tool_outcome_is_reported_truthfully_to_model(tmp_path: Path) -> None:
    class FailedRead(Source):
        async def read(self, **kwargs: Any) -> Evidence:
            raise RuntimeError("Source offline")

    model = Model([{"name": "read_document", "args": {"index_id": "a", "document_id": "x"}}])
    runtime = HarnessRuntime(settings(tmp_path), FailedRead(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Read", mode="investigate"))
        await finished(runtime, submitted["id"])
        context = json.loads(model.requests[1]["messages"][-1]["content"])
        outcome = context["prior_tool_attempts"][0]["result"]
        assert "недоступно" in outcome and "добавлены" not in outcome
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_quick_slow_source_does_not_spend_synthesis_reserve(tmp_path: Path) -> None:
    class SlowSource(Source):
        async def search(self, **kwargs: Any) -> list[Evidence]:
            if kwargs["index_id"] == "b":
                await asyncio.sleep(1)
            return await super().search(**kwargs)

    model = Model()
    runtime = HarnessRuntime(settings(tmp_path, quick_timeout_seconds=0.3), SlowSource(), model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="One slow source"))
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial" and "Explanation" in run["answer"]
        assert len(model.requests) == 1
        assert any("TimeoutError" in warning for warning in run["warnings"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_invalid_model_decision_is_corrected_without_losing_research(tmp_path: Path) -> None:
    class RecoveringModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                raise ValueError('Untrusted provider output must not appear in the trace')
            context = json.loads(kwargs['messages'][-1]['content'])
            assert any(item['name'] == 'model_response' for item in context['prior_tool_attempts'])
            assert 'Untrusted provider output' not in json.dumps(context)
            assert context['evidence']
            return {'content': 'Corrected investigation [a].', 'tool_calls': []}

    model = RecoveringModel()
    runtime = HarnessRuntime(settings(tmp_path), Source(), model)
    await runtime.start()
    try:
        run = await runtime.submit(OWNER, RunRequest(question='Investigate', mode='investigate'))
        result = await finished(runtime, run['id'])
        assert result['status'] == 'completed'
        assert result['answer'] == 'Corrected investigation [a].'
        assert result['metrics']['decision_errors'] == 1
        assert result['metrics']['model_calls'] == 2
    finally:
        await runtime.stop()
