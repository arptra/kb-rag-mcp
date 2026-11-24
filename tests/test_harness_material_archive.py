"""A real Deep Agents StateBackend finds omitted text inside the bounded Autopilot loop."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_harness_autonomy import OWNER, Model, build, finished

from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.material_archive import search_materials

pytest.importorskip("deepagents", minversion="0.7.39")
MARKER = "payment.dispatch.backoff"


@pytest.mark.asyncio
async def test_archive_offsets_and_scope_are_checked_against_the_original_text() -> None:
    archive = {"mode": "autonomous", "research_plan": {"current_branch": {"number": 1}},
               "index_ids": ["ok", "revoked"], "evidence": []}
    body = "первая строка\r\n" + "строка" * 1000 + MARKER + " = 17 seconds"
    for index, doc_id, text in [("ok", "kept", body), ("revoked", "secret", MARKER),
                                ("elsewhere", "unselected", MARKER)]:
        archive["evidence"].append({"index_id": index, "id": doc_id, "text": text})
    matches = await search_materials(archive, {"query": MARKER}, {"ok", "elsewhere"})
    assert len(matches) == 1
    hit = matches[0]
    assert (hit["index_id"], hit["id"], hit["line"]) == ("ok", "kept", 2)
    assert MARKER in hit["excerpt"] == body[hit["offset"]:hit["end_offset"]]
    assert len(hit["excerpt"]) <= 260


@pytest.mark.asyncio
async def test_autopilot_searches_retained_material_then_verifies_its_source(
    tmp_path: Path,
) -> None:
    indexes = [f"idx-{n}" for n in range(10)]
    document = "default = 1\n" * 300 + MARKER + " applies to legacy tenants."

    class Sources:
        def __init__(self) -> None:
            self.reads: list[str] = []

        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": idx, "name": idx} for idx in indexes]

        async def search(self, *, index_id: str, **kwargs: Any) -> list[Evidence]:
            return [Evidence(id="doc-" + index_id, index_id=index_id, title="Config",
                             text=document if index_id == indexes[-1] else "No relevant entries.")]

        async def read(self, *, index_id: str, document_id: str, **kwargs: Any) -> Evidence:
            self.reads.append(document_id)
            return Evidence(id=document_id, index_id=index_id, title="Config",
                            text=MARKER + " applies to legacy tenants; interval = 17 seconds.")

    class Reader(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            context = json.loads(kwargs["messages"][-1]["content"])
            tools = [item["function"]["name"] for item in kwargs["tools"]]
            position = len(self.requests)
            if position == 1:
                assert "search_collected_materials" not in tools
                return {"content": "Сначала проверю настройки.", "tool_calls": []}
            if position == 2:
                assert "search_collected_materials" in tools
                return {"content": "", "tool_calls": [
                    {"name": "search_collected_materials", "args": {"query": MARKER}},
                ]}
            if position == 3:
                match, = context["archive_matches"]
                assert match["id"] == "doc-idx-9" and MARKER in match["excerpt"]
                assert not any(item.get("checked_with") for item in context["evidence"])
                return {"content": "", "tool_calls": [{"name": "read_document", "args": {
                    "index_id": match["index_id"], "document_id": match["id"],
                }}]}
            assert position == 4
            return {"content": "Интервал составляет 17 секунд [doc-idx-9].", "tool_calls": []}

    source = Sources()
    model = Reader([])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, timeout_seconds=3,
                    evidence_chars=8000)
    # Retain the full search excerpt long enough for the local exact lookup.
    settings = runtime.control_store.load()
    settings.runtime.evidence_chars = 8000
    runtime.control_store.save(settings, expected_revision=settings.revision)
    runtime.source = source
    await runtime.start()
    try:
        requested = await runtime.submit(OWNER, RunRequest(
            question="Какой интервал повторной отправки?", mode="autonomous", index_ids=indexes,
        ))
        result = await finished(runtime, requested["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == "Интервал составляет 17 секунд [doc-idx-9]."
        assert source.reads == ["doc-idx-9"] and result["metrics"]["source_calls"] == 11
        assert result["metrics"]["model_calls"] == 4
        assert sum(item.get("checked_with") == "read_document" for item in result["evidence"]) == 1
        assert not any(item.get("checked_with") == "search_collected_materials"
                       for item in result["evidence"])
        assert runtime._graph
        graph = runtime._graph
        snapshot = await graph.graph.aget_state(graph.config(OWNER, requested["id"]))
        assert "files" not in snapshot.values
    finally:
        await runtime.stop()
