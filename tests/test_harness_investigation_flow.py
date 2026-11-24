"""A real persisted graph follows evidence across RAG, MCP SDK and the model API.

Only the corporate peers are simulated. The model response script validates the
facts and tool descriptions actually received before selecting its next action.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore
from agent_harness.model_connection import ModelConnection, ModelRouter
from agent_harness.remote_tools import RemoteTools
from agent_harness.runtime import HarnessRuntime

OWNER = Principal("investigator")
COLLEAGUE = Principal("second-investigator")
QUESTION = "Почему платёж выполнен, а клиент видит статус «В обработке»?"  # noqa: RUF001
ISSUE = "PAY-3471"
NEXT_ISSUE = "PAY-3522"
DOCUMENT = "incident-routing-73"
FOLLOW_UP = "notification-projector status-retry-v2 delay"
REMOTE_TOOL = "get_issue_details"
SCHEMA = {
    "type": "object",
    "properties": {"issue_key": {"type": "string", "pattern": "^PAY-[0-9]+$"}},
    "required": ["issue_key"],
    "additionalProperties": False,
}


class CurrentKnowledge:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.queries: list[str] = []

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "knowledge", "name": "Confluence"}]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        assert kwargs["principal"] in {OWNER, COLLEAGUE} and kwargs["index_id"] == "knowledge"
        query = kwargs["query"]
        self.queries.append(query)
        if query.startswith(QUESTION):
            self.events.append("initial_rag")
            return [Evidence(
                id="routing-hit", document_id=DOCUMENT, index_id="knowledge",
                title="Путь диагностики статуса платежа",
                text="Номер связанного инцидента указан в полной версии документа.",
                revision="2026-11-24",
            )]
        assert query == FOLLOW_UP, "The next search must use facts from the MCP issue"
        self.events.append("follow_up_rag")
        return [Evidence(
            id="retry-policy", index_id="knowledge", title="Повтор доставки статуса",
            text="notification-projector retries status-retry-v2 after 180 seconds; "
                 "the customer status remains pending until the projection receives the event.",
            revision="2026-11-24",
        )]

    async def read(self, **kwargs: Any) -> Evidence:
        assert kwargs["principal"] in {OWNER, COLLEAGUE} and kwargs["index_id"] == "knowledge"
        assert kwargs["document_id"] == DOCUMENT
        self.events.append("read_document")
        return Evidence(
            id=DOCUMENT, index_id="knowledge", title="Диагностика платежного контура",
            text=json.dumps({"issue_key": ISSUE if kwargs["principal"] == OWNER else NEXT_ISSUE,
                             "mcp_connection": "jira"}),
            revision="2026-11-24",
        )


class MCPPeer:
    """Minimal Streamable HTTP peer exercised by the installed MCP client SDK."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.requests: list[dict[str, Any]] = []

    async def handle(self, request: Request) -> Response:
        assert request.url.hostname == "jira.test"
        assert "authorization" not in request.headers
        if request.method != "POST":
            return Response(status_code=405)
        body = await request.json()
        self.requests.append(body)
        method = body["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Jira test peer", "version": "1"}}
        elif method.startswith("notifications/"):
            return Response(status_code=202)
        elif method == "tools/list":
            self.events.append("mcp_tools_list")
            result = {"tools": [
                {"name": REMOTE_TOOL, "description": "Read the current issue and its component",
                 "inputSchema": SCHEMA},
                {"name": "delete_issue", "description": "UNAUTHORIZED_PRIVATE_TOOL",
                 "inputSchema": {"type": "object"}},
            ]}
        elif method == "tools/call":
            issue = body["params"]["arguments"]["issue_key"]
            assert issue in {ISSUE, NEXT_ISSUE}
            assert body["params"] == {"name": REMOTE_TOOL, "arguments": {"issue_key": issue}}
            self.events.append("mcp_tool_call")
            result = {"content": [{"type": "text", "text": json.dumps({
                "issue_key": issue, "component": "notification-projector",
                "retry_topic": "status-retry-v2", "status": "confirmed",
            })}], "isError": False}
        else:
            raise AssertionError(f"Unexpected MCP method: {method}")
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})


