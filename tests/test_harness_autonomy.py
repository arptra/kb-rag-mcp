"""Autopilot grants are bounded, observable and fixed to the run's operator revision."""

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, ModelDecisionError, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("alice")


class Source:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.available = True

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "a", "name": "Documents"}] if self.available else []

    async def search(self, **kwargs: Any) -> list[Evidence]:
        self.queries.append(kwargs["query"])
        query = kwargs["query"]
        return [Evidence(id=query, title="Source", text="Verified " + query, index_id="a")]

    async def read(self, **kwargs: Any) -> Evidence:
        return Evidence(id=kwargs["document_id"], title="Source", text="Fresh detail", index_id="a")


class Model:
    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = decisions
        self.requests: list[dict[str, Any]] = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.block_first = False
        self.delay_second = 0.0

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        position = len(self.requests) - 1
        self.entered.set()
        if position == 0 and self.block_first:
            await self.release.wait()
        if position == 1 and self.delay_second:
            await asyncio.sleep(self.delay_second)
        return self.decisions[min(position, len(self.decisions) - 1)]


def request(**changes: Any) -> dict[str, Any]:
    return {
        "content": "",
        "tool_calls": [
            {
                "name": "request_budget",
                "args": {
                    "reason": "Verify the unresolved dependency using additional source checks",
                    "next_action": "Read the dependency implementation",
                    "changes": changes,
                },
            }
        ],
    }


ANSWER = {"content": "Verified final answer [Explain].", "tool_calls": []}
PLAN_READY = {"content": "", "tool_calls": [{"name": "update_research_plan", "args": {
    "goal": "Explain", "completed": ["Verified source [Explain]"], "gaps": [],
    "ready_to_answer": True,
}}]}
READ = {"content": "", "tool_calls": [
    {"name": "read_document", "args": {"index_id": "a", "document_id": "Explain"}},
]}


def build(tmp_path: Path, model: Model, **overrides: Any) -> HarnessRuntime:
    runtime = HarnessRuntime(
        HarnessSettings(
            _env_file=None,
            state_dir=tmp_path,
            enabled=True,
            cleanup_timeout_seconds=0.02,
        ),
        Source(),
        model,
    )
    policy = runtime.control_store.load()
    policy.runtime.final_reserve_seconds = 0.1
    policy.runtime.max_answer_retries = 0
    policy.context.window_tokens = 8192
    policy.context.response_reserve_tokens = 1024
    for profile in policy.profiles["autonomous"].values():
        for name, value in {
            "model_calls": 4,
            "summary_calls": 2,
            "source_calls": 2,
            "tool_calls": 4,
            "document_reads": 0,
            "timeout_seconds": 2.0,
            "context_tokens": 6000,
            **overrides,
        }.items():
            setattr(profile, name, value)
    for name, value in {
        "model_calls": 6,
        "summary_calls": 3,
        "source_calls": 5,
        "tool_calls": 8,
        "timeout_seconds": 3.0,
        "context_tokens": 10000,
    }.items():
        setattr(policy.autonomy.caps, name, value)
    runtime.control_store.save(policy, expected_revision=0)
    return runtime


