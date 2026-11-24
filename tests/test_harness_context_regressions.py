"""Large source sets must preserve investigated detail, tool contracts and ACL boundaries."""

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.policy import TOOLS
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import timestamp

OWNER = Principal("review-owner")


class Source:
    def __init__(self, allowed: list[str]) -> None:
        self.allowed = allowed

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": item, "name": item} for item in self.allowed]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        index_id = kwargs["index_id"]
        return [
            Evidence(id=f"{index_id}-doc", title="Payment", text="Current state", index_id=index_id)
        ]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(kwargs)
        envelope = {"messages": kwargs["messages"], "tools": kwargs["tools"]}
        assert len(json.dumps(envelope, ensure_ascii=False)) <= kwargs["max_input_chars"]
        return {"content": "Answer based on current evidence.", "tool_calls": []}


def record(app: HarnessRuntime, indexes: list[str]) -> dict[str, Any]:
    data = {
        "id": "context-review",
        **RunRequest(question="payment systems", mode="investigate", effort="high").model_dump(),
        "index_ids": indexes,
        "status": "running",
        "phase": "thinking",
        "created_at": timestamp(),
        "answer": "",
        "evidence": [],
        "warnings": [],
        "context_index_ids": [],
        "research_trace": [],
        "memory_used": [],
        "metrics": {
            "source_calls": 0,
            "bootstrap_calls": 0,
            "model_calls": 0,
            "tool_calls": 0,
            "duration_ms": 0,
        },
    }
    app.store.save(OWNER.owner_id, data)
    return data


async def finished(app: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(5):
        while True:
            result = await app.get(OWNER, run_id)
            if result["status"] not in {"queued", "running", "cancelling"}:
                return result
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_thousand_fragments_leave_room_for_a_fully_read_document(tmp_path: Path) -> None:
    indexes = [f"i{i:04d}" for i in range(1000)]
    model = Model()
    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), Source(indexes), model
    )
    await app.start()
    try:
        run = record(app, indexes)
        document_text = "payment " + "x" * 31_950 + " DEEP_VERIFIED_CAUSE_AT_DOCUMENT_END"
        for i, index in enumerate(indexes):
            assert app._add_evidence(
                run,
                Evidence(
                    id=f"doc-{i}",
                    title="payment systems",
                    index_id=index,
                    text=document_text if i == 0 else "payment " + "x" * 3990,
                ),
                index,
                document=i == 0,
            )
        await app._decide(OWNER, run, Event(), TOOLS)
        context = json.loads(model.requests[-1]["messages"][-1]["content"])
        investigated = next(item for item in context["evidence"] if item["id"] == "doc-0")
        assert investigated["text"] == document_text
        assert not investigated.get("context_truncated")
        assert len({item["index_id"] for item in context["evidence"]}) > 1
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_complete_mcp_schema_survives_a_thousand_rag_fragments(tmp_path: Path) -> None:
    indexes = [f"i{i:04d}" for i in range(999)] + ["connection:remote"]
    model = Model()
    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, max_evidence=1500, _env_file=None),
        Source(indexes),
        model,
    )
    await app.start()
    try:
        run = record(app, indexes)
        for i in range(1000):
            index = indexes[i % 999]
            assert app._add_evidence(
                run,
                Evidence(
                    id=f"doc-{i}",
                    title="payment systems",
                    index_id=index,
                    text="payment " + "x" * 3990,
                ),
                index,
            )
        schema = {
            "name": "jira_search",
            "description": "z" * 6000,
            "inputSchema": {
                "type": "object",
                "required": ["exact_queue_id"],
                "properties": {
                    "exact_queue_id": {
                        "type": "string",
                        "enum": ["SENTINEL_REQUIRED_VALUE_AT_TAIL"],
                    }
                },
            },
        }
        schema_text = json.dumps(schema)
        assert app._add_evidence(
            run,
            Evidence(
                id="required-tool-schema",
                title="Tool contract",
                index_id="connection:remote",
                text=schema_text,
                kind="tool_metadata",
            ),
            "connection:remote",
        )
        await app._decide(OWNER, run, Event(), TOOLS)
        context = json.loads(model.requests[-1]["messages"][-1]["content"])
        metadata = next(
            item for item in context["evidence"] if item["id"] == "required-tool-schema"
        )
        assert json.loads(metadata["text"]) == schema
        assert not metadata.get("context_truncated")
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_late_tool_result_replaces_old_same_index_evidence_at_capacity(
    tmp_path: Path,
) -> None:
    class Backend:
        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
            return [
                Evidence(
                    id="fresh-result",
                    index_id="connection:remote",
                    title="New result",
                    text="Verified incident outcome",
                )
            ]

    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, max_evidence=2, _env_file=None)
    )
    await app.start()
    try:
        run = record(app, ["knowledge", "connection:remote"])
        for key, index in (("doc", "knowledge"), ("old-schema", "connection:remote")):
            assert app._add_evidence(
                run, Evidence(id=key, index_id=index, title=key, text=key), index
            )
        app.tools = Backend()  # type: ignore[assignment]
        result = await app._external_tool(
            OWNER, run, Event(), "mcp_call", {}, asyncio.get_running_loop().time() + 5
        )
        assert result == "Получено подтверждений: 1."
        assert {item["id"] for item in run["evidence"]} == {"doc", "fresh-result"}
    finally:
        app.tools = None
        await app.stop()


