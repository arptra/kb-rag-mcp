"""One live SDK investigation while an authorized MCP peer changes its manifest."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from test_harness_investigation_flow import (
    OWNER,
    CurrentKnowledge,
    MCPPeer,
    finished,
    local_endpoints,
)

from agent_harness.config import HarnessSettings
from agent_harness.contracts import RunRequest
from agent_harness.runtime import HarnessRuntime

TOKEN = "temporary-peer-credential"


class DynamicPeer(MCPPeer):
    generation = 1
    after_dispatch = False

    @staticmethod
    def definition(name: str, arg: str, *, read_only: bool = True) -> dict[str, Any]:
        return {
            "name": name, "description": f"Read records {TOKEN}",
            "inputSchema": {"type": "object", "properties": {arg: {"type": "string"}},
                            "required": [arg], "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only},
        }

    async def handle(self, request: Request) -> Response:
        assert request.url.hostname == "jira.test"
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        if request.method != "POST":
            return Response(status_code=405)
        body = await request.json()
        self.requests.append(body)
        method = body["method"]
        if method.startswith("notifications/"):
            return Response(status_code=202)
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": body["id"]}
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "changing-local-peer", "version": "1"}}
        elif method == "tools/list":
            field = "service" if self.generation == 1 else "service_id"
            tools = [self.definition("inspect", field),
                     self.definition("private_erase", "id", read_only=False)]
            if self.generation == 1:
                tools.append(self.definition("legacy", "id"))
            else:
                key = "key" if self.generation == 2 else "new_key"
                tools.append(self.definition("lookup", key))
            if self.generation >= 4:
                tools.append(self.definition("get_receipt", "id"))
            result = {"tools": tools}
        else:
            assert method == "tools/call"
            if self.after_dispatch and body["params"]["name"] == "inspect":
                reply["error"] = {"code": -32602, "message": "Arguments changed at dispatch"}
                return JSONResponse(reply)
            result = {"content": [{"type": "text", "text": "payment_status=settled"}]}
        return JSONResponse({**reply, "result": result})


@pytest.mark.asyncio
@pytest.mark.parametrize("after_dispatch", [False, True])
async def test_refresh_and_preflight_recovery_while_mcp_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_dispatch: bool,
) -> None:
    peer = DynamicPeer([])
    hub, router, clients = local_endpoints(tmp_path, monkeypatch, peer)
    config = hub.store.load()
    connection = next(item for item in config.connections if item.id == "jira")
    connection.allowed_tools = []
    connection.include_read_only_tools = True
    connection.credential_env = "TEMP_DYNAMIC_MCP_KEY"
    monkeypatch.setenv("TEMP_DYNAMIC_MCP_KEY", TOKEN)
    hub.store.save(config)
    turn = 0

    def model(request: httpx.Request) -> httpx.Response:
        nonlocal turn
        turn += 1
        assert TOKEN not in request.content.decode() and "authorization" not in request.headers
        context = json.loads(json.loads(request.content)["messages"][-1]["content"])
        assert "private_erase" not in json.dumps(context)
        metadata = [e for e in context["evidence"] if e.get("kind") == "tool_metadata"]
        catalogs = [e for e in metadata if "tools" in json.loads(e["text"])]
        schemas = {s["name"]: s for e in metadata
                   if (s := json.loads(e["text"])).get("inputSchema")}
        args: dict[str, Any] = {"connection_id": "jira"}
        if turn == 1:
            tools = json.loads(catalogs[0]["text"])["tools"]
            assert {t["name"] for t in tools} == {"inspect", "legacy"}
            name = "mcp_tool_schema"
            args["tool"] = "inspect"
        elif turn == 2:
            assert schemas["inspect"]["inputSchema"]["required"] == ["service"]
            peer.generation = 2
            name = "mcp_call"
            args.update(tool="inspect", arguments={"service": "payment"})
        elif turn == 3:
            assert schemas["inspect"]["inputSchema"]["required"] == ["service_id"]
            assert any(s["name"] == "mcp_contract_refresh" for s in context["prior_tool_attempts"])
            assert catalogs[0]["tool_changes"] == {
                "added": ["lookup"], "changed": ["inspect"], "removed": ["legacy"],
            }
            name = "mcp_list_tools"
            args["refresh"] = True
        elif turn == 4:
            assert "tool_changes" not in catalogs[0]
            name = "mcp_call"
            args.update(tool="legacy", arguments={"id": "old"})
        elif turn == 5:
            assert all(t["name"] != "legacy" for t in json.loads(catalogs[0]["text"])["tools"])
            name = "mcp_tool_schema"
            args["tool"] = "lookup"
        elif turn == 6:
            assert schemas["lookup"]["inputSchema"]["required"] == ["key"]
            peer.generation = 3
            name = "mcp_tool_schema"
            args.update(tool="lookup", refresh=True)
        elif after_dispatch and turn in {7, 8}:
            peer.after_dispatch = True
            name = "mcp_call"
            # Even new arguments cannot replay an action whose dispatch result
            # is unknown. Discovery is allowed, then an independent tool can run.
            args.update(tool="inspect", arguments={"service_id": "payment" if turn == 7 else "new"})
        elif turn == (9 if after_dispatch else 7):
            assert schemas["lookup"]["inputSchema"]["required"] == ["new_key"]
            # This time the chosen tool still works. Its normal SDK preflight
            # observes a new peer capability and updates the model's catalog.
            peer.generation = 4
            name = "mcp_call"
            args.update(tool="lookup", arguments={"new_key": "payment"})
        else:
            assert turn == (10 if after_dispatch else 8)
            assert catalogs[0]["tool_changes"] == {"added": ["get_receipt"]}
            source = next(e for e in context["evidence"] if e["kind"] == "source")
            assert source["text"] == "payment_status=settled"
            return httpx.Response(200, json={"choices": [{"message": {
                "content": f"Оплата завершена [{source['id']}].",
            }}]})
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"type": "function", "function": {"name": name, "arguments": json.dumps(args)}},
        ]}}]})

    router.transport = httpx.MockTransport(model)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
                             CurrentKnowledge([]), router, hub)
    await runtime.start()
    try:
        run = await runtime.submit(OWNER, RunRequest(
            question="Каков статус оплаты?", mode="autonomous", index_ids=["connection:jira"],
        ))
        result = await finished(runtime, run["id"])
        # An unresolved post-dispatch outcome remains visible even when a
        # different tool independently confirms the requested payment status.
        expected_status = "partial" if after_dispatch else "completed"
        assert result["status"] == expected_status, result["warnings"]
        assert "Оплата завершена" in result["answer"]
        assert TOKEN not in json.dumps(result, ensure_ascii=False)
        persisted = runtime.store.get(OWNER.owner_id, run["id"])
        assert TOKEN not in json.dumps(persisted, ensure_ascii=False)
        assert runtime._graph is not None
        checkpoint_config = runtime._graph.config(OWNER, run["id"])
        async for step in runtime._graph.graph.aget_state_history(checkpoint_config):
            assert TOKEN not in json.dumps(step.values, ensure_ascii=False)
        calls = [r["params"] for r in peer.requests if r["method"] == "tools/call"]
        expected = ([{"name": "inspect", "arguments": {"service_id": "payment"}}]
                    if after_dispatch else [])
        assert calls == [*expected, {"name": "lookup", "arguments": {"new_key": "payment"}}]
    finally:
        await runtime.stop()
    assert all(client.is_closed for client in clients)