async def finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            run = await runtime.get(OWNER, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_autopilot_requests_expand_only_to_operator_and_context_caps(tmp_path: Path) -> None:
    model = Model(
        [
            request(
                model_calls=999,
                source_calls=999,
                tool_calls=999,
                timeout_seconds=999.0,
                context_tokens=999999,
            ),
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "search_knowledge",
                        "args": {
                            "query": "new dependency",
                            "index_id": "a",
                        },
                    }
                ],
            },
            ANSWER,
        ]
    )
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert run["budget"]["extensions"] == 1
        assert run["budget"]["current"]["model_calls"] == 6
        assert run["budget"]["current"]["source_calls"] == 5
        assert run["budget"]["current"]["tool_calls"] == 8
        assert run["budget"]["current"]["timeout_seconds"] == 3
        assert run["budget"]["current"]["context_tokens"] == 8192 - 1024
        assert run["budget"]["events"][0]["requested"]["model_calls"] == 999
        assert run["budget"]["events"][0]["granted"]["model_calls"] == 6
        assert run["metrics"]["decision_calls"] == len(model.requests)
        assert run["metrics"]["model_calls"] == len(model.requests)
        assert any(item["name"] == "request_budget" for item in run["research_trace"])
        first_context = json.loads(model.requests[0]["messages"][-1]["content"])
        assert "resource_budget" in first_context
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "investigate"])
async def test_fixed_modes_never_grant_model_budget_requests(tmp_path: Path, mode: str) -> None:
    model = Model([request(model_calls=999), ANSWER])
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode=mode,
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["budget"]["extensions"] == 0
        assert run["budget"]["initial"] == run["budget"]["current"]
        assert not any(item["accepted"] for item in run["budget"]["events"])
        assert all(
            "request_budget" not in {tool["function"]["name"] for tool in invocation["tools"]}
            for invocation in model.requests
        )
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_repeated_extension_without_new_evidence_is_denied(tmp_path: Path) -> None:
    model = Model([request(model_calls=6), request(source_calls=5), ANSWER])
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert [item["accepted"] for item in run["budget"]["events"]] == [True, False]
        assert run["budget"]["extensions"] == 1
        assert run["budget"]["current"]["source_calls"] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_time_grant_extends_outer_clock_not_just_displayed_budget(tmp_path: Path) -> None:
    model = Model([request(timeout_seconds=2.0, model_calls=6), READ, ANSWER])
    model.delay_second = 0.6
    runtime = build(tmp_path, model, timeout_seconds=0.4)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert run["metrics"]["duration_ms"] > 400
        assert run["budget"]["initial"]["timeout_seconds"] == 0.4
        assert run["budget"]["current"]["timeout_seconds"] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_time_grant_finishes_pending_bootstrap_without_repeating_searched_indexes(
    tmp_path: Path,
) -> None:
    class SlowInitialSource(Source):
        def __init__(self) -> None:
            super().__init__()
            self.attempted: list[str] = []

        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": index, "name": index} for index in ("a", "b", "c", "d")]

        async def search(self, **kwargs: Any) -> list[Evidence]:
            index = kwargs["index_id"]
            self.attempted.append(index)
            if index in {"a", "b"}:
                await asyncio.sleep(0.25)
            return [Evidence(id=index, title=index, text="Verified", index_id=index)]

    model = Model([request(timeout_seconds=2.0, model_calls=6), ANSWER])
    runtime = build(tmp_path, model, timeout_seconds=0.5, parallelism=1)
    source = SlowInitialSource()
    runtime.source = source
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(question="Explain", mode="autonomous", index_ids=["a", "b", "c", "d"]),
        )
        run = await finished(runtime, submitted["id"])
        assert run["budget"]["extensions"] == 1
        assert run["budget"]["current"]["timeout_seconds"] == 2.0
        assert source.attempted == ["a", "b", "c", "d"]
        assert run["coverage"]["a"]["status"] == "searched"
        assert run["coverage"]["c"]["status"] == "searched"
        assert run["coverage"]["d"]["status"] == "searched"
        assert all(item["status"] != "pending" for item in run["coverage"].values())
        assert run["metrics"]["bootstrap_calls"] == 4
        assert run["metrics"]["source_calls"] == 4
        assert run["answer"] == ANSWER["content"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_policy_edit_does_not_replace_active_run_ceiling(tmp_path: Path) -> None:
    model = Model([request(model_calls=999), ANSWER])
    model.block_first = True
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        await asyncio.wait_for(model.entered.wait(), 2)
        edited = runtime.control_store.load()
        for profile in edited.profiles["autonomous"].values():
            profile.model_calls = 3
        edited.autonomy.caps.model_calls = 3
        runtime.control_store.save(edited, expected_revision=edited.revision)
        model.release.set()
        run = await finished(runtime, submitted["id"])
        assert run["budget"]["settings_revision"] == 1
        assert run["budget"]["current"]["model_calls"] == 6
        second = await runtime.submit(
            OWNER,
            RunRequest(
                question="Next",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        next_run = await finished(runtime, second["id"])
        assert next_run["budget"]["settings_revision"] == 2
        assert next_run["budget"]["ceiling"]["model_calls"] == 3
    finally:
        model.release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_provider_invocations_obey_separate_summary_and_decision_caps(tmp_path: Path) -> None:
    model = Model([ANSWER])
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        policy = runtime.control_store.load()
        run: dict[str, Any] = {
            "id": "counter-test",
            "mode": "autonomous",
            "effort": "medium",
            "metrics": {"model_calls": 0, "source_calls": 0, "tool_calls": 0},
            "evidence": [],
            "warnings": [],
        }
        runtime._initialize_budget(run, policy)
        messages = [{"role": "user", "content": "Small prompt"}]
        for _ in range(4):
            await runtime._invoke_model(OWNER, run, Event(), messages, [])
        with pytest.raises(ModelDecisionError):
            await runtime._invoke_model(OWNER, run, Event(), messages, [])
        grant = json.loads(
            runtime._change_budget(
                run, request(model_calls=6)["tool_calls"][0]["args"], started=runtime._now(run)
            )
        )
        assert grant["accepted"] is True
        for _ in range(2):
            await runtime._invoke_model(OWNER, run, Event(), messages, [])
        with pytest.raises(ModelDecisionError):
            await runtime._invoke_model(OWNER, run, Event(), messages, [])
        for _ in range(2):
            await runtime._invoke_model(OWNER, run, Event(), messages, [], summary=True)
        with pytest.raises(ModelDecisionError):
            await runtime._invoke_model(OWNER, run, Event(), messages, [], summary=True)
        assert len(model.requests) == 8
        assert run["metrics"]["model_calls"] == 8
        assert run["metrics"]["decision_calls"] == 6
        assert run["metrics"]["summary_calls"] == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_low_initial_budget_triggers_model_review_before_investigation(
    tmp_path: Path,
) -> None:
    model = Model([request(model_calls=6), READ, ANSWER])
    runtime = build(tmp_path, model, model_calls=2)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "completed"
        assert [tool["function"]["name"] for tool in model.requests[0]["tools"]] == [
            "request_budget"
        ]
        assert run["budget"]["initial"]["model_calls"] == 2
        assert run["budget"]["current"]["model_calls"] == 6
        assert run["metrics"]["budget_requests"] == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_one_call_budget_preserves_final_answer_without_ungranted_extra_call(
    tmp_path: Path,
) -> None:
    model = Model([ANSWER])
    runtime = build(tmp_path, model, model_calls=1)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Explain", mode="autonomous", index_ids=["a"])
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial"
        assert run["answer"] == ANSWER["content"]
        assert run["budget"]["extensions"] == 0
        assert run["budget"]["current"]["model_calls"] == 1
        assert run["metrics"]["decision_calls"] == 1
        assert len(model.requests) == 1
        assert not model.requests[0]["tools"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_malformed_governor_tool_call_still_produces_reserved_final_answer(
    tmp_path: Path,
) -> None:
    model = Model([{"content": "", "tool_calls": ["invalid"]}, ANSWER])
    runtime = build(tmp_path, model, model_calls=2)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER, RunRequest(question="Explain", mode="autonomous", index_ids=["a"])
        )
        run = await finished(runtime, submitted["id"])
        assert run["answer"] == ANSWER["content"]
        assert run["metrics"]["decision_calls"] == 2
        assert not any("AttributeError" in warning for warning in run["warnings"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_source_cap_stops_excessive_model_tool_batch(tmp_path: Path) -> None:
    model = Model(
        [
            request(source_calls=999, tool_calls=999),
            {
                "content": "",
                "tool_calls": [
                    {
                        "name": "search_knowledge",
                        "args": {"index_id": "a", "query": f"dependency-{n}"},
                    }
                    for n in range(20)
                ],
            },
            ANSWER,
        ]
    )
    runtime = build(tmp_path, model)
    source = runtime.source
    assert isinstance(source, Source)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["metrics"]["source_calls"] - run["metrics"]["bootstrap_calls"] == 5
        assert len(source.queries) == 6
        assert run["budget"]["current"]["source_calls"] == 5
        assert run["stop_reason"] == "budget_ceiling"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_maximum_extension_count_still_applies_after_new_evidence(tmp_path: Path) -> None:
    first = request(model_calls=6)
    first["tool_calls"].append(
        {
            "name": "search_knowledge",
            "args": {
                "index_id": "a",
                "query": "new dependency",
            },
        }
    )
    model = Model([first, request(source_calls=5), ANSWER])
    runtime = build(tmp_path, model)
    policy = runtime.control_store.load()
    policy.autonomy.max_extensions = 1
    runtime.control_store.save(policy, expected_revision=policy.revision)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert [item["accepted"] for item in run["budget"]["events"]] == [True, False]
        assert run["budget"]["current"]["source_calls"] == 2
        assert "число расширений" in run["budget"]["events"][-1]["message"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_budget_explanations_follow_source_access_redaction(tmp_path: Path) -> None:
    secret = "PRIVATE_SOURCE_FINDING"
    expansion = request(model_calls=6)
    expansion["tool_calls"][0]["args"]["reason"] = "Verify details from " + secret
    model = Model([expansion, ANSWER])
    runtime = build(tmp_path, model)
    source = runtime.source
    assert isinstance(source, Source)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert secret in json.dumps(run)
        source.available = False
        redacted = await runtime.get(OWNER, submitted["id"])
        assert secret not in json.dumps(redacted)
        assert not redacted["budget"]["events"]
        assert secret in json.dumps(runtime.store.get(OWNER.owner_id, submitted["id"]))
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_budget_request_cannot_change_source_permissions(tmp_path: Path) -> None:
    model = Model([request(model_calls=6, allowed_indexes=["private"]), ANSWER])
    runtime = build(tmp_path, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(
            OWNER,
            RunRequest(
                question="Explain",
                mode="autonomous",
                index_ids=["a"],
            ),
        )
        run = await finished(runtime, submitted["id"])
        assert run["status"] == "partial"
        assert run["index_ids"] == ["a"]
        assert run["budget"]["extensions"] == 0
        assert {item["index_id"] for item in run["evidence"]} == {"a"}
        assert any("недопустимый" in warning for warning in run["warnings"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_autopilot_rechecks_early_ready_claim_and_accepts_confirmed_same_document(
    tmp_path: Path,
) -> None:
    premature = {"content": "Early summary of the initial sources.", "tool_calls": []}
    model = Model([PLAN_READY, premature, READ, ANSWER])
    runtime = build(tmp_path, model, model_calls=6, document_reads=1, source_calls=4)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode="autonomous", index_ids=["a"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["answer"] == ANSWER["content"]
        assert result["status"] == "completed", result["warnings"]
        # Both the automatic inspection and the later model-directed verification
        # return exactly the same id and body. Confirmation need not add a new source.
        assert result["metrics"]["document_reads"] == 2
        assert [item["id"] for item in result["evidence"]] == ["Explain"]
        assert result["evidence"][0]["checked_with"] == "read_document"
        assert any(step["name"] == "goal_check" for step in result["research_trace"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", [
    None, "mcp_list_tools", "mcp_tool_schema", "clone_repository", "mcp_call",
])
async def test_plan_and_preparatory_tools_cannot_complete_bootstrap_only_research(
    tmp_path: Path, operation: str | None,
) -> None:
    external_id = "repo:service" if operation == "clone_repository" else "connection:jira"
    actions: list[str] = []

    class PreparationTools:
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": external_id, "name": "Selected connection"}]

        async def describe(
            self, principal: Principal, index_ids: list[str],
        ) -> list[dict[str, Any]]:
            return [{"type": "function", "function": {
                "name": operation, "parameters": {"type": "object"},
            }}] if operation else []

        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
            actions.append(name)
            if name == "mcp_call":
                return []  # The backend was called but returned no findings.
            return [Evidence(
                id=name, index_id=external_id, title="Preparation", text="Available.",
                # The repository adapter returns the checked-out ref as a source.
                kind="source" if name == "clone_repository" else "tool_metadata",
            )]

        async def close_run(self, run_id: str) -> None:
            pass

    decisions = [PLAN_READY]
    if operation:
        decisions[0] = {"content": "", "tool_calls": [
            {"name": operation, "args": {}}, *PLAN_READY["tool_calls"],
        ]}
    model = Model([*decisions, ANSWER])
    runtime = build(tmp_path, model, model_calls=6, document_reads=1, source_calls=4)
    runtime.tools = PreparationTools()
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode="autonomous", index_ids=["a", external_id],
        ))
        result = await finished(runtime, submitted["id"])
        assert actions == ([operation] if operation else [])
        assert any(step["name"] == "update_research_plan" for step in result["research_trace"])
        assert result["metrics"]["document_reads"] == 1
        assert not any(item.get("checked_with") for item in result["evidence"])
        assert result["answer"] and result["status"] == "partial"
        assert result["warnings"]
    finally:
        await runtime.stop()


async def test_repeating_initial_search_is_not_an_autopilot_followup(tmp_path: Path) -> None:
    replay = {"name": "search_knowledge", "args": {"index_id": "a", "query": "Explain"}}
    model = Model([{"content": "", "tool_calls": [replay, *PLAN_READY["tool_calls"]]}, ANSWER])
    runtime = build(tmp_path, model, model_calls=6)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode="autonomous", index_ids=["a"],
        ))
        result = await finished(runtime, submitted["id"])
        assert isinstance(runtime.source, Source)
        assert runtime.source.queries == ["Explain", "Explain"]
        assert result["status"] == "partial"
        assert not any(item.get("checked_with") for item in result["evidence"])
    finally:
        await runtime.stop()
