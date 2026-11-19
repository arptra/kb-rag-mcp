from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from test_a2a import indexed, message

from corporate_kb.a2a.service import A2AService
from corporate_kb.a2a.store import now
from corporate_kb.mcp.http_server import create_http_app


@asynccontextmanager
async def wire(settings_factory):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        issued = await client.post(
            "/admin/api/a2a/agents", json={"name": "Wire Agent", "index_ids": ["default"]}
        )
        assert issued.status_code == 201
        engine = next(item for item in app.state.integrations if isinstance(item, A2AService))
        yield client, engine, issued.json()


def legacy_message(**configuration):
    request = message(**configuration)
    request["method"] = "message/send"
    item = request["params"]["message"]
    item.update(kind="message", role="user")
    item["parts"][0]["kind"] = "text"
    return request


@pytest.mark.parametrize("version", ["1.0", "0.3"])
async def test_endpoint_explains_connection_without_credentials_or_counting_a_call(
    settings_factory, version
):
    async with wire(settings_factory) as (client, engine, issued):
        response = await client.get("/a2a", params={"A2A-Version": version})
        assert response.status_code == 200
        contract = response.json()
        assert contract["protocolVersion"] == version
        assert issued["token"] not in response.text
        assert issued["agent"]["id"] not in response.text
        assert engine.store.stats()["total_calls"] == 0
        headers = {**contract["headers"], "Authorization": "Bearer " + issued["token"]}
        example = contract["requestExample"]
        example["params"]["message"]["messageId"] = str(uuid4())
        answer = await client.post(contract["url"], headers=headers, json=example)
        result = answer.json()["result"]
        assert (result["task"] if version == "1.0" else result)["artifacts"]


async def test_unknown_legacy_method_returns_a_working_example_in_the_same_version(
    settings_factory,
):
    async with wire(settings_factory) as (client, _, issued):
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "0.3"}
        response = await client.post(
            "/a2a", headers=headers,
            json={"jsonrpc": "2.0", "id": "bad-method", "method": "message/sned", "params": {}},
        )
        error = response.json()["error"]
        assert error["code"] == -32601
        assert "Unknown A2A 0.3 method" in error["message"]
        assert error["data"]["supportedMethods"] == ["message/send", "tasks/get", "tasks/cancel"]
        example = error["data"]["requestExample"]
        example["params"]["message"]["messageId"] = str(uuid4())
        recovered = await client.post("/a2a", headers=headers, json=example)
        assert recovered.json()["result"]["status"]["state"] == "completed"


async def test_downloaded_config_executes_native_contract_and_keeps_new_questions_distinct(
    settings_factory,
):
    async with wire(settings_factory) as (client, _, issued):
        config = issued["config"]
        assert config["protocol"] == "A2A"
        assert config["protocolVersion"] == "1.0"
        assert config["protocolBinding"] == "JSONRPC"
        assert "messageId" in config["instructions"]
        assert "MCP" in config["instructions"]
        assert set(config["supportedMethods"]) == {
            "SendMessage",
            "GetTask",
            "ListTasks",
            "CancelTask",
        }
        task_ids = []
        for _ in range(2):
            request = deepcopy(config["requestExample"])
            request["params"]["message"]["messageId"] = str(uuid4())
            answer = await client.post(config["url"], headers=config["headers"], json=request)
            assert answer.status_code == 200
            assert answer.headers["A2A-Version"] == "1.0"
            task = answer.json()["result"]["task"]
            assert task["status"]["state"] == "TASK_STATE_COMPLETED"
            assert "kind" not in task
            assert task["history"][0]["role"] == "ROLE_USER"
            assert "kind" not in task["artifacts"][0]["parts"][0]
            task_ids.append(task["id"])
        assert len(set(task_ids)) == 2


@pytest.mark.parametrize("version", [None, "0.3", "0.3.0"])
async def test_legacy_send_get_preserves_original_method_and_separate_usage(
    settings_factory, version
):
    async with wire(settings_factory) as (client, engine, issued):
        headers = {"Authorization": "Bearer " + issued["token"]}
        if version:
            headers["A2A-Version"] = version
        request = legacy_message(blocking=True)
        response = await client.post("/a2a", headers=headers, json=request)
        assert response.status_code == 200
        assert response.headers["A2A-Version"] == "0.3"
        task = response.json()["result"]
        assert "task" not in task
        assert task["kind"] == "task"
        assert task["status"]["state"] == "completed"
        assert task["history"][0] == request["params"]["message"]
        part = task["artifacts"][0]["parts"][0]
        assert part["kind"] == "data"
        assert part["data"]["version_scope"] == "latest"
        assert part["data"]["indexes"][0]["results"]
        repeated = await client.post("/a2a", headers=headers, json=request)
        assert repeated.json()["result"]["id"] == task["id"]
        fetched = await client.post(
            "/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task["id"]}},
        )
        assert fetched.json()["result"] == task
        calls = engine.store.calls()["calls"]
        assert [item["method"] for item in calls] == ["message/send", "message/send", "tasks/get"]
        assert all(item["agent_id"] == issued["agent"]["id"] for item in calls)
        assert all(item["status"] == "success" for item in calls)
        assert engine.store.stats()["total_calls"] == 3
        assert engine.catalog._usage.snapshot()["total_calls"] == 0


