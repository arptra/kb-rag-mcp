"""DeepSeek deployment defaults preserve operator limits and complete slow research."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.control.settings import ControlStore
from agent_harness.model_connection import (
    ModelConnection,
    ModelConnectionStore,
    ModelTransientError,
)
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("alice")


def connection(path: Path, **values: Any) -> None:
    ModelConnectionStore(path).save(ModelConnection(
        provider="openai", base_url="https://model.example.test/v1",
        model="deepseek-v4-flash", **values,
    ))


def settings(path: Path, **values: Any) -> HarnessSettings:
    return HarnessSettings(_env_file=None, enabled=True, state_dir=path, **values)


def test_deepseek_defaults_apply_without_restart_and_migrate_only_untouched_values(
    tmp_path: Path,
) -> None:
    store = ControlStore(settings(tmp_path))
    generic = store.load()
    generic.runtime.source_timeout_seconds = 41.0
    generic.profiles["autonomous"]["medium"].timeout_seconds = 333.0
    store.save(generic, expected_revision=0)
    connection(tmp_path)
    deepseek = store.load()
    assert deepseek.model_profile == "deepseek_v4_flash"
    assert deepseek.revision == 1
    assert deepseek.context.window_tokens == 256_000
    assert deepseek.context.response_reserve_tokens == 32_768
    assert deepseek.profiles["quick"]["medium"].timeout_seconds == 3900
    assert deepseek.profiles["autonomous"]["medium"].timeout_seconds == 333
    assert deepseek.profiles["autonomous"]["low"].model_calls == 12
    assert deepseek.runtime.source_timeout_seconds == 41
    assert deepseek.runtime.final_reserve_seconds == 3600
    assert deepseek.runtime.answer_chars == 200_000
    # Saving an explicit small value after migration must not upgrade it again.
    deepseek.context.response_reserve_tokens = 4096
    store.save(deepseek, expected_revision=1)
    assert store.load().context.response_reserve_tokens == 4096
    ModelConnectionStore(tmp_path).save(ModelConnection())
    returned = store.load()
    assert returned.model_profile == "generic"
    assert returned.profiles["quick"]["medium"].timeout_seconds == 120
    assert returned.runtime.source_timeout_seconds == 41


def test_deepseek_respects_explicit_environment_limits_and_operator_ceiling(tmp_path: Path) -> None:
    configured = settings(tmp_path, quick_timeout_seconds=55.0,
                          investigate_context_chars=180_000, final_reserve_seconds=20.0)
    store = ControlStore(configured)
    custom = store.load()
    custom.autonomy.caps.model_calls = 20
    store.save(custom, expected_revision=0)
    connection(tmp_path)
    migrated = store.load()
    assert migrated.profiles["quick"]["medium"].timeout_seconds == 55
    assert migrated.profiles["autonomous"]["high"].context_tokens == 60_000
    assert migrated.profiles["autonomous"]["high"].model_calls == 20
    assert migrated.runtime.final_reserve_seconds == 20


class Source:
    def __init__(self) -> None:
        self.searches: list[str] = []
        self.reads: list[str] = []

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "docs", "name": "Docs"}]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        self.searches.append(kwargs["query"])
        return [Evidence(id="payment", title="Payment", index_id="docs",
                         text="Follow ledger acknowledgement for payment status.")]

    async def read(self, **kwargs: Any) -> Evidence:
        self.reads.append(kwargs["document_id"])
        return Evidence(id=kwargs["document_id"], title="Ledger", index_id="docs",
                        text="Verified transaction detail. " * 3000)


def plan(ready: bool = False) -> dict[str, Any]:
    return {"name": "update_research_plan", "args": {
        "goal": "Explain payments", "questions": ["Check ledger"],
        "completed": ["Ledger verified [ledger]"] if ready else [],
        "gaps": [] if ready else ["Read ledger"],
        "next_action": "Answer" if ready else "Read source", "ready_to_answer": ready,
    }}


async def finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            result = runtime.store.get(OWNER.owner_id, run_id)
            if result["status"] not in {"queued", "running", "cancelling"}:
                return result
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_slow_autopilot_plans_searches_reads_and_finishes_with_full_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection(tmp_path)
    elapsed = 0.0
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[dict[str, Any]] = []
    answer = "## Итог\n\n" + "Проверенные данные [ledger]. " * 2300
    decisions = [
        {"content": "", "tool_calls": [plan(), {"name": "search_knowledge", "args": {
            "query": "ledger acknowledgement", "index_id": "docs",
        }}]},
        {"content": "", "tool_calls": [{"name": "read_document", "args": {
            "document_id": "ledger", "index_id": "docs",
        }}]},
        {"content": "", "tool_calls": [plan(True)]},
        {"content": answer, "tool_calls": [], "finish_reason": "stop"},
    ]

    class SlowModel:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal elapsed
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            await asyncio.sleep(0.01)
            elapsed += 900  # 15-minute upstream delay, without a wall-clock wait.
            return decisions[len(calls) - 1]

    source = Source()
    runtime = HarnessRuntime(settings(tmp_path), source, SlowModel())
    monkeypatch.setattr(runtime, "_now", lambda run: time.monotonic() + elapsed)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain payments", mode="autonomous", effort="low", index_ids=["docs"],
        ))
        await asyncio.wait_for(entered.wait(), 2)
        live = runtime.store.get(OWNER.owner_id, submitted["id"])
        assert live["current_action"]["name"] == "model_response"
        assert live["current_action"]["started_at"]
        assert live["status"] == "running" and not live["answer"]
        release.set()
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == answer.strip() and len(answer) > 50_000
        assert result["current_action"] is None
        assert result["research_plan"]["ready_to_answer"] is True
        assert "ledger acknowledgement" in source.searches
        assert "ledger" in source.reads
        context = json.loads(calls[-1]["messages"][-1]["content"])
        ledger = next(item for item in context["evidence"] if item["id"] == "ledger")
        assert not ledger.get("context_truncated")
        assert ledger["text"] == "Verified transaction detail. " * 3000
        assert result["budget"]["initial"]["context_tokens"] + 32_768 < 256_000
        assert result["metrics"]["decision_calls"] == 4
    finally:
        release.set()
        await runtime.stop()


@pytest.mark.asyncio
async def test_incomplete_plan_is_not_accepted_as_completed_answer(tmp_path: Path) -> None:
    connection(tmp_path)
    stages: list[str] = []

    class PlanningOnly:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            context = json.loads(kwargs["messages"][-1]["content"])
            stages.append(context["stage"])
            if len(stages) == 1:
                return {"content": "", "tool_calls": [plan()]}
            return {"content": ("План: проверю ledger." if kwargs["tools"]
                                else "Исходные материалы найдены; ledger не проверен [payment]."),
                    "tool_calls": [], "finish_reason": "stop"}

    runtime = HarnessRuntime(settings(tmp_path), Source(), PlanningOnly())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode="autonomous", index_ids=["docs"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "partial"
        assert stages[-1] == "synthesizing"
        assert result["answer"].startswith("Исходные материалы")
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "length", "max_tokens", "insufficient_system_resource", "aborted",
])
async def test_slow_incomplete_response_gets_a_complete_final_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    connection(tmp_path)
    elapsed = 0.0
    calls = 0

    class RecoveringModel:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal elapsed, calls
            calls += 1
            await asyncio.sleep(0.01)
            elapsed += 1700
            if calls == 1:
                return {"content": "Незаконченный ответ", "tool_calls": [], "finish_reason": reason}
            return {"content": "Законченный ответ [payment].", "tool_calls": [],
                    "finish_reason": "stop"}

    runtime = HarnessRuntime(settings(tmp_path), Source(), RecoveringModel())
    monkeypatch.setattr(runtime, "_now", lambda run: time.monotonic() + elapsed)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Explain", index_ids=["docs"]))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == "Законченный ответ [payment]."
        assert result["metrics"]["answer_retries"] == 1 and calls == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [False, True])
async def test_prose_plan_requires_research_before_autopilot_can_finish(
    tmp_path: Path, recovers: bool,
) -> None:
    connection(tmp_path)
    calls = 0

    class ProseFirst:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1 or not recovers:
                return {"content": "Сейчас я проверю код, после этого исследую зависимости.",
                        "tool_calls": []}
            if calls == 2:
                return {"content": "", "tool_calls": [plan(), {
                    "name": "read_document", "args": {
                        "index_id": "docs", "document_id": "ledger",
                    },
                }]}
            if calls == 3:
                return {"content": "", "tool_calls": [plan(True)]}
            return {"content": "Проверено: платёж ждёт подтверждения ledger [ledger].",
                    "tool_calls": []}

    runtime = HarnessRuntime(settings(tmp_path), Source(), ProseFirst())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode="autonomous", index_ids=["docs"],
        ))
        result = await finished(runtime, submitted["id"])
        if recovers:
            assert result["status"] == "completed", result["warnings"]
            assert result["answer"].startswith("Проверено")
            assert calls == 4
        else:
            assert result["status"] == "partial"
            assert calls == 3
            assert "Модель не подтвердила завершение исследования." in result["warnings"]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "investigate"])
async def test_transient_failure_recovers_without_replaying_actions_or_partial_status(
    tmp_path: Path, mode: str,
) -> None:
    connection(tmp_path)
    calls = 0

    class TransientModel:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelTransientError("API модели временно недоступен.")
            assert not kwargs["tools"]
            return {"content": "Законченный ответ [payment].", "tool_calls": []}

    source = Source()
    runtime = HarnessRuntime(settings(tmp_path), source, TransientModel())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Explain", mode=mode, index_ids=["docs"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert calls == 2
        assert source.searches == ["Explain"]
        assert any(step["name"] == "model_retry" for step in result["research_trace"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_exhausted_transient_retry_keeps_partial_text_and_failure_reason(
    tmp_path: Path,
) -> None:
    connection(tmp_path)
    calls = 0

    class PartialThenUnavailable:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"content": "Сохранённая часть ответа [payment].", "tool_calls": [],
                        "finish_reason": "length"}
            raise ModelTransientError("API модели временно недоступен.")

    runtime = HarnessRuntime(settings(tmp_path), Source(), PartialThenUnavailable())
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(question="Explain", index_ids=["docs"]))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "partial"
        assert result["answer"].startswith("Сохранённая часть")
        assert "API модели временно недоступен." in result["warnings"]
        assert calls == 2
    finally:
        await runtime.stop()
