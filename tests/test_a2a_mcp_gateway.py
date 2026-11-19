from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client, streamablehttp_client
from test_a2a_wire_compat import wire

from corporate_kb.a2a.mcp_gateway import MCP_VERSION
from corporate_kb.a2a.store import now


def headers(issued, **extra):
    return {
        "Authorization": "Bearer " + issued["token"],
        "Accept": "application/json, text/event-stream",
        **extra,
    }


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def initialize(version=MCP_VERSION):
    return rpc(
        "initialize",
        {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "GigaAgent", "version": "test"},
        },
    )


async def test_real_mcp_sdk_discovers_reads_and_preserves_separate_usage(settings_factory):
    async with wire(settings_factory) as (client, engine, issued):
        client.headers.update(headers(issued))
        async with (
            streamable_http_client("http://testserver/a2a", http_client=client) as (
                read,
                write,
                session_id,
            ),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session,
        ):
            result = await session.initialize()
            assert result.protocolVersion == MCP_VERSION
            assert result.capabilities.tools and not result.capabilities.tools.listChanged
            assert session_id() is None
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "ask_knowledge_agent",
                "get_knowledge_task",
                "cancel_knowledge_task",
                "search_knowledge",
                "read_knowledge_document",
            }
            assert (await session.list_resources()).resources == []
            assert (await session.list_resource_templates()).resourceTemplates == []
            assert (await session.list_prompts()).prompts == []
            await session.send_ping()
            result = await session.call_tool("search_knowledge", {"query": "daily limit"})
            assert not result.isError
            evidence = result.structuredContent
            hit = evidence["indexes"][0]["results"][0]
            document = await session.call_tool(
                "read_knowledge_document",
                {
                    "index_id": "default",
                    "document_id": hit["document_id"],
                },
            )
            assert not document.isError
            assert "500" in document.content[0].text
        calls = engine.store.calls()["calls"]
        assert len(calls) == 9
        assert all(call["agent_id"] == issued["agent"]["id"] for call in calls)
        assert all(call["status"] == "success" for call in calls)
        assert engine.store.stats()["total_calls"] == 9
        assert engine.catalog._usage.snapshot()["total_calls"] == 0
        assert issued["token"] not in json.dumps(calls)
        assert all(call["transport"] == "mcp" for call in calls)
        assert calls[-1]["tool"] == "read_knowledge_document"


async def test_gigaagent_older_streamablehttp_sdk_alias_works(settings_factory):
    async with wire(settings_factory) as (client, _, issued):
        # GigaAgent's public implementation uses the older SDK function name.
        def factory(**kwargs):
            return httpx.AsyncClient(
                transport=client._transport, base_url="http://testserver", **kwargs
            )

        with pytest.warns(DeprecationWarning):
            async with (
                streamablehttp_client(
                    "http://testserver/a2a",
                    headers=headers(issued),
                    httpx_client_factory=factory,
                ) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert (await session.list_tools()).tools


async def test_gateway_config_is_copyable_and_get_sse_is_optional(settings_factory):
    async with wire(settings_factory) as (client, _, issued):
        config = issued["mcp_config"]
        server = config["mcpServers"]["corporate-knowledge-agent"]
        assert server["url"] == "http://testserver/a2a"
        assert server["headers"] == {"Authorization": "Bearer " + issued["token"]}
        assert issued["config"]["mcpServers"] == config["mcpServers"]
        assert issued["config"]["protocol"] == "A2A"
        help_response = await client.get("/a2a")
        assert help_response.status_code == 200
        assert issued["token"] not in help_response.text
        no_sse = await client.get("/a2a", headers={"Accept": "text/event-stream"})
        assert no_sse.status_code == 405 and not no_sse.content


@pytest.mark.parametrize("version", ["2025-03-26", "2025-06-18", "2025-11-25"])
async def test_initialize_negotiates_only_supported_versions(settings_factory, version):
    async with wire(settings_factory) as (client, _, issued):
        answer = await client.post("/a2a", headers=headers(issued), json=initialize(version))
        assert answer.json()["result"]["protocolVersion"] == (
            version if version != "2025-11-25" else MCP_VERSION
        )
        assert "a2a-version" not in answer.headers
        assert "mcp-session-id" not in answer.headers
        invalid = await client.post(
            "/a2a",
            headers=headers(
                issued,
                **{
                    "MCP-Protocol-Version": "unsupported",
                },
            ),
            json=rpc("ping"),
        )
        assert invalid.status_code == 400


async def test_mcp_gateway_requires_auth_and_revocation_takes_effect(settings_factory):
    async with wire(settings_factory) as (client, engine, issued):
        assert (await client.post("/a2a", json=initialize())).status_code == 401
        assert engine.store.stats()["total_calls"] == 0
        assert (
            "result"
            in (
                await client.post(
                    "/a2a",
                    headers=headers(issued),
                    json=initialize(),
                )
            ).json()
        )
        engine.store.revoke(issued["agent"]["id"])
        assert (
            await client.post("/a2a", headers=headers(issued), json=rpc("tools/list"))
        ).status_code == 401
        assert engine.store.stats()["total_calls"] == 1


async def test_mcp_gateway_rechecks_token_after_body(settings_factory, monkeypatch):
    async with wire(settings_factory) as (client, engine, issued):
        authenticate = engine.store.authenticate
        calls = 0

        def revoke_between_authentication(token):
            nonlocal calls
            calls += 1
            if calls == 2:
                engine.store.revoke(issued["agent"]["id"])
            return authenticate(token)

        monkeypatch.setattr(engine.store, "authenticate", revoke_between_authentication)
        result = await client.post("/a2a", headers=headers(issued), json=rpc("tools/list"))
        assert result.status_code == 401
        assert engine.store.stats()["total_calls"] == 1
        assert engine.store.calls()["calls"][0]["status"] == "error"


async def test_tools_enforce_index_and_task_ownership(settings_factory):
    async with wire(settings_factory) as (client, engine, issued):
        denied = await client.post(
            "/a2a",
            headers=headers(issued),
            json=rpc(
                "tools/call",
                {
                    "name": "search_knowledge",
                    "arguments": {"query": "x", "index_id": "private"},
                },
            ),
        )
        assert denied.json()["result"]["isError"] is True
        assert "no access" in denied.text
        assert "no access" in engine.store.calls()["calls"][0]["error"]
        other = engine.store.issue("Other", ["default"])
        task = {
            "id": str(uuid4()),
            "contextId": str(uuid4()),
            "status": {"state": "TASK_STATE_WORKING", "timestamp": now()},
        }
        engine.store.create_task(issued["agent"]["id"], str(uuid4()), "fingerprint", task)
        for name in ("get_knowledge_task", "cancel_knowledge_task"):
            result = await client.post(
                "/a2a",
                headers=headers(other),
                json=rpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": {"task_id": task["id"]},
                    },
                ),
            )
            assert result.json()["result"]["isError"]
            assert "not found" in result.text
        assert engine.store.task(issued["agent"]["id"], task["id"])["status"]["state"] == (
            "TASK_STATE_WORKING"
        )
        cancelled = await client.post(
            "/a2a",
            headers=headers(issued),
            json=rpc(
                "tools/call",
                {
                    "name": "cancel_knowledge_task",
                    "arguments": {"task_id": task["id"]},
                },
            ),
        )
        assert not cancelled.json()["result"]["isError"]
        assert "TASK_STATE_CANCELED" in cancelled.text
        assert engine.catalog._usage.snapshot()["total_calls"] == 0


