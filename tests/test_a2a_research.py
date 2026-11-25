from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from test_a2a import indexed, message

from corporate_kb.a2a.service import A2AService
from corporate_kb.agents.activation import GigaCodeAgentController
from corporate_kb.mcp.http_server import create_http_app


class Model(GigaCodeAgentController):
    def __init__(self, settings):
        self.enabled = False
        self.calls = 0
        self.cancelled = False
        self.block = False

    def status(self):
        return {
            "enabled": self.enabled,
            "available": self.enabled,
            "state": "ready" if self.enabled else "disabled",
            "message": "ready",
            "reason": "ready",
            "events": [],
            "authentication_url": "https://auth.example/login?code=ADMIN-SECRET",
        }

    def enable(self):
        self.enabled = True
        return self.status()

    def disable(self):
        self.enabled = False
        return self.status()

    def stop(self):
        self.disable()

    async def decide(self, *, messages, tools, cancel, **kwargs):
        self.calls += 1
        try:
            while self.block and not cancel.is_set():
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if cancel.is_set():
            self.cancelled = True
            raise asyncio.CancelledError
        assert "ADMIN-SECRET" not in json.dumps(messages)
        assert {t["function"]["name"] for t in tools} >= {"read_document", "search_knowledge"}
        if self.calls == 1:
            return {"content": "", "tool_calls": [{
                "name": "search_knowledge", "args": {"index_id": "default", "query": "daily limit"},
            }]}
        data = json.loads(messages[-1]["content"])
        if self.calls == 2:
            assert data[0]["index_id"] == "default"
            return {"content": "", "tool_calls": [{
                "name": "read_document", "args": {
                    "index_id": "default", "document_id": data[0]["document_id"],
                },
            }]}
        assert "500" in data["text"]
        return {"content": "Дневной лимит — 500 [" + data["id"] + "].", "tool_calls": []}


@pytest.fixture
def model_patch(monkeypatch):
    monkeypatch.setattr("corporate_kb.agents.activation.GigaCodeAgentController", Model)


async def terminal(client, headers, task_id):
    for _ in range(200):
        response = await client.post(
            "/a2a",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": "poll",
                "method": "GetTask",
                "params": {"id": task_id},
            },
        )
        task = response.json()["result"]
        if task["status"]["state"] not in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            return task
        await asyncio.sleep(0.02)
    pytest.fail("research did not finish")


async def test_activation_research_tool_loop_scopes_logs_and_counters(
    settings_factory, model_patch
):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        engine = next(x for x in app.state.integrations if isinstance(x, A2AService))
        issued = (
            await client.post(
                "/admin/api/a2a/agents",
                json={
                    "name": "Researcher",
                    "index_ids": ["default"],
                },
            )
        ).json()
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0"}
        assert (await client.get("/admin/api/a2a/runtime")).json()["state"] == "disabled"
        assert (await client.post("/admin/api/a2a/runtime/enable", json={})).json()["available"]
        answer = (await client.post("/a2a", headers=headers, json=message())).json()
        task = await terminal(client, headers, answer["result"]["task"]["id"])
        assert task["status"]["state"] == "TASK_STATE_COMPLETED", task
        result = task["artifacts"][0]["parts"][0]["data"]
        assert result["action"] == "research"
        assert "500" in result["answer"]
        assert result["metrics"]["model_calls"] == 3
        assert result["metrics"]["tool_calls"] == 2
        assert result["coverage"]["full_corpus"] is False
        assert result["citations"][0]["index_id"] == "default"
        assert "ADMIN-SECRET" not in json.dumps(task)
        total = engine.store.stats()["total_calls"]
        events = (await client.get("/admin/api/a2a/events")).json()["events"]
        assert {"search", "read", "finished"} <= {e["phase"] for e in events}
        assert {e["agent_name"] for e in events} == {"Researcher"}
        assert engine.store.stats()["total_calls"] == total
        overview = (await client.get("/admin/api/overview")).json()
        assert overview["usage"]["total_calls"] == 0
        forbidden = (
            await client.post(
                "/a2a",
                headers=headers,
                json=message(
                    {
                        "action": "research",
                        "query": "secret",
                        "index_id": "private",
                    }
                ),
            )
        ).json()
        assert forbidden["error"]["code"] == -32004


async def test_research_cancel_stops_model_and_never_publishes(settings_factory, model_patch):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        engine = next(x for x in app.state.integrations if isinstance(x, A2AService))
        controller = engine.research.controller
        controller.enable()
        controller.block = True
        issued = engine.store.issue("Cancel", ["default"])
        headers = {"Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0"}
        task = (await client.post("/a2a", headers=headers, json=message())).json()["result"]["task"]
        for _ in range(200):
            if controller.calls:
                break
            await asyncio.sleep(0.01)
        assert controller.calls
        cancel = await client.post(
            "/a2a",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "CancelTask",
                "params": {"id": task["id"]},
            },
        )
        assert cancel.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
        for _ in range(200):
            if controller.cancelled:
                break
            await asyncio.sleep(0.01)
        assert controller.cancelled
        assert "artifacts" not in engine.store.task(issued["agent"]["id"], task["id"])


async def test_runtime_authorization_url_is_admin_only(settings_factory, model_patch):
    service, settings = indexed(settings_factory, admin_password="secret-admin-long-enough")
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client,
    ):
        for method, path in [
            ("GET", "/runtime"),
            ("POST", "/runtime/enable"),
            ("POST", "/runtime/disable"),
        ]:
            denied = await client.request(method, "/admin/api/a2a" + path)
            assert denied.status_code == 401
            assert "ADMIN-SECRET" not in denied.text
        allowed = await client.get(
            "/admin/api/a2a/runtime",
            headers={
                "X-KB-Admin-Password": "secret-admin-long-enough",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["cache-control"] == "no-store"
        assert "ADMIN-SECRET" in allowed.text
        public = await client.get("/a2a")
        assert "ADMIN-SECRET" not in public.text


@pytest.mark.parametrize("change", ["revoke", "rotate"])
async def test_research_access_change_cancels_and_withholds_result(
    settings_factory, model_patch, change,
):
    service, settings = indexed(settings_factory)
    app = create_http_app(service, settings)
    async with app.router.lifespan_context(app):
        engine = next(x for x in app.state.integrations if isinstance(x, A2AService))
        controller = engine.research.controller
        controller.enable()
        controller.block = True
        issued = engine.store.issue("Revocation test", ["default"])
        agent = issued["agent"]
        task = engine.dispatch(agent, "SendMessage", message()["params"])["task"]
        for _ in range(200):
            if controller.calls:
                break
            await asyncio.sleep(0.01)
        assert controller.calls
        getattr(engine.store, change)(agent["id"])
        for _ in range(200):
            saved = engine.store.task(agent["id"], task["id"])
            if saved["status"]["state"] == "TASK_STATE_FAILED":
                break
            await asyncio.sleep(0.01)
        assert controller.cancelled
        assert saved["status"]["state"] == "TASK_STATE_FAILED"
        assert "artifacts" not in saved
        assert engine.store.authenticate(issued["token"]) is None
