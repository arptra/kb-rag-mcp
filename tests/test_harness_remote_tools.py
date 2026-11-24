"""Exercise the outgoing protocols against local ASGI peers, without network access."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from agent_harness import remote_tools
from agent_harness.contracts import Principal
from agent_harness.integrations_config import ConnectionConfig
from agent_harness.remote_tools import RemoteToolError, RemoteTools

_SECRET = "only-the-server-knows-this-token"
_SCHEMA = {
    "type": "object", "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"], "additionalProperties": False,
}


class Peer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.schema: dict[str, Any] = _SCHEMA
        self.extra_tools: list[dict[str, Any]] = []
        self.tool_pages: list[list[dict[str, Any]]] | None = None
        self.text = "Relevant system description"
        self.mcp_error = False
        self.rpc_error = False
        self.redirect = False
        self.working = False
        self.immediate = False
        self.block_mcp = False
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def handle(self, request: Request) -> Response:
        body = await request.json() if request.method == "POST" else {}
        self.requests.append({
            "url": str(request.url), "body": body,
            "authorization": request.headers.get("authorization"),
            "version": request.headers.get("a2a-version"),
        })
        if self.redirect:
            return RedirectResponse("https://unexpected.test/steal", status_code=307)
        if request.url.path == "/.well-known/agent-card.json":
            return JSONResponse({
                "name": f"Peer {_SECRET}", "description": "System investigator",
                "protocolVersion": "0.3.0", "url": "https://unexpected.test/steal",
                "skills": [{"id": "read", "name": "Read", "description": "Read source"}],
            })
        if request.method != "POST":
            return Response(status_code=405)
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "local-test", "version": "1"}}
        elif method.startswith("notifications/"):
            return Response(status_code=202)
        elif method == "tools/list":
            result = {"tools": [{"name": "search", "description": "Search source",
                                 "inputSchema": self.schema}, *self.extra_tools]}
            if self.tool_pages is not None:
                page = int(body.get("params", {}).get("cursor", "0"))
                result = {"tools": self.tool_pages[page]}
                if page + 1 < len(self.tool_pages):
                    result["nextCursor"] = str(page + 1)
        elif method == "tools/call":
            if self.block_mcp:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.closed.set()
            result = {"content": [{"type": "text", "text": self.text}],
                      "isError": self.mcp_error}
        elif method == "message/send":
            self.started.set()
            result = (
                {"kind": "message", "role": "agent", "messageId": "reply-1",
                 "parts": [{"kind": "text", "text": self.text}]}
                if self.immediate else
                {"kind": "task", "id": "task-1", "status": {"state": "working"}}
            )
        elif method == "tasks/get":
            result = {"kind": "task", "id": "task-1",
                      "status": {"state": "working" if self.working else "completed"}}
            if not self.working:
                result["artifacts"] = [{"artifactId": "answer", "parts": [
                    {"kind": "text", "text": self.text},
                ]}]
        elif method == "tasks/cancel":
            result = {"kind": "task", "id": "task-1", "status": {"state": "canceled"}}
        else:
            raise AssertionError(f"Unexpected method {method}")
        if self.rpc_error:
            return JSONResponse({"jsonrpc": "2.0", "id": body["id"],
                                 "error": {"code": -32000, "message": _SECRET}})
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def peer(monkeypatch: pytest.MonkeyPatch) -> tuple[Peer, list[httpx.AsyncClient]]:
    server = Peer()
    app = Starlette(routes=[Route(path, server.handle, methods=["GET", "POST", "DELETE"])
                            for path in ("/mcp", "/a2a", "/.well-known/agent-card.json")])
    real_client = httpx.AsyncClient
    clients = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        result = real_client(transport=httpx.ASGITransport(app=app), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(remote_tools.httpx, "AsyncClient", client)
    monkeypatch.setenv("TEST_CONNECTION_TOKEN", _SECRET)
    return server, clients


def connection(kind: str = "mcp", **overrides: Any) -> ConnectionConfig:
    return ConnectionConfig.model_validate({
        "id": "source", "name": "Corporate source", "kind": kind,
        "url": f"https://peer.test/{kind}", "credential_env": "TEST_CONNECTION_TOKEN",
        "allowed_tools": ["search"], **overrides,
    })


async def call(
    tools: RemoteTools, config: ConnectionConfig, *, cancel: Event | None = None,
    name: str | None = None, args: dict[str, Any] | None = None, owner: str = "alice",
    run_id: str = "run-1",
) -> Any:
    return await tools.call(
        name or ("mcp_call" if config.kind == "mcp" else "a2a_ask"),
        args if args is not None else {
            "connection_id": config.id,
            **({"tool": "search", "arguments": {"query": "system"}}
               if config.kind == "mcp" else {"question": "How does the system work?"}),
        }, connection=config, principal=Principal(owner), run_id=run_id, cancel=cancel or Event(),
    )


async def test_live_read_only_permission_tracks_new_changed_and_removed_tools(peer: Any) -> None:
    server, _ = peer
    tools = RemoteTools()
    config = connection(allowed_tools=[], include_read_only_tools=True)
    args = {"connection_id": "source"}

    async def catalog() -> list[str]:
        receipt, = await call(tools, config, name="mcp_list_tools", args=args)
        return [tool["name"] for tool in json.loads(receipt.text)["tools"]]

    assert await catalog() == []
    entry = {"name": "read_inventory", "inputSchema": server.schema,
             "annotations": {"readOnlyHint": True}}
    server.extra_tools.append(entry)
    assert await catalog() == ["read_inventory"]
    action = {**args, "tool": "read_inventory", "arguments": {"query": "services"}}
    await call(tools, config, args=action)
    entry["annotations"] = {"readOnlyHint": True, "destructiveHint": True}
    assert await catalog() == []
    with pytest.raises(remote_tools.RemoteToolError):
        await call(tools, config, args=action)
    server.extra_tools.clear()
    assert await catalog() == []
    sent = [r for r in server.requests if r["body"].get("method") == "tools/call"]
    assert len(sent) == 1


async def test_mcp_sdk_discovery_and_call_validate_and_preserve_provenance(peer: Any) -> None:
    server, clients = peer
    tools, config = RemoteTools(), connection()
    probe = await tools.probe(config)
    assert probe["available"] is True
    assert probe["tools"][0]["inputSchema"] == _SCHEMA
    evidence = (await call(tools, config))[0]
    assert evidence.index_id == "connection:source"
    assert evidence.url == config.url
    assert evidence.text == server.text
    sent = [item["body"] for item in server.requests if item["body"].get("method") == "tools/call"]
    assert sent[0]["params"] == {"name": "search", "arguments": {"query": "system"}}
    assert all(item["authorization"] == f"Bearer {_SECRET}" for item in server.requests)
    assert all(item["url"] == config.url for item in server.requests)
    assert all(client.is_closed for client in clients)
    assert evidence.id == (await call(tools, config))[0].id
    assert evidence.id != (await call(tools, config, run_id="another-run"))[0].id


async def test_agent_discovers_only_allowed_tools_and_can_search_a_large_catalog(peer: Any) -> None:
    server, clients = peer
    # Unselected tools must neither leak into the result nor consume its item limit.
    server.extra_tools = [
        {"name": f"private_{i}", "description": "Private database", "inputSchema": _SCHEMA}
        for i in range(140)
    ] + [
        {"name": f"inspect_{i}", "description": ("Details " * 80) + f" target{i}",
         "inputSchema": _SCHEMA}
        for i in range(99)
    ]
    config = connection(allowed_tools=["search", *[f"inspect_{i}" for i in range(99)]])
    tools = RemoteTools()
    evidence = (await call(tools, config, name="mcp_list_tools", args={
        "connection_id": "source",
    }))[0]
    catalog = json.loads(evidence.text)
    assert evidence.kind == "tool_metadata"
    assert catalog["matched_count"] == 100
    assert catalog["truncated"] is True
    first = catalog["tools"][0]
    assert first["name"] == "search" and first["description"] == "Search source"
    assert len(first["revision"]) == 64
    assert "private_" not in evidence.text
    filtered = (await call(tools, config, name="mcp_list_tools", args={
        "connection_id": "source", "query": "target98",
    }))[0]
    assert [item["name"] for item in json.loads(filtered.text)["tools"]] == ["inspect_98"]
    assert json.loads(filtered.text)["truncated"] is False
    assert not any(item["body"].get("method") == "tools/call" for item in server.requests)
    assert all(client.is_closed for client in clients)


async def test_agent_catalog_requires_allowed_tools_and_redacts_remote_secrets(peer: Any) -> None:
    server, _ = peer
    with pytest.raises(RemoteToolError):
        await call(RemoteTools(), connection(allowed_tools=[]), name="mcp_list_tools",
                   args={"connection_id": "source"})
    assert not server.requests
    server.extra_tools = [{"name": "inspect", "description": f"Inspect {_SECRET}",
                           "inputSchema": _SCHEMA}]
    item = (await call(RemoteTools(), connection(allowed_tools=["inspect"]),
                       name="mcp_list_tools", args={"connection_id": "source"}))[0]
    assert _SECRET not in item.text
    assert "[redacted]" in item.text


async def test_agent_catalog_follows_pagination_past_unpermitted_tools(peer: Any) -> None:
    server, _ = peer
    server.tool_pages = [
        [{"name": f"private_{i}", "description": "Private", "inputSchema": _SCHEMA}
         for i in range(140)],
        [{"name": "search", "description": "Find incidents", "inputSchema": _SCHEMA}],
    ]
    item = (await call(RemoteTools(), connection(), name="mcp_list_tools", args={
        "connection_id": "source", "query": "incidents",
    }))[0]
    assert [(t["name"], t["description"]) for t in json.loads(item.text)["tools"]] == [
        ("search", "Find incidents"),
    ]
    requests = [item["body"] for item in server.requests
                if item["body"].get("method") == "tools/list"]
    assert len(requests) == 2
    assert requests[-1]["params"]["cursor"] == "1"


async def test_capped_catalog_can_still_open_a_specific_permitted_tool(
    peer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _ = peer
    monkeypatch.setattr(remote_tools, "_MAX_TOOLS", 2)
    server.tool_pages = [
        [{"name": name, "inputSchema": _SCHEMA} for name in ("first", "second")],
        [{"name": "search", "description": "Find incidents", "inputSchema": _SCHEMA}],
    ]
    config = connection(allowed_tools=["first", "second", "search"])
    result = await call(RemoteTools(), config, name="mcp_tool_schema",
                        args={"connection_id": "source", "tool": "search"})
    assert json.loads(result[0].text)["inputSchema"] == _SCHEMA
    assert not any(r["body"].get("method") == "tools/call" for r in server.requests)


@pytest.mark.parametrize("changes,owner", [
    ({"allowed_tools": []}, "alice"),
    ({"allowed_tools": ["Search"]}, "alice"),
    ({"allowed_owners": ["bob"]}, "alice"),
    ({"enabled": False}, "alice"),
    ({}, ""),
])
async def test_permission_denial_precedes_any_network(
    peer: Any, changes: dict[str, Any], owner: str,
) -> None:
    server, clients = peer
    with pytest.raises(RemoteToolError, match="PermissionError"):
        await call(RemoteTools(), connection(**changes), owner=owner)
    assert server.requests == []
    assert clients == []


@pytest.mark.parametrize("arguments", [{}, {"query": 17}, {"query": "x", "write": True}])
async def test_mcp_rejects_invalid_input_without_calling_tool(peer: Any, arguments: Any) -> None:
    server, _ = peer
    with pytest.raises(RemoteToolError, match="ValidationError") as issue:
        await call(RemoteTools(), connection(), args={
            "connection_id": "source", "tool": "search", "arguments": arguments,
        })
    assert issue.value.reason == "mcp_input_mismatch"
    assert not any(item["body"].get("method") == "tools/call" for item in server.requests)


async def test_mcp_does_not_fetch_external_json_schema_references(peer: Any) -> None:
    server, _ = peer
    server.schema = {"type": "object", "$ref": "https://unexpected.test/schema"}
    with pytest.raises(RemoteToolError, match="ValueError"):
        await call(RemoteTools(), connection())
    assert all(item["url"] == "https://peer.test/mcp" for item in server.requests)
    assert not any(item["body"].get("method") == "tools/call" for item in server.requests)


@pytest.mark.parametrize("kind", ["mcp", "a2a"])
async def test_peer_cannot_echo_credential_and_output_is_bounded(peer: Any, kind: str) -> None:
    server, _ = peer
    server.text = _SECRET + ":" + "x" * 20000
    evidence = (await call(RemoteTools(poll_interval=0.001), connection(kind)))[0]
    assert len(evidence.text) == 12000
    assert evidence.text.startswith("[redacted]:")
    assert _SECRET not in evidence.model_dump_json()
    if kind == "a2a":
        probe = await RemoteTools().probe(connection(kind))
        assert probe["available"] is True
        assert _SECRET not in json.dumps(probe)


async def test_mcp_error_is_not_treated_as_evidence_or_returned_raw(peer: Any) -> None:
    server, _ = peer
    server.mcp_error = True
    server.text = _SECRET
    with pytest.raises(RemoteToolError) as error:
        await call(RemoteTools(), connection())
    assert str(error.value) == "Remote operation failed (ValueError)"
    assert _SECRET not in repr(error.value)


async def test_a2a_wire_uses_configured_endpoint_and_only_supplied_question(peer: Any) -> None:
    server, clients = peer
    tools, config = RemoteTools(poll_interval=0.001), connection("a2a")
    probe = await tools.probe(config)
    assert probe["agent_card"]["protocol_version"] == "0.3.0"
    assert "url" not in probe["agent_card"]
    evidence = (await call(tools, config))[0]
    assert evidence.text == server.text
    messages = [item for item in server.requests if item["body"].get("method") == "message/send"]
    assert len(messages) == 1
    message = messages[0]["body"]["params"]["message"]
    assert set(message) == {"kind", "role", "messageId", "parts"}
    assert message["parts"] == [{"kind": "text", "text": "How does the system work?"}]
    assert messages[0]["body"]["params"]["configuration"]["blocking"] is False
    assert [item["body"].get("method") for item in server.requests][1:] == [
        "message/send", "tasks/get",
    ]
    assert all(item["url"] == config.url for item in server.requests[1:])
    assert all(item["authorization"] == f"Bearer {_SECRET}" for item in server.requests)
    assert all(item["version"] == "0.3" for item in server.requests)
    assert all(client.is_closed for client in clients)


async def test_a2a_accepts_immediate_message_without_polling(peer: Any) -> None:
    server, _ = peer
    server.immediate = True
    assert (await call(RemoteTools(), connection("a2a")))[0].text == server.text
    assert [item["body"].get("method") for item in server.requests] == ["message/send"]


async def test_a2a_sanitizes_jsonrpc_errors(peer: Any) -> None:
    server, _ = peer
    server.rpc_error = True
    with pytest.raises(RemoteToolError) as error:
        await call(RemoteTools(), connection("a2a"))
    assert str(error.value) == "Remote operation failed (ValueError)"
    assert _SECRET not in repr(error.value)


@pytest.mark.parametrize("cancel_with_event", [True, False])
async def test_a2a_cancellation_cancels_remote_task_and_closes_client(
    peer: Any, cancel_with_event: bool,
) -> None:
    server, clients = peer
    server.working = True
    cancel = Event()
    task = asyncio.create_task(call(
        RemoteTools(poll_interval=0.1), connection("a2a"), cancel=cancel,
    ))
    await asyncio.wait_for(server.started.wait(), timeout=1)
    await asyncio.sleep(0.01)
    if cancel_with_event:
        cancel.set()
    else:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    cancels = [item["body"] for item in server.requests
               if item["body"].get("method") == "tasks/cancel"]
    assert len(cancels) == 1
    assert cancels[0]["params"] == {"id": "task-1"}
    assert all(client.is_closed for client in clients)


async def test_a2a_timeout_also_cancels_known_remote_task(peer: Any) -> None:
    server, clients = peer
    server.working = True
    with pytest.raises(RemoteToolError, match="TimeoutError"):
        await asyncio.wait_for(call(
            RemoteTools(timeout_seconds=0.15, poll_interval=0.01), connection("a2a"),
        ), timeout=1)
    assert any(item["body"].get("method") == "tasks/cancel" for item in server.requests)
    assert all(client.is_closed for client in clients)


async def test_mcp_cancellation_interrupts_live_sdk_call_and_closes_client(peer: Any) -> None:
    server, clients = peer
    server.block_mcp = True
    cancel = Event()
    task = asyncio.create_task(call(RemoteTools(), connection(), cancel=cancel))
    await asyncio.wait_for(server.started.wait(), timeout=1)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert server.closed.is_set()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("kind", ["mcp", "a2a"])
async def test_redirect_never_forwards_credentials(peer: Any, kind: str) -> None:
    server, clients = peer
    server.redirect = True
    tools = RemoteTools(timeout_seconds=0.5)
    with pytest.raises(RemoteToolError):
        await call(tools, connection(kind))
    assert len(server.requests) == 1
    assert server.requests[0]["url"] == f"https://peer.test/{kind}"
    assert all(client.is_closed for client in clients)


async def test_missing_credential_fails_without_connecting_or_exposing_env_name(
    peer: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, _ = peer
    monkeypatch.delenv("TEST_CONNECTION_TOKEN")
    config = connection()
    probe = await RemoteTools().probe(config)
    assert probe["available"] is False
    assert probe["error"] == "ValueError"
    assert "TEST_CONNECTION_TOKEN" not in json.dumps(probe)
    with pytest.raises(RemoteToolError) as error:
        await call(RemoteTools(), config)
    assert "TEST_CONNECTION_TOKEN" not in str(error.value)
    assert server.requests == []


@pytest.mark.parametrize("kind,args", [
    ("mcp", {"connection_id": "other", "tool": "search", "arguments": {}}),
    ("mcp", {"connection_id": "source", "tool": "search", "url": "https://elsewhere.test"}),
    ("mcp", {"connection_id": "source", "tool": "search", "arguments": []}),
    ("a2a", {"connection_id": "source", "question": "   "}),
    ("a2a", {"connection_id": "source", "question": "x" * 8001}),
    ("a2a", {"connection_id": "source", "question": "x", "history": ["private"]}),
])
async def test_model_arguments_cannot_change_destination_or_add_hidden_context(
    peer: Any, kind: str, args: Any,
) -> None:
    server, _ = peer
    with pytest.raises(RemoteToolError, match="ValueError"):
        await call(RemoteTools(), connection(kind), args=args)
    assert server.requests == []


async def test_oversized_peer_response_is_refused_before_becoming_evidence(peer: Any) -> None:
    server, clients = peer
    server.immediate = True
    server.text = "x" * (300 * 1024)
    with pytest.raises(RemoteToolError, match="ValueError"):
        await call(RemoteTools(), connection("a2a"))
    assert all(client.is_closed for client in clients)


async def test_mcp_schema_lookup_is_read_only_and_marked_as_metadata(peer: Any) -> None:
    server, _ = peer
    evidence = (await call(RemoteTools(), connection(), name="mcp_tool_schema", args={
        "connection_id": "source", "tool": "search",
    }))[0]
    payload = json.loads(evidence.text)
    assert payload["kind"] == "tool_schema_not_system_evidence"
    assert payload["inputSchema"] == _SCHEMA
    assert payload["truncated"] is False
    assert not any(item["body"].get("method") == "tools/call" for item in server.requests)


async def test_mcp_schema_lookup_requires_explicit_allowlist(peer: Any) -> None:
    server, _ = peer
    with pytest.raises(RemoteToolError, match="PermissionError"):
        await call(RemoteTools(), connection(allowed_tools=[]), name="mcp_tool_schema", args={
            "connection_id": "source", "tool": "search",
        })
    assert server.requests == []


async def test_oversized_mcp_schema_is_not_returned_as_broken_json(peer: Any) -> None:
    server, _ = peer
    server.schema = {"type": "object", "description": "x" * 20000}
    evidence = (await call(RemoteTools(), connection(), name="mcp_tool_schema", args={
        "connection_id": "source", "tool": "search",
    }))[0]
    payload = json.loads(evidence.text)
    assert payload["truncated"] is True
    assert payload["inputSchema"] is None
    assert payload["message"]
    assert len(evidence.text) <= 12000