async def test_legacy_translation_does_not_rewrite_nested_knowledge_data(
    settings_factory, monkeypatch
):
    async with wire(settings_factory) as (client, engine, issued):
        application_data = {
            "role": "ROLE_AGENT",
            "kind": "domain-value",
            "status": {"state": "TASK_STATE_WORKING"},
            "task": {"parts": [{"data": {"role": "ROLE_USER"}}]},
        }
        monkeypatch.setattr(engine, "retrieve", lambda *_: deepcopy(application_data))
        response = await client.post(
            "/a2a", headers={"Authorization": "Bearer " + issued["token"]}, json=legacy_message()
        )
        task = response.json()["result"]
        assert task["kind"] == "task" and task["status"]["state"] == "completed"
        assert task["artifacts"][0]["parts"][0] == {"kind": "data", "data": application_data}
        # Stored tasks remain native, allowing either protocol to fetch the same task.
        stored = engine.store.task(issued["agent"]["id"], task["id"])
        assert stored["status"]["state"] == "TASK_STATE_COMPLETED"
        assert "kind" not in stored["artifacts"][0]["parts"][0]


async def test_legacy_task_get_and_cancel_keep_agent_ownership(settings_factory):
    async with wire(settings_factory) as (client, engine, issued):
        other = engine.store.issue("Other Agent", ["default"])
        task = {
            "id": str(uuid4()),
            "contextId": str(uuid4()),
            "status": {"state": "TASK_STATE_WORKING", "timestamp": now()},
        }
        engine.store.create_task(issued["agent"]["id"], str(uuid4()), "test-fingerprint", task)
        request = {"jsonrpc": "2.0", "id": 1, "params": {"id": task["id"]}}
        for method in ("tasks/get", "tasks/cancel"):
            denied = await client.post(
                "/a2a",
                headers={"Authorization": "Bearer " + other["token"]},
                json={**request, "method": method},
            )
            assert denied.json()["error"]["code"] == -32001
        assert engine.store.task(issued["agent"]["id"], task["id"])["status"]["state"] == (
            "TASK_STATE_WORKING"
        )
        canceled = await client.post(
            "/a2a",
            headers={"Authorization": "Bearer " + issued["token"]},
            json={**request, "method": "tasks/cancel"},
        )
        assert canceled.json()["result"]["kind"] == "task"
        assert canceled.json()["result"]["status"]["state"] == "canceled"


async def test_discovery_selects_native_or_legacy_card_without_credentials(settings_factory):
    async with wire(settings_factory) as (client, engine, _):
        native = await client.get("/.well-known/agent-card.json")
        assert native.json()["supportedInterfaces"][0]["protocolVersion"] == "1.0"
        assert native.headers["A2A-Version"] == "1.0"
        variants = [
            ("/.well-known/agent-card.json", {"A2A-Version": "0.3"}),
            ("/.well-known/agent-card.json?A2A-Version=0.3", {}),
            ("/.well-known/agent.json", {}),
        ]
        for url, headers in variants:
            response = await client.get(url, headers=headers)
            assert response.status_code == 200
            card = response.json()
            assert card["protocolVersion"] == "0.3.0"
            assert card["url"] == "http://testserver/a2a"
            assert card["preferredTransport"] == "JSONRPC"
            assert "supportedInterfaces" not in card
            assert card["securitySchemes"]["agentToken"]["type"] == "http"
            assert card["security"] == [{"agentToken": []}]
            assert card["capabilities"]["streaming"] is False
        assert engine.store.stats()["total_calls"] == 0


@pytest.mark.parametrize("version", [None, "9.9"])
async def test_version_rejection_retains_request_id_method_and_agent_in_journal(
    settings_factory, version
):
    async with wire(settings_factory) as (client, engine, issued):
        headers = {"Authorization": "Bearer " + issued["token"]}
        if version:
            headers["A2A-Version"] = version
        request = message()
        request["id"] = "version-probe"
        response = await client.post("/a2a", headers=headers, json=request)
        payload = response.json()
        assert payload["id"] == "version-probe"
        assert payload["error"]["code"] == -32009
        assert payload["error"]["data"]["protocolVersion"] == "1.0"
        (call,) = engine.store.calls()["calls"]
        assert call["method"] == "SendMessage"
        assert call["agent_id"] == issued["agent"]["id"]
        assert call["status"] == "error"


@pytest.mark.parametrize(
    ("method", "version", "hint"),
    [
        ("message/send", "1.0", "0.3"),
        ("made-up-method", None, "Supported methods"),
    ],
)
async def test_wrong_protocol_methods_have_actionable_errors_and_do_not_execute(
    settings_factory, method, version, hint
):
    async with wire(settings_factory) as (client, engine, issued):
        headers = {"Authorization": "Bearer " + issued["token"]}
        if version:
            headers["A2A-Version"] = version
        request = {"jsonrpc": "2.0", "id": "probe", "method": method, "params": {}}
        assert (await client.post("/a2a", json=request)).status_code == 401
        payload = (await client.post("/a2a", headers=headers, json=request)).json()
        assert payload["id"] == "probe"
        assert "result" not in payload
        assert payload["error"]["code"] == -32601
        assert hint in payload["error"]["message"]
        expected_send = "message/send" if method == "made-up-method" else "SendMessage"
        assert expected_send in payload["error"]["data"]["supportedMethods"]
        calls = engine.store.calls()["calls"]
        assert len(calls) == 1
        assert calls[0]["method"] == method
        assert calls[0]["agent_id"] == issued["agent"]["id"]
        assert engine.store.stats()["failed_calls"] == 1
        assert engine.catalog._usage.snapshot()["total_calls"] == 0
        with engine.store.db() as db:
            assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
