"""Ordinary chat follows live MCP read contracts before giving its answer."""

import json
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from test_harness_investigation_flow import COLLEAGUE, OWNER, MCPPeer, finished, local_endpoints

from agent_harness.config import HarnessSettings
from agent_harness.contracts import RunRequest
from agent_harness.mcp_catalog import question_record_lookup, quick_schema_reads, text_read_call
from agent_harness.runtime import HarnessRuntime


class Tickets(MCPPeer):
    activity = "waiting"

    async def handle(self, request: Request) -> Response:
        if request.method != "POST":
            return await super().handle(request)
        body = await request.json()
        if body["method"] not in {"tools/list", "tools/call"}:
            return await super().handle(request)
        self.requests.append(body)
        if body["method"] == "tools/list":
            operations = [
                ("lookup_ticket", "key", True), ("old_ticket_lookup", "key", True),
                ("get_activity", "activity_id", True), ("erase_ticket", "key", False),
            ]
            result = {"tools": [
                {"name": name, "description": name.replace("_", " "),
                 "inputSchema": {"type": "object", "properties": {field: {"type": "string"}},
                                 "required": [field], "additionalProperties": False},
                 "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only}}
                for name, field, read_only in operations
            ]}
        else:
            name, args = body["params"]["name"], body["params"]["arguments"]
            assert name != "erase_ticket"
            if name == "get_activity":
                assert args == {"activity_id": "act-78"}
                detail = "Current status: " + self.activity
            else:
                assert args == {"key": "PAY-42"}
                detail = ("No current records." if name == "old_ticket_lookup"
                          else "activity_id=act-78")
            result = {"isError": name == "old_ticket_lookup",
                      "content": [{"type": "text", "text": detail}]}
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.mark.parametrize(("mode", "outage"), [("quick", False), ("quick", True),
                                              ("autonomous", False), ("investigate", False)])
async def test_quick_mcp_continues_to_fresh_evidence_and_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, outage: bool,
) -> None:
    peer = Tickets([])
    hub, model, _ = local_endpoints(tmp_path, monkeypatch, peer)
    settings = hub.store.load()
    connection = next(c for c in settings.connections if c.id == "jira")
    connection.allowed_tools = [
        "lookup_ticket", "get_activity", "old_ticket_lookup", "erase_ticket",
    ]
    # Explicit allowlisting alone does not grant a mutating tool to quick chat.
    hub.store.save(settings)
    did_fail = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal did_fail
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        catalog = {t["function"]["name"]: t["function"] for t in payload.get("tools", [])}
        assert "erase_ticket" not in catalog
        if mode == "quick":
            assert "erase_ticket" not in json.dumps(payload)
        evidence = ctx["evidence"]
        current = next((s for s in evidence if "Current status:" in s["text"]), None)
        link = next((s for s in evidence if "activity_id=act-78" in s["text"]), None)
        if current:
            answer = f"Статус заявки PAY-42 — {peer.activity}."
            if catalog:
                message = {"content": answer + f" [{current['id']}]."}
            else:
                source = current["number"]
                # A small writer can select the right source and garble its literal value.
                message = {"content": json.dumps({"answer": "Пawaiting", "sources": [source]})}
        else:
            assert catalog, "Chat sent metadata to final synthesis without a lookup"
            name = ("get_activity" if link else
                    "old_ticket_lookup" if outage and not did_fail else "lookup_ticket")
            if name in catalog:
                required = catalog[name]["parameters"]["required"]
                assert required == (["activity_id"] if link else ["key"])
                args = {"activity_id": link["text"].split("=")[1]} if link else {"key": "PAY-42"}
                did_fail |= name == "old_ticket_lookup"
            else:
                # Selected connections are actually discovered before the first decision.
                assert any(s.get("kind") == "tool_metadata" for s in evidence)
                args = {"connection_id": "jira", "tool": name}
                name = "mcp_tool_schema"
            message = {"tool_calls": [{"type": "function", "function": {
                "name": name, "arguments": json.dumps(args),
            }}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    model.transport = httpx.MockTransport(respond)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
                             model=model, tools=hub)
    await runtime.start()
    try:
        request = RunRequest.model_validate({
            "question": "Какой сейчас статус заявки PAY-42 в Jira?",
            "index_ids": ["connection:jira"], "mode": mode,
        })
        run = await runtime.submit(OWNER, request)
        result = await finished(runtime, run["id"])
        assert result["status"] == ("partial" if outage else "completed"), result["warnings"]
        assert "waiting" in result["answer"] and "[" in result["answer"]
        called = [m["params"]["name"] for m in peer.requests if m["method"] == "tools/call"]
        first = ["old_ticket_lookup"] if outage else []
        assert called == [*first, "lookup_ticket", "get_activity"]
        assert [s["name"] for s in result["research_trace"]].count("mcp_call") == len(called)
        if not outage:
            assert result["metrics"]["model_calls"] == 1
            assert any(s["name"] == "mcp_reference" for s in result["research_trace"])
        if mode == "quick":
            peer.activity = "resolved"
            run = await runtime.submit(COLLEAGUE, request)
            later = await finished(runtime, run["id"], COLLEAGUE)
            assert "resolved" in later["answer"] and "waiting" not in later["answer"]
            assert [s for s in later["research_trace"] if s["name"] == "memory_route"], later
            working = {action["remote_tool"] for item in later["memory_used"]
                       for action in item.get("successful_tools", []) if action.get("remote_tool")}
            assert "get_activity" in working and "old_ticket_lookup" not in working
    finally:
        await runtime.stop()


def test_schema_prefetch_respects_budget_and_ranks_large_truncated_manifest() -> None:
    items = [{"name": f"read_item_{i}", "description": "List items"} for i in range(16)]
    items.append({"name": "getSettlement", "description": "Прочитать статус платежа"})
    catalog = {"text": json.dumps({"kind": "tool_catalog_not_system_evidence", "truncated": True,
                                  "tools": items})}
    reads = quick_schema_reads(catalog, "jira", 2, set(), "Проверить статус платежа")
    assert len(reads) == 2 and reads[0]["args"]["tool"] == "getSettlement"
    assert not quick_schema_reads(catalog, "jira", 0, set(), "статус")


def test_verified_record_requires_an_exact_single_id_and_text_tools_are_unambiguous() -> None:
    question = "Какой статус заявки PAY-421?"
    assert question_record_lookup(question, {"ticket": "PAY-421"})
    assert not question_record_lookup(question, {"ticket": "PAY-42"})
    assert not question_record_lookup(question, {"ticket": "PAY-421", "other": "JIRA-1"})
    assert not question_record_lookup("Сравни PAY-421 и PAY-422", {"ticket": "PAY-421"})
    targets = {"lookup_ticket": {"connection_id": "jira", "tool": "lookup_ticket"}}
    draft = '{"name":"lookup Ticket","parameters":{"ticket":"PAY-421"}}'
    assert text_read_call(draft, targets) == [
        {"name": "lookup_ticket", "args": {"ticket": "PAY-421"}},
    ]
    assert not text_read_call(draft, {**targets, "lookupTicket": targets["lookup_ticket"]})
    assert not text_read_call('{"name":"eraseTicket","parameters":{"ticket":"PAY-421"}}', targets)