def test_get_after_access_revocation_hides_trace_and_context_metadata(tmp_path: Path) -> None:
    from starlette.requests import Request
    from starlette.testclient import TestClient

    from agent_harness.http import create_app

    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
        Source(["public"]),
        Model(),
    )

    async def authenticate(request: Request) -> Principal:
        return OWNER

    with TestClient(create_app(app, authenticate)) as client:
        run = record(app, ["private", "public"])
        run.update(
            status="completed",
            evidence=[
                Evidence(
                    id="private-doc", index_id="private", title="Private", text="Secret"
                ).model_dump()
            ],
            research_trace=[
                {
                    "name": "read_document",
                    "args": {"index_id": "private", "document_id": "SECRET_DOCUMENT_ID"},
                    "result": "Document read",
                }
            ],
            coverage={"private": {"status": "searched", "count": 1}},
            context_summary={"evidence_included": 1},
            answer_kind="verified_excerpt", answer="SECRET_ANSWER [private-doc]",
            _retrieval_support={"id": "private-doc", "index_id": "private",
                                "quote": "SECRET_ANSWER"},
        )
        app.store.save(OWNER.owner_id, run)
        response = client.get(f"/api/runs/{run['id']}")
        assert response.status_code == 200
        visible = response.json()
        assert visible["evidence"] == [] and visible["answer"] == ""
        assert visible.get("research_trace", []) == []
        assert visible.get("coverage", {}) == {}
        assert visible.get("context_summary", {}) == {}
        assert "SECRET_DOCUMENT_ID" not in response.text and "SECRET_ANSWER" not in response.text


@pytest.mark.asyncio
async def test_resume_in_narrower_scope_drops_diagnostics_from_revoked_sources(
    tmp_path: Path,
) -> None:
    source, model = Source(["private", "public"]), Model()
    app = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), source, model
    )
    await app.start()
    try:
        submitted = await app.submit(
            OWNER, RunRequest(question="payment", mode="investigate", index_ids=source.allowed)
        )
        run = await finished(app, submitted["id"])
        run.update(
            status="partial",
            research_trace=[
                {
                    "name": "read_document",
                    "args": {"index_id": "private", "document_id": "SECRET_OLD_DOCUMENT"},
                    "result": "Document read",
                }
            ],
            coverage={"private": {"status": "searched", "count": 1}},
            context_summary={"evidence_included": 1},
        )
        app.store.save(OWNER.owner_id, run)
        source.allowed = ["public"]
        previous_calls = len(model.requests)
        resumed = await app.resume(OWNER, run["id"], ["public"])
        assert resumed.get("research_trace", []) == []
        assert resumed.get("coverage", {}) == {}
        assert resumed.get("context_summary", {}) == {}
        await finished(app, run["id"])
        assert len(model.requests) > previous_calls
        for request in model.requests[previous_calls:]:
            assert "SECRET_OLD_DOCUMENT" not in json.dumps(request["messages"])
    finally:
        await app.stop()
