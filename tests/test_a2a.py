from __future__ import annotations

import json
import time
from uuid import uuid4

import httpx
import pytest

from corporate_kb.a2a.store import A2AStore
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService


def indexed(settings_factory, **kwargs):
    settings = settings_factory(**kwargs)
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text("# Limits\n\nThe daily limit is 500.")
    service = KnowledgeService(
        settings, provider=HashEmbeddingProvider(settings.embedding_dimension)
    )
    service.build_index(force=True)
    return service, settings


def message(data=None, **configuration):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": str(uuid4()),
                "role": "ROLE_USER",
                "parts": [{"data": data} if data is not None else {"text": "daily limit"}],
            },
            "configuration": configuration,
        },
    }


def test_tokens_hashed_rotation_expiry_and_separate_usage(tmp_path):
    store = A2AStore(tmp_path)
    first = store.issue("Agent One", ["default"])
    assert first["token"].encode() not in store.path.read_bytes()
    assert store.authenticate(first["token"])["name"] == "Agent One"
    second = store.rotate(first["agent"]["id"])
    assert store.authenticate(first["token"]) is None
    assert store.authenticate(second["token"])
    store.record(
        second["agent"], "SendMessage", "success", 2, {"query": "Bearer " + second["token"]}
    )
    assert second["token"] not in json.dumps(store.calls())
    assert store.stats()["total_calls"] == 1
    assert A2AStore(tmp_path).stats()["total_calls"] == 1
    with store.db() as db:
        db.execute("UPDATE agents SET expires_at=?", (time.time() - 1,))
    assert store.authenticate(second["token"]) is None
    third = store.rotate(first["agent"]["id"])
    store.revoke(first["agent"]["id"])
    assert store.authenticate(third["token"]) is None


@pytest.mark.asyncio
async def test_a2a_protocol_scopes_history_identity_and_mcp_counter(settings_factory):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert card["capabilities"]["streaming"] is False
        issued = (
            await client.post(
                "/admin/api/a2a/agents", json={"name": "Agent One", "index_ids": ["default"]}
            )
        ).json()
        other = (
            await client.post(
                "/admin/api/a2a/agents", json={"name": "Agent Two", "index_ids": ["default"]}
            )
        ).json()
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0"}
        assert (await client.post("/a2a", json=message())).status_code == 401
        missing_version = await client.post(
            "/a2a", headers={"Authorization": headers["Authorization"]}, json=message()
        )
        assert missing_version.json()["error"]["code"] == -32009
        request = message(historyLength=0)
        answer = (await client.post("/a2a", headers=headers, json=request)).json()
        task = answer["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", answer
        assert "history" not in task
        result = task["artifacts"][0]["parts"][0]["data"]
        assert result["version_scope"] == "latest"
        hit = result["indexes"][0]["results"][0]
        assert hit["is_latest"] is True
        duplicate = (await client.post("/a2a", headers=headers, json=request)).json()["result"][
            "task"
        ]
        assert duplicate["id"] == task["id"]
        request["params"]["message"]["parts"] = [{"text": "different request"}]
        assert (await client.post("/a2a", headers=headers, json=request)).json()["error"][
            "code"
        ] == -32602
        stolen = await client.post(
            "/a2a",
            headers={**headers, "Authorization": "Bearer " + other["token"]},
            json={"jsonrpc": "2.0", "id": 4, "method": "GetTask", "params": {"id": task["id"]}},
        )
        assert stolen.json()["error"]["code"] == -32001
        denied = await client.post(
            "/a2a",
            headers=headers,
            json=message({"action": "search", "query": "x", "index_id": "private"}),
        )
        assert denied.json()["error"]["code"] == -32004
        listed = (
            await client.post(
                "/a2a",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "ListTasks",
                    "params": {"historyLength": 0},
                },
            )
        ).json()["result"]
        assert listed["totalSize"] == 1 and "artifacts" not in listed["tasks"][0]
        overview = (await client.get("/admin/api/a2a")).json()
        assert overview["stats"]["total_calls"] == 7
        assert overview["agents"][1]["call_count"] == 6
        assert (await client.get("/admin/api/overview")).status_code in {200, 404}
        # A2A never calls the shared MCP tools/counter.
        engine = next(item for item in app.state.integrations if hasattr(item, "catalog"))
        assert engine.catalog._usage.snapshot()["total_calls"] == 0
        await client.post(f"/admin/api/a2a/agents/{issued['agent']['id']}/revoke", json={})
        assert (await client.post("/a2a", headers=headers, json=message())).status_code == 401


