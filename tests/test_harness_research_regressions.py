"""Research must cover the selected scope and preserve usable evidence for the model."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("research-regression-user")


class ResearchSource:
    def __init__(self, count: int = 1, *, quoted: bool = False, long_read: bool = False) -> None:
        self.ids = [f"index-{number:03d}" for number in range(count)]
        self.quoted = quoted
        self.long_read = long_read
        self.searches: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    async def indexes(self, _principal: Principal) -> list[dict[str, str]]:
        return [{"id": index_id, "name": index_id} for index_id in self.ids]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        self.searches.append(kwargs)
        text = (
            '{"service": "payments", "path": "src/main/java/Payment.java"}\n' * 40
            if self.quoted
            else "Search excerpt: the payment service applies an account limit."
        )
        return [
            Evidence(
                id=f"document-{kwargs['index_id']}-{number}",
                title=f"Payment rules {number}",
                text=text,
                index_id=kwargs["index_id"],
            )
            for number in range(kwargs["limit"])
        ]

    async def read(self, **kwargs: Any) -> Evidence:
        self.reads.append(kwargs)
        text = (
            "Implementation detail. " * 320 + "TAIL_MARKER: verified transaction rollback behavior."
            if self.long_read
            else "DOCUMENT_VERIFIED: the account limit is checked before reserving the amount."
        )
        return Evidence(
            id=kwargs["document_id"],
            title="Current implementation",
            text=text,
            index_id=kwargs["index_id"],
        )


class ResearchModel:
    def __init__(self, source: ResearchSource, *, tool: str | None = None) -> None:
        self.source = source
        self.tool = tool
        self.requests: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True, "state": "ready"}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append({
            "messages": copy.deepcopy(kwargs["messages"]),
            "tools": copy.deepcopy(kwargs["tools"]),
            "max_input_chars": kwargs["max_input_chars"],
            "searched_indexes": {item["index_id"] for item in self.source.searches},
            "read_count": len(self.source.reads),
        })
        if len(self.requests) == 1 and self.tool and kwargs["tools"]:
            args = (
                {"index_id": self.source.ids[0], "query": "follow-up detail", "limit": 3}
                if self.tool == "search_knowledge"
                else {
                    "index_id": self.source.ids[0],
                    "document_id": f"document-{self.source.ids[0]}-0",
                }
            )
            return {"content": "", "tool_calls": [{"name": self.tool, "args": args}]}
        context = json.loads(kwargs["messages"][-1]["content"])
        source = context["evidence"][0]
        return {"content": f"FINAL: {source['text'][:90]} [{source['id']}]", "tool_calls": []}


def settings(tmp_path: Path) -> HarnessSettings:
    return HarnessSettings(
        _env_file=None,
        enabled=True,
        state_dir=tmp_path,
        quick_timeout_seconds=10,
        investigate_timeout_seconds=20,
        final_reserve_seconds=2,
    )


async def finished(runtime: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(15):
        while True:
            run = await runtime.get(OWNER, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "investigate"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
async def test_every_selected_index_is_searched_at_every_effort(
    tmp_path: Path, mode: str, effort: str,
) -> None:
    source = ResearchSource(42)
    model = ResearchModel(source)
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest.model_validate({
            "question": "Как сейчас работает система платежей?",
            "mode": mode,
            "effort": effort,
            "index_ids": source.ids,
        }))
        run = await finished(runtime, submitted["id"])
        assert {item["index_id"] for item in source.searches} == set(source.ids)
        assert model.requests and model.requests[0]["searched_indexes"] == set(source.ids)
        assert run["answer"].startswith("FINAL:"), run
        assert not any("Часть индексов не опрошена" in warning for warning in run["warnings"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_initial_coverage_does_not_consume_follow_up_tool_budget(tmp_path: Path) -> None:
    source = ResearchSource(42)
    model = ResearchModel(source, tool="search_knowledge")
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Исследуй правила обработки платежей и проверь детали.",
            mode="investigate", effort="low", index_ids=source.ids,
        ))
        run = await finished(runtime, submitted["id"])
        assert model.requests and model.requests[0]["tools"], run
        assert any(item["query"] == "follow-up detail" for item in source.searches), run
        assert len(model.requests) >= 2
        assert run["answer"].startswith("FINAL:"), run
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("effort", ["medium", "high"])
async def test_quoted_code_evidence_fits_the_serialized_model_request(
    tmp_path: Path, effort: str,
) -> None:
    source = ResearchSource(24, quoted=True)
    model = ResearchModel(source)
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest.model_validate({
            "question": "Разбери конфигурацию сервисов и код обработки платежей.",
            "mode": "investigate", "effort": effort, "index_ids": source.ids,
        }))
        run = await finished(runtime, submitted["id"])
        assert model.requests, run
        for request in model.requests:
            serialized = json.dumps(
                {"messages": request["messages"], "tools": request["tools"]},
                ensure_ascii=False,
            )
            assert len(serialized) <= request["max_input_chars"] - 1500
        assert run["answer"].startswith("FINAL:"), run
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_document_read_preserves_details_beyond_the_search_excerpt(tmp_path: Path) -> None:
    source = ResearchSource(long_read=True)
    model = ResearchModel(source, tool="read_document")
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Проверь полное описание отката транзакции.",
            mode="investigate", effort="low", index_ids=source.ids,
        ))
        run = await finished(runtime, submitted["id"])
        assert source.reads, run
        assert any(
            "TAIL_MARKER" in item["text"] and len(item["text"]) > 6500
            for item in run["evidence"]
        ), run
        assert any(
            "TAIL_MARKER" in message["content"]
            for request in model.requests
            for message in request["messages"]
        ), run
        assert run["answer"].startswith("FINAL:"), run
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_early_model_answer_cannot_skip_document_verification(tmp_path: Path) -> None:
    source = ResearchSource()
    model = ResearchModel(source)
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Исследуй как и когда проверяется лимит счёта.",
            mode="investigate", effort="medium", index_ids=source.ids,
        ))
        run = await finished(runtime, submitted["id"])
        assert model.requests and model.requests[0]["read_count"] > 0, run
        assert any(
            "DOCUMENT_VERIFIED" in message["content"]
            for message in model.requests[0]["messages"]
        ), run
        assert run["answer"].startswith("FINAL:"), run
    finally:
        await runtime.stop()
