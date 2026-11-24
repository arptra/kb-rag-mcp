"""Measure planning requests across tool rounds and the final synthesis."""

import json
from pathlib import Path
from threading import Event

import pytest
from test_harness_context_regressions import OWNER, Model, Source, record
from test_harness_deepseek_runtime import connection

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence
from agent_harness.planning_context import discovery_connection
from agent_harness.policy import TOOLS
from agent_harness.runtime import HarnessRuntime


@pytest.mark.parametrize(("question", "expected"), [
    ("Сколько сервисов в системе?", "connection:cmdb"),
    ("Проверь Jira", "connection:jira"),
    ("Как обновить банковский лимит?", None),
])
def test_discover_only_a_relevant_authorized_mcp(question: str, expected: str | None) -> None:
    indexes = ["docs", "connection:images", "connection:cmdb", "connection:jira", "connection:old"]
    names = {"connection:jira": "MCP · Jira"}
    allowed = {"connection:images": {"get_photograph"}, "connection:cmdb": {"getServiceInventory"},
               "connection:jira": {"find_issue"}, "connection:old": set()}
    assert discovery_connection(question, indexes, names, allowed) == expected


@pytest.mark.asyncio
async def test_planner_sends_focused_results_and_keeps_full_evidence_for_final(
    tmp_path: Path, record_property,
) -> None:
    connection(tmp_path)
    indexes = ["docs", "connection:jira"]
    model = Model()
    app = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
                         Source(indexes), model)
    await app.start()
    try:
        run = record(app, indexes)
        run.update(mode="autonomous", phase="investigating", research_plan={
            "current_branch": {"number": 1, "subquestion": "Check ledgerReceipt", "target": "docs",
                               "tool": "read_document", "reason": "missing result"},
        })
        for number in range(20):
            text = "Stored detail. " * 1200
            if number == 0:
                text += "ledgerReceipt is confirmed in the settlement journal."
            app._add_evidence(run, Evidence(id=str(number), index_id="docs", title="Archive",
                                           text=text), "docs", document=True)
        schema = json.dumps({"name": "find_issue", "description": "x" * 6500,
                             "inputSchema": {"required": ["issue_key"]}})
        app._add_evidence(run, Evidence(id="schema", index_id=indexes[1], kind="tool_metadata",
                                       title="find_issue", text=schema), indexes[1])
        await app._decide(OWNER, run, Event(), TOOLS)

        # A read replaced an old snippet in place. Its position alone cannot
        # distinguish it from unrelated archive entries.
        observation = '{"issue_key":"PAY-403", "status":"observed"}'
        app._add_evidence(run, Evidence(id="1", index_id="docs", title="Status",
                                       text=observation), "docs", document=True,
                          checked_with="read_document")
        match = {"index_id": "docs", "id": "0", "line": 1, "offset": 18000,
                 "end_offset": 18013, "excerpt": "ledgerReceipt"}
        stale = {**match, "index_id": "removed-source", "excerpt": "revoked content"}
        exchange = [{"name": "search_collected_materials", "archive_matches": [match, stale]},
                    {"name": "read_document", "evidence_refs": [["docs", "1"]]}]
        await app._decide(OWNER, run, Event(), TOOLS, exchange)
        context = json.loads(model.requests[-1]["messages"][-1]["content"])
        visible = {item["id"]: item for item in context["evidence"]}
        assert len(visible) <= 8 and context["evidence_total"] == 21
        assert visible["1"]["text"] == observation
        assert visible["schema"]["text"] == schema
        assert "ledgerReceipt" in visible["0"]["text"] and visible["0"]["context_offset"] > 0
        assert context["research_plan"]["current_branch"] == run["research_plan"]["current_branch"]
        assert context["allowed_indexes"] == indexes
        assert context["archive_matches"] == [match]
        assert app._run_scope(run) == set(indexes)

        run["phase"] = "synthesizing"
        app.store.save(OWNER.owner_id, run)
        await app._decide(OWNER, run, Event(), [])
        final = json.loads(model.requests[-1]["messages"][-1]["content"])
        archive = app.store.get(OWNER.owner_id, run["id"])["evidence"]
        assert {e["id"]: e["text"] for e in final["evidence"]} == {
            e["id"]: e["text"] for e in archive if e["kind"] == "source"
        }
        assert next(e["text"] for e in archive if e["id"] == "schema") == schema
        sizes = [len(json.dumps({"messages": r["messages"], "tools": r["tools"]},
                                ensure_ascii=False).encode("utf-8")) for r in model.requests]
        record_property("serialized_request_bytes", str(sizes))
        assert max(sizes[:2]) < 50_000 and max(sizes[:2]) * 6 < sizes[2], sizes

        # An explicit targeted request can examine the complete retained file
        # once. A later planning turn returns to excerpts.
        run["phase"] = "investigating"
        app._trace(run, "read_document", {"index_id": "docs", "document_id": "0"}, "Успешно.")
        exchange = [{"name": "read_document", "evidence_refs": [["docs", "0"]]}]
        for expected_full in (True, False):
            await app._decide(OWNER, run, Event(), TOOLS, exchange)
            ctx = json.loads(model.requests[-1]["messages"][-1]["content"])
            doc = next(e for e in ctx["evidence"] if e["id"] == "0")
            assert (doc["text"] == archive[0]["text"]) is expected_full
    finally:
        await app.stop()