@pytest.mark.asyncio
async def test_dashboard_a2a_calls_are_separate_and_survive_restart(settings_factory):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        initial = await client.get("/admin/api/overview")
        assert initial.status_code == 200
        assert initial.json()["a2a_usage"]["enabled"] is True
        assert initial.json()["a2a_usage"]["total_calls"] == 0
        issued = (
            await client.post(
                "/admin/api/a2a/agents", json={"name": "Dashboard Agent", "index_ids": ["default"]}
            )
        ).json()
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0"}
        # Existing REST/tool usage remains in its own process-local counter.
        assert (await client.get("/api/v1/stats")).status_code == 200
        assert (await client.post("/a2a", json=message())).status_code == 401
        sent = (await client.post("/a2a", headers=headers, json=message())).json()
        task_id = sent["result"]["task"]["id"]
        failed = await client.post(
            "/a2a", headers={"Authorization": headers["Authorization"]}, json=message()
        )
        assert failed.json()["error"]["code"] == -32009
        polled = await client.post(
            "/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task_id}},
        )
        assert polled.json()["result"]["id"] == task_id
        # Refreshing either dashboard only reads counters, including errors and task polling.
        for _ in range(2):
            overview = (await client.get("/admin/api/overview")).json()
            a2a_stats = (await client.get("/admin/api/a2a")).json()["stats"]
            assert overview["a2a_usage"] == {"enabled": True, **a2a_stats}
            assert overview["a2a_usage"] == {
                "enabled": True,
                "total_calls": 3,
                "successful_calls": 2,
                "failed_calls": 1,
                "active_agents": 1,
                "calls_last_minute": 3,
            }
            assert overview["usage"]["total_calls"] == 1
            assert overview["usage"]["calls_by_tool"] == {"kb_stats": 1}

    restarted_service = KnowledgeService(
        settings, provider=HashEmbeddingProvider(settings.embedding_dimension)
    )
    restarted_service.load_cached_index()
    restarted_app = create_http_app(restarted_service, settings)
    async with (
        restarted_app.router.lifespan_context(restarted_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_app), base_url="http://testserver"
        ) as client,
    ):
        overview = (await client.get("/admin/api/overview")).json()
        a2a_stats = (await client.get("/admin/api/a2a")).json()["stats"]
        assert overview["a2a_usage"] == {"enabled": True, **a2a_stats}
        assert overview["a2a_usage"]["total_calls"] == 3
        assert overview["a2a_usage"]["successful_calls"] == 2
        assert overview["a2a_usage"]["failed_calls"] == 1
        assert overview["usage"]["total_calls"] == 0


@pytest.mark.asyncio
async def test_a2a_management_requires_admin_but_agent_needs_only_token(settings_factory):
    service, settings = indexed(
        settings_factory,
        mcp_http_bearer_token="mcp-" + "x" * 40,
        admin_password="admin-password-long-enough",
    )
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client,
    ):
        assert (
            await client.post("/admin/api/a2a/agents", json={"name": "A", "index_ids": ["default"]})
        ).status_code == 403
        issued = await client.post(
            "/admin/api/a2a/agents",
            headers={"x-kb-admin-password": "admin-password-long-enough"},
            json={"name": "A", "index_ids": ["default"]},
        )
        assert issued.status_code == 201
        token = issued.json()["token"]
        assert (
            await client.get("/api/v1/stats", headers={"Authorization": "Bearer " + token})
        ).status_code == 401
        result = await client.post(
            "/a2a",
            headers={"Authorization": "Bearer " + token, "A2A-Version": "1.0"},
            json=message(),
        )
        assert result.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        assert (
            await client.get("/admin/api/a2a", headers={"Authorization": "Bearer " + token})
        ).status_code == 401


@pytest.mark.asyncio
async def test_async_a2a_tasks_poll_and_cancel(settings_factory):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        issued = (
            await client.post("/admin/api/a2a/agents", json={"name": "A", "index_ids": ["default"]})
        ).json()
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0"}
        engine = next(item for item in app.state.integrations if hasattr(item, "catalog"))
        import threading

        running, release = threading.Event(), threading.Event()
        original = engine.retrieve

        def slow(agent, command):
            running.set()
            release.wait(5)
            return original(agent, command)

        engine.retrieve = slow
        sent = (
            await client.post("/a2a", headers=headers, json=message(returnImmediately=True))
        ).json()
        task_id = sent["result"]["task"]["id"]
        assert running.wait(2)
        canceled = await client.post(
            "/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "CancelTask", "params": {"id": task_id}},
        )
        release.set()
        assert canceled.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        again = await client.post(
            "/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 3, "method": "CancelTask", "params": {"id": task_id}},
        )
        assert again.json()["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_admin_password_alone_protects_integration_logs(settings_factory):
    service, settings = indexed(settings_factory, admin_password="long-admin-password")
    app = create_http_app(service, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in ("/admin/api/a2a", "/admin/api/a2a/calls", "/admin/api/confluence"):
            assert (await client.get(path)).status_code == 401
            assert (
                await client.get(path, headers={"x-kb-admin-password": "long-admin-password"})
            ).status_code == 200


@pytest.mark.asyncio
async def test_wire_rejects_invalid_json_objects_and_nonfinite_ids(settings_factory):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        issued = (
            await client.post("/admin/api/a2a/agents", json={"name": "A", "index_ids": ["default"]})
        ).json()
        headers = {
            "Authorization": "Bearer " + issued["token"],
            "A2A-Version": "1.0",
            "Content-Type": "application/json",
        }
        for invalid, code in (
            ("[]", -32600),
            ('{"id":NaN}', -32700),
            ('{"id":Infinity}', -32700),
            ("{", -32700),
        ):
            result = await client.post("/a2a", content=invalid, headers=headers)
            assert result.status_code == 200 and result.json()["error"]["code"] == code


@pytest.mark.asyncio
async def test_disabled_integrations_have_explicit_status_and_no_wire_endpoint(settings_factory):
    service, settings = indexed(settings_factory, a2a_enabled=False, confluence_enabled=False)
    app = create_http_app(service, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/auth/a2a/status")).json()["enabled"] is False
        assert (await client.get("/admin/api/confluence")).json()["enabled"] is False
        assert (await client.post("/a2a", json=message())).status_code == 404
        overview = await client.get("/admin/api/overview")
        assert overview.status_code == 200
        assert overview.json()["a2a_usage"] == {
            "enabled": False,
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "active_agents": 0,
            "calls_last_minute": 0,
        }