class ModelPeer:
    def __init__(self, events: list[str], *, invalid_first: bool = False) -> None:
        self.invalid_first = invalid_first
        self.bad_decision_sent = False
        self.events = events
        self.contexts: list[dict[str, Any]] = []
        self.selected_tool = ""
        self.issue_key = ""

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url == "https://models.test/v1/chat/completions"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["thinking"] == {"type": "disabled"}
        assert payload["max_tokens"] == 32768
        context = json.loads(payload["messages"][-1]["content"])
        if self.invalid_first and not self.bad_decision_sent:
            self.bad_decision_sent = True
            self.events.append("model:malformed_action")
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "broken", "type": "function", "function": {
                    "name": "read_document", "arguments": '{"document_id":',
                }}],
            }, "finish_reason": "tool_calls"}]})
        self.contexts.append(context)
        serialized = json.dumps(context)
        assert "UNAUTHORIZED_PRIVATE_TOOL" not in serialized and "delete_issue" not in serialized
        assert set(context["allowed_indexes"]) == {"knowledge", "connection:jira"}
        evidence = {item["id"]: item for item in context["evidence"]}
        assert evidence[DOCUMENT]["read_document"] is True
        route = json.loads(evidence[DOCUMENT]["text"])
        turn = len(self.contexts)
        name, args, content = "", {}, ""
        if turn == 1:
            # A public plan is one model decision, not a completed investigation.
            assert self.events[:2] == ["initial_rag", "read_document"]
            if self.invalid_first:
                assert any(item["name"] == "model_response"
                           for item in context["prior_tool_attempts"])
            self.issue_key = route["issue_key"]
            name, args = "update_research_plan", {
                "goal": QUESTION,
                "questions": ["Проверить инцидент", "Уточнить задержку повторной доставки"],
                "gaps": ["Нужны текущие факты из Jira и регламент повторной доставки"],
                "next_action": "Найти инструмент чтения инцидента", "ready_to_answer": False,
            }
        elif turn == 2:
            assert context["research_plan"]["model_updated"] is True
            assert context["research_plan"]["ready_to_answer"] is False
            name, args = "mcp_list_tools", {
                "connection_id": route["mcp_connection"], "query": "issue",
            }
        elif turn == 3:
            catalogs = [json.loads(item["text"]) for item in evidence.values()
                        if item.get("kind") == "tool_metadata"]
            assert len(catalogs) == 1
            assert catalogs[0]["kind"] == "tool_catalog_not_system_evidence"
            assert catalogs[0]["matched_count"] == 1
            self.selected_tool = catalogs[0]["tools"][0]["name"]
            name, args = "mcp_tool_schema", {
                "connection_id": route["mcp_connection"], "tool": self.selected_tool,
            }
        elif turn == 4:
            metadata = [json.loads(item["text"]) for item in evidence.values()
                        if item.get("kind") == "tool_metadata"]
            schema = next(item for item in metadata
                          if item["kind"] == "tool_schema_not_system_evidence")
            assert schema["name"] == self.selected_tool
            assert schema["inputSchema"] == SCHEMA
            name, args = "mcp_call", {
                "connection_id": route["mcp_connection"], "tool": self.selected_tool,
                "arguments": {schema["inputSchema"]["required"][0]: self.issue_key},
            }
        elif turn == 5:
            remote = next(item for item in evidence.values()
                          if item["index_id"] == "connection:jira" and item["kind"] == "source")
            incident = json.loads(remote["text"])
            assert incident["issue_key"] == self.issue_key
            name, args = "search_knowledge", {
                "index_id": "knowledge",
                "query": f"{incident['component']} {incident['retry_topic']} delay",
            }
        elif turn == 6:
            assert "180 seconds" in evidence["retry-policy"]["text"]
            name, args = "update_research_plan", {
                "goal": QUESTION, "gaps": [], "ready_to_answer": True,
                "completed": [f"Инцидент {self.issue_key} подтверждён",
                              "Задержка повторной доставки проверена [retry-policy]"],
                "next_action": "Объяснить причину задержки и привести источники",
            }
        else:
            assert turn == 7, "The graph should finish after the verified answer"
            assert context["research_plan"]["ready_to_answer"] is True
            assert "180 seconds" in evidence["retry-policy"]["text"]
            remote = next(item for item in evidence.values()
                          if item["index_id"] == "connection:jira" and item["kind"] == "source")
            content = (
                "## Причина\n\nПлатёж завершён, но клиентский статус ждёт обновления проекции. "  # noqa: RUF001
                f"Это подтверждено инцидентом {self.issue_key} [{remote['id']}].\n\n"
                "## Когда обновится статус\n\nnotification-projector повторно обрабатывает "
                "status-retry-v2 через **180 секунд** [retry-policy]."
            )
        self.events.append("model:" + (name or "final_answer"))
        message: dict[str, Any] = {"role": "assistant", "content": content or None}
        if name:
            assert name in {tool["function"]["name"] for tool in payload["tools"]}
            message["tool_calls"] = [{
                "id": f"call_{turn}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }]
        return httpx.Response(200, json={
            "id": f"completion-{turn}", "object": "chat.completion", "created": 1,
            "model": "deepseek-flash", "choices": [{
                "index": 0, "message": message, "finish_reason": "tool_calls" if name else "stop",
            }],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        })


async def finished(
    runtime: HarnessRuntime, run_id: str, principal: Principal = OWNER,
) -> dict[str, Any]:
    async with asyncio.timeout(10):
        while True:
            run = await runtime.get(principal, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


def local_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, peer: MCPPeer,
) -> tuple[IntegrationHub, ModelRouter, list[httpx.AsyncClient]]:
    remote_app = Starlette(routes=[Route("/mcp", peer.handle, methods=["GET", "POST", "DELETE"])])
    real_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        # ModelRouter supplies its own MockTransport. Real RemoteTools uses the
        # SDK with the ASGI peer, so no network or corporate credentials are used.
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.ASGITransport(app=remote_app)
        result = real_client(**kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncClient", client)
    integrations = IntegrationStore(tmp_path)
    integrations.save(IntegrationSettings.model_validate({"connections": [{
        "id": "jira", "name": "Jira", "kind": "mcp", "url": "https://jira.test/mcp",
        "allowed_tools": [REMOTE_TOOL], "allowed_owners": [OWNER.owner_id, COLLEAGUE.owner_id],
    }, {
        "id": "private", "name": "Private", "kind": "mcp", "url": "https://private.test/mcp",
        "allowed_tools": ["delete_issue"], "allowed_owners": ["someone-else"],
    }]}))

    async def authorized(principal: Principal) -> bool:
        return principal in {OWNER, COLLEAGUE}

    hub = IntegrationHub(integrations, authorized)
    hub._remote = RemoteTools()
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(
        provider="openai", base_url="https://models.test/v1", profile="deepseek_v4_flash",
    ))
    return hub, router, clients


async def test_empty_memory_discovers_selected_mcp_before_first_http_model_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = MCPPeer([])
    hub, router, _ = local_endpoints(tmp_path, monkeypatch, peer)
    turns = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal turns
        turns += 1
        ctx = json.loads(json.loads(request.content)["messages"][-1]["content"])
        evidence = ctx["evidence"]
        assert ctx["shared_memory_not_evidence"] == []
        if turns == 1:
            assert peer.events == ["mcp_tools_list"]
            catalog = json.loads(evidence[0]["text"])
            assert [tool["name"] for tool in catalog["tools"]] == [REMOTE_TOOL]
        args: dict[str, Any] = {"connection_id": "jira"}
        if turns == 4:
            observed = next(e for e in evidence if e["kind"] == "source")
            assert json.loads(observed["text"])["issue_key"] == ISSUE
            message = {"content": f"Статус задачи {ISSUE} подтверждён [{observed['id']}]."}
        else:
            name = ["mcp_list_tools", "mcp_tool_schema", "mcp_call"][turns - 1]
            if turns >= 2:
                args["tool"] = REMOTE_TOOL
            if turns == 3:
                args["arguments"] = {"issue_key": ISSUE}
            message = {"tool_calls": [{"type": "function", "function": {
                "name": name, "arguments": json.dumps(args),
            }}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    router.transport = httpx.MockTransport(respond)
    app = HarnessRuntime(HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path),
                         CurrentKnowledge(peer.events), router, hub)
    await app.start()
    try:
        created = await app.submit(OWNER, RunRequest(question=f"Проверь {ISSUE}", mode="autonomous",
                                                    index_ids=["connection:jira"]))
        result = await finished(app, created["id"])
        assert result["status"] == "completed", result["warnings"]
        assert turns == 4
        # One list for discovery, one for the schema, one for pre-call validation.
        assert peer.events == ["mcp_tools_list"] * 3 + ["mcp_tool_call"]
        assert any(step["name"] == "source_discovery" for step in result["research_trace"])
    finally:
        await app.stop()


@pytest.mark.parametrize("invalid_first", [False, True])
async def test_autonomous_deepseek_investigates_across_rag_and_real_mcp_sdk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_first: bool,
) -> None:
    events: list[str] = []
    source, peer = CurrentKnowledge(events), MCPPeer(events)
    model = ModelPeer(events, invalid_first=invalid_first)
    hub, router, clients = local_endpoints(tmp_path, monkeypatch, peer)
    router.transport = httpx.MockTransport(model.respond)
    settings = HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path)
    runtime = HarnessRuntime(settings, source, router, hub)
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question=QUESTION, mode="autonomous", index_ids=["knowledge", "connection:jira"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", {
            "warnings": result["warnings"], "events": events,
            "trace": result["research_trace"], "answer": result["answer"],
        }
        assert "180 секунд" in result["answer"] and ISSUE in result["answer"]
        assert not result["warnings"]
        assert len(model.contexts) == 7
        assert source.queries == [QUESTION, FOLLOW_UP]
        assert events.index("mcp_tool_call") < events.index("follow_up_rag")
        trace = result["research_trace"]
        assert [item["name"] for item in trace if item["name"] in {
            "read_document", "update_research_plan", "mcp_list_tools", "mcp_tool_schema",
            "mcp_call", "search_knowledge",
        }] == [
            "search_knowledge", "read_document", "update_research_plan",
            "mcp_list_tools", "mcp_tool_schema",
            "mcp_call", "search_knowledge", "update_research_plan",
        ]
        assert all(item["timestamp"] for item in trace)
        assert {item["kind"] for item in result["evidence"]} == {"source", "tool_metadata"}
        assert runtime._graph is not None
        snapshots = [snapshot async for snapshot in runtime._graph.graph.aget_state_history(
            runtime._graph.config(OWNER, submitted["id"]),
        )]
        assert {("collect",), ("inspect",), ("decide",), ("tools",), ("reflect",)} <= {
            snapshot.next for snapshot in snapshots
        }
        assert snapshots[0].values["run"]["answer"] == result["answer"]
        assert result["metrics"]["model_calls"] == 7 + int(invalid_first)
        assert result["metrics"].get("decision_errors", 0) == int(invalid_first)
        assert len([request for request in peer.requests
                    if request["method"] == "tools/call"]) == 1
    finally:
        await runtime.stop()

    assert all(opened.is_closed for opened in clients)
    assert (tmp_path / "runs.sqlite3").exists() and (tmp_path / "checkpoints.sqlite3").exists()
    restored = HarnessRuntime(settings, source, router, hub)
    await restored.start()
    try:
        durable = await restored.get(OWNER, submitted["id"])
        assert durable["answer"] == result["answer"]
        assert durable["research_trace"] == trace
        assert durable["research_plan"]["ready_to_answer"] is True
        assert len(model.contexts) == 7, "Reading durable history must not rerun the investigation"
        await check_colleague_tool_reuse(restored, router, peer)
    finally:
        await restored.stop()


async def check_colleague_tool_reuse(
    runtime: HarnessRuntime, router: ModelRouter, peer: MCPPeer,
) -> None:
    """Only shared memory identifies which of the gateway's tools worked in the first run."""
    visible = (await runtime.list_memory(COLLEAGUE))["memories"]
    assert len(visible) == 1 and "source_run_id" not in visible[0]
    assert not (await runtime.list_memory(Principal("reader-without-jira")))["memories"]
    hints = visible[0]["strategy"]["successful_tools"]
    route = next(h for h in hints if h["tool"] == "mcp_call")
    assert route["remote_tool"] == REMOTE_TOOL and route["index_id"] == "connection:jira"
    assert route["evidence_ids"] and "arguments" not in route
    requests_before = len(peer.requests)
    model_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal model_calls
        model_calls += 1
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        saved = ctx["shared_memory_not_evidence"][0]["strategy"]["successful_tools"]
        hint = next(h for h in saved if h["tool"] == "mcp_call")
        assert hint == route
        name = hint["remote_tool"]
        args: dict[str, Any] = {
            "connection_id": hint["index_id"].removeprefix("connection:"), "tool": name,
        }
        evidence = {item["id"]: item for item in ctx["evidence"]}
        remote = [item for item in evidence.values() if item["index_id"] == hint["index_id"]]
        docs = [item for item in remote if item["kind"] == "source"]
        message: dict[str, Any]
        if docs:
            assert json.loads(docs[0]["text"])["issue_key"] == NEXT_ISSUE
            message = {"content": f"Статус задачи {NEXT_ISSUE} подтверждён [{docs[0]['id']}]."}
        else:
            # The current contract has already been read by the graph before
            # this user's first model request. Reasking for it must be local.
            assert len(remote) == 1
            schema = json.loads(remote[0]["text"])
            assert schema["name"] == name and schema["inputSchema"] == SCHEMA
            tool = "mcp_tool_schema"
            if model_calls > 1:
                current_issue = json.loads(evidence[DOCUMENT]["text"])["issue_key"]
                assert current_issue == NEXT_ISSUE
                args["arguments"] = {schema["inputSchema"]["required"][0]: current_issue}
                tool = "mcp_call"
            message = {"tool_calls": [{"type": "function", "function": {
                "name": tool, "arguments": json.dumps(args),
            }}]}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    router.transport = httpx.MockTransport(respond)
    submitted = await runtime.submit(COLLEAGUE, RunRequest(
        question=f"{QUESTION} Проверь {NEXT_ISSUE}.", mode="autonomous",
        index_ids=["knowledge", "connection:jira"],
    ))
    result = await finished(runtime, submitted["id"], COLLEAGUE)
    assert result["status"] == "completed", result["warnings"]
    assert NEXT_ISSUE in result["answer"]
    assert route in result["memory_used"][0]["successful_tools"]
    trace = result["research_trace"]
    first_model = next(i for i, step in enumerate(trace) if step["name"] == "model_response")
    assert any(step["name"] == "mcp_tool_schema" for step in trace[:first_model])
    assert any(step["name"] == "memory_route" and REMOTE_TOOL in step["result"]
               for step in trace[:first_model])
    assert sum(r["method"] == "tools/list" for r in peer.requests[requests_before:]) == 2
    calls = [r["params"] for r in peer.requests if r["method"] == "tools/call"]
    assert calls == [{"name": REMOTE_TOOL, "arguments": {"issue_key": issue}}
                     for issue in (ISSUE, NEXT_ISSUE)]
    # The connection can stay visible while this individual tool is revoked.
    assert isinstance(runtime.tools, IntegrationHub)
    integration_config = runtime.tools.store.load()
    jira = next(conn for conn in integration_config.connections if conn.id == "jira")
    jira.allowed_tools = ["other_permitted_tool"]
    runtime.tools.store.save(integration_config)
    durable = runtime.store.get(COLLEAGUE.owner_id, submitted["id"])
    routes = await runtime._initial_routes(COLLEAGUE, durable, {"knowledge"}, mcp=True)
    assert "mcp_tool_schema" not in routes