async def test_research_is_async_and_polls_same_scoped_a2a_task(settings_factory, monkeypatch):
    async with wire(settings_factory) as (client, engine, issued):
        dispatched = []
        task = {"id": str(uuid4()), "status": {"state": "TASK_STATE_WORKING"}}

        def dispatch(agent, method, params):
            dispatched.append((agent, method, params))
            return {"task": task} if method == "SendMessage" else task

        monkeypatch.setattr(engine, "dispatch", dispatch)
        result = await client.post(
            "/a2a",
            headers=headers(
                issued,
                **{
                    "MCP-Protocol-Version": MCP_VERSION,
                },
            ),
            json=rpc(
                "tools/call",
                {
                    "name": "ask_knowledge_agent",
                    "arguments": {
                        "query": "Explain limits",
                        "index_id": "default",
                        "mode": "investigate",
                        "effort": "high",
                    },
                },
            ),
        )
        data = result.json()["result"]["structuredContent"]
        assert data["task_id"] == task["id"] and data["next_tool"] == "get_knowledge_task"
        agent, method, params = dispatched[0]
        assert agent["id"] == issued["agent"]["id"] and method == "SendMessage"
        assert params["configuration"]["returnImmediately"] is True
        assert params["message"]["parts"][0]["data"] == {
            "action": "research",
            "query": "Explain limits",
            "index_id": "default",
            "mode": "investigate",
            "effort": "high",
        }
        await client.post(
            "/a2a",
            headers=headers(issued),
            json=rpc(
                "tools/call",
                {
                    "name": "get_knowledge_task",
                    "arguments": {"task_id": data["task_id"]},
                },
            ),
        )
        assert dispatched[1][1:] == ("GetTask", {"id": task["id"]})
        assert engine.store.stats()["total_calls"] == 2


@pytest.mark.parametrize(
    "notification",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "method": "notifications/roots/list_changed"},
    ],
)
async def test_notifications_return_empty_accepted_and_count_once(settings_factory, notification):
    async with wire(settings_factory) as (client, engine, issued):
        result = await client.post("/a2a", headers=headers(issued), json=notification)
        assert result.status_code == 202 and not result.content
        assert engine.store.stats()["total_calls"] == 1
        assert engine.store.calls()["calls"][0]["method"] == notification["method"]


@pytest.mark.parametrize(
    "payload",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": []},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": True}},
        {"jsonrpc": "1.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "tools/call", "params": {}},
    ],
)
async def test_malformed_notifications_never_create_work(settings_factory, payload):
    async with wire(settings_factory) as (client, engine, issued):
        response = await client.post("/a2a", headers=headers(issued), json=payload)
        assert response.status_code == 400
        assert "id" not in response.json()
        assert engine.store.stats()["failed_calls"] == 1
        with engine.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("method", "params", "code"),
    [
        ("resources/read", {"uri": "missing://resource"}, -32002),
        ("resources/subscribe", {"uri": "missing://resource"}, -32002),
        ("resources/unsubscribe", {"uri": "missing://resource"}, -32002),
        ("prompts/get", {"name": "missing"}, -32602),
        ("tools/call", {"name": "missing"}, -32602),
    ],
)
async def test_missing_resource_prompt_tool_are_semantic_errors(
    settings_factory, method, params, code
):
    async with wire(settings_factory) as (client, _, issued):
        response = await client.post("/a2a", headers=headers(issued), json=rpc(method, params))
        assert response.json()["error"]["code"] == code


async def test_mcp_accept_origin_and_tool_schema_are_enforced(settings_factory):
    async with wire(settings_factory) as (client, engine, issued):
        denied_origin = await client.post(
            "/a2a", headers=headers(issued, Origin="https://evil.test"), json=initialize()
        )
        assert denied_origin.status_code == 403
        same_origin = await client.post(
            "/a2a", headers=headers(issued, Origin="http://testserver"), json=initialize()
        )
        assert "result" in same_origin.json()
        invalid_accept = await client.post(
            "/a2a", headers=headers(issued, Accept="application/json"), json=initialize()
        )
        assert invalid_accept.status_code == 406
        invalid = await client.post(
            "/a2a",
            headers=headers(issued),
            json=rpc(
                "tools/call",
                {
                    "name": "search_knowledge",
                    "arguments": {"query": "limits", "top_k": True},
                },
            ),
        )
        assert invalid.json()["result"]["isError"] is True
        assert "top_k" in invalid.text
        assert engine.store.stats()["total_calls"] == 4


async def test_browser_gateway_cors_is_explicit_and_preserves_bearer_auth(settings_factory):
    origin = "https://gigaagent.example"
    def factory(**kwargs):
        return settings_factory(a2a_allowed_origins=(origin,), **kwargs)
    async with wire(factory) as (client, engine, issued):
        request_headers = {
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,mcp-protocol-version",
        }
        preflight = await client.options("/a2a", headers=request_headers)
        assert preflight.status_code == 204
        assert preflight.headers["Access-Control-Allow-Origin"] == origin
        assert "POST" in preflight.headers["Access-Control-Allow-Methods"]
        assert "access-control-allow-credentials" not in preflight.headers
        assert "Origin" in preflight.headers["Vary"]
        no_auth = await client.post("/a2a", headers={"Origin": origin}, json=initialize())
        assert no_auth.status_code == 401
        assert no_auth.headers["Access-Control-Allow-Origin"] == origin
        answer = await client.post(
            "/a2a", headers=headers(issued, Origin=origin), json=initialize()
        )
        assert answer.json()["result"]["protocolVersion"] == MCP_VERSION
        assert answer.headers["Access-Control-Allow-Origin"] == origin
        assert "access-control-allow-credentials" not in answer.headers
        stream = await client.get("/a2a", headers={"Origin": origin, "Accept": "text/event-stream"})
        assert stream.status_code == 405
        assert stream.headers["Access-Control-Allow-Origin"] == origin
        for invalid in ("https://evil.example", "null", "https://gigaagent.example.evil.test"):
            denied = await client.options("/a2a", headers={**request_headers, "Origin": invalid})
            assert denied.status_code == 403
            assert "access-control-allow-origin" not in denied.headers
            denied_post = await client.post(
                "/a2a", headers=headers(issued, Origin=invalid), json=initialize()
            )
            assert denied_post.status_code == 403
            assert "access-control-allow-origin" not in denied_post.headers
        unsupported_header = await client.options(
            "/a2a",
            headers={
                **request_headers,
                "Access-Control-Request-Headers": "X-Admin-Password",
            },
        )
        assert unsupported_header.status_code == 400
        # Preflights and unauthenticated requests never inflate an agent's call counter.
        assert engine.store.stats()["total_calls"] == 4


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "null",
        "https://*.example",
        "https://site/path",
        "https://user:pass@site",
        "https://site?token=x",
        "ftp://site",
    ],
)
def test_browser_allowed_origins_reject_wildcards_credentials_and_paths(settings_factory, origin):
    with pytest.raises(ValueError, match="origins"):
        settings_factory(a2a_allowed_origins=(origin,))


def test_browser_allowed_origins_can_be_configured_as_json_environment(
    settings_factory, monkeypatch
):
    monkeypatch.setenv("KB_A2A_ALLOWED_ORIGINS", '["https://GIGAAGENT.example/"]')
    assert settings_factory().a2a_allowed_origins == ("https://gigaagent.example",)
