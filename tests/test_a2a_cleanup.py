from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, current_thread
from uuid import uuid4

import httpx
import pytest

from corporate_kb.a2a.service import A2AService
from corporate_kb.a2a.store import A2AStore, now
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService


def task():
    return {"id": str(uuid4()), "contextId": str(uuid4()),
            "status": {"state": "TASK_STATE_WORKING", "timestamp": now()}}


def app_for(settings_factory, **overrides):
    settings = settings_factory(**overrides)
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text("# Limits\n\nDaily limit is 500.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index(force=True)
    app = create_http_app(service, settings)
    engine = next(item for item in app.state.integrations if isinstance(item, A2AService))
    return app, engine


def test_cleanup_preserves_usage_and_other_agents_after_restart(tmp_path):
    store = A2AStore(tmp_path)
    first = store.issue("First", ["default"])["agent"]
    second = store.issue("Second", ["default"])["agent"]
    for agent, status in ((first, "success"), (first, "error"), (second, "success")):
        store.record(agent, "SendMessage", status, 1, {})
        store.record_event(agent, "task", "searching", "Reading sources")
    before = store.stats()
    last_call = store.calls()["next_cursor"]
    last_event = store.events()["next_cursor"]
    assert store.clear_calls(first["id"])["deleted_count"] == 2
    assert store.clear_events(first["id"])["deleted_count"] == 2
    assert {row["agent_id"] for row in store.calls()["calls"]} == {second["id"]}
    assert {row["agent_id"] for row in store.events()["events"]} == {second["id"]}
    assert store.stats() == before
    assert {row["id"]: row["call_count"] for row in store.agents()} == {
        first["id"]: 2, second["id"]: 1,
    }
    assert store.clear_calls()["deleted_count"] == 1
    assert store.clear_events()["deleted_count"] == 1
    reopened = A2AStore(tmp_path)
    assert reopened.stats() == before
    assert reopened.calls()["calls"] == reopened.events()["events"] == []
    reopened.record(first, "GetTask", "success", 1, {})
    reopened.record_event(first, "task", "finished", "Done")
    assert reopened.stats()["total_calls"] == 4
    assert reopened.stats()["successful_calls"] == 3
    assert reopened.stats()["failed_calls"] == 1
    assert reopened.stats()["calls_last_minute"] == 4
    assert reopened.calls(cursor=last_call)["calls"][0]["id"] > last_call
    assert reopened.events(cursor=last_event)["events"][0]["id"] > last_event
    assert next(row for row in reopened.agents() if row["id"] == first["id"])["call_count"] == 3


def test_old_journal_usage_migrates_once_without_double_counting(tmp_path):
    store = A2AStore(tmp_path)
    agent = store.issue("Existing", ["default"])["agent"]
    store.record(agent, "SendMessage", "success", 2, {})
    store.record(agent, "SendMessage", "error", 2, {})
    # This is the pre-migration schema: counters existed only as call-log rows.
    with store.db() as db:
        db.execute("UPDATE calls SET timestamp=0 WHERE status='error'")
        db.execute("DROP TABLE usage_totals")
        db.execute("DROP TABLE usage_recent")
        db.execute("DROP TABLE schema_migrations")
    with ThreadPoolExecutor(max_workers=2) as pool:
        restored = list(pool.map(lambda _: A2AStore(tmp_path), range(2)))
    for reopened in restored:
        assert reopened.stats() == {
            "total_calls": 2, "successful_calls": 1, "failed_calls": 1,
            "active_agents": 1, "calls_last_minute": 1,
        }
        assert reopened.agents()[0]["call_count"] == 2
    restored[0].clear_calls()
    assert A2AStore(tmp_path).stats()["total_calls"] == 2


def test_delete_removes_credentials_tasks_events_but_preserves_external_calls(tmp_path):
    store = A2AStore(tmp_path)
    issued = store.issue("Deleted", ["default"])
    agent = issued["agent"]
    other = store.issue("Kept", ["default"])["agent"]
    first, second = task(), task()
    store.create_task(agent["id"], "one", "one", first)
    store.create_task(other["id"], "two", "two", second)
    for owner, item in ((agent, first), (other, second)):
        store.record(owner, "SendMessage", "success", 1, {})
        store.record_event(owner, item["id"], "searching", "Read")
    assert store.delete_agent(agent["id"]) == {
        "deleted": True, "agent_id": agent["id"], "deleted_tasks": 1, "deleted_events": 1,
    }
    assert store.authenticate(issued["token"]) is None
    assert not store.authorized_snapshot(agent)
    with pytest.raises(KeyError):
        store.task(agent["id"], first["id"])
    assert store.task(other["id"], second["id"]) == second
    assert len(store.calls()["calls"]) == 2
    assert store.stats()["total_calls"] == 2
    assert [item["agent_id"] for item in store.events()["events"]] == [other["id"]]
    # An already-running worker cannot recreate state after deletion.
    store.record_event(agent, first["id"], "finished", "Late update")
    assert not store.save_task(agent["id"], first)
    with pytest.raises(PermissionError):
        store.create_task(agent["id"], "late", "late", task())
    assert store.events(agent_id=agent["id"])["events"] == []
    assert A2AStore(tmp_path).agents()[0]["id"] == other["id"]
    with pytest.raises(KeyError):
        store.delete_agent(agent["id"])


def test_rotation_and_deletion_are_serialized(tmp_path, monkeypatch):
    store = A2AStore(tmp_path)
    issued = store.issue("Rotating", ["default"])
    agent_id = issued["agent"]["id"]
    original_db = store.db
    checked, release, deleting = Event(), Event(), Event()

    class Connection:
        def __init__(self, db):
            self.db = db

        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE" and current_thread().name.startswith("delete"):
                deleting.set()
            result = self.db.execute(sql, *args)
            if sql == "SELECT id FROM agents WHERE id=?":
                checked.set()
                assert release.wait(3)
            return result

    @contextmanager
    def paused_db():
        with original_db() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "db", paused_db)
    with (
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="rotate") as rotation_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="delete") as deletion_pool,
    ):
        rotation = rotation_pool.submit(store.issue, "Rotating", ["default"], agent_id=agent_id)
        try:
            assert checked.wait(2)
            deletion = deletion_pool.submit(store.delete_agent, agent_id)
            assert deleting.wait(2)
            # Deletion waits for the entire rotation, not only its last UPDATE.
            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.05)
        finally:
            release.set()
        rotated = rotation.result(timeout=2)
        assert deletion.result(timeout=2)["deleted"] is True
    assert store.authenticate(rotated["token"]) is None
    with pytest.raises(KeyError):
        store.rotate(agent_id)


@pytest.mark.asyncio
async def test_cleanup_endpoints_require_admin_and_filter_journals(settings_factory):
    app, engine = app_for(
        settings_factory, admin_password="cleanup-admin-password",
        mcp_http_bearer_token="reader-" + "x" * 40,
    )
    first = engine.store.issue("First", ["default"])
    second = engine.store.issue("Second", ["default"])
    for issued in (first, second):
        engine.store.record(issued["agent"], "SendMessage", "success", 1, {})
        engine.store.record_event(issued["agent"], "task", "searching", "Read")
    admin = {"x-kb-admin-password": "cleanup-admin-password"}
    agent_id = first["agent"]["id"]
    paths = (f"/admin/api/a2a/agents/{agent_id}", "/admin/api/a2a/calls",
             "/admin/api/a2a/events")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        for headers in ({}, {"Authorization": "Bearer " + first["token"]},
                        {"Authorization": "Bearer reader-" + "x" * 40}):
            for path in paths:
                assert (await client.delete(path, headers=headers)).status_code in {401, 403}
        for path in paths[1:]:
            assert (await client.delete(path + "?agent_id=", headers=admin)).status_code == 400
        assert engine.store.stats()["total_calls"] == 2
        for path in paths[1:]:
            cleared = await client.delete(path, params={"agent_id": agent_id}, headers=admin)
            assert cleared.status_code == 200
            assert cleared.json() == {"deleted_count": 1, "agent_id": agent_id}
        assert len(engine.store.calls()["calls"]) == len(engine.store.events()["events"]) == 1
        assert engine.store.stats()["total_calls"] == 2
        assert (await client.delete(paths[0], headers=admin)).json()["deleted"] is True
        assert (await client.delete(paths[0], headers=admin)).status_code == 404
        assert (await client.post("/a2a", headers={
            "Authorization": "Bearer " + first["token"],
        }, json={})).status_code == 401
        for path in paths[1:]:
            assert (await client.delete(path, headers=admin)).json()["deleted_count"] == 1
        assert engine.store.stats()["total_calls"] == 2
        assert engine.store.agents()[0]["call_count"] == 1


@pytest.mark.asyncio
async def test_cleanup_cookie_authorization_requires_csrf_and_same_origin(
    settings_factory, tmp_path
):
    password = "cleanup-admin-password"
    app, engine = app_for(
        settings_factory, access_enabled=True, access_db_path=tmp_path / "access.sqlite3",
        access_bootstrap_admin_password=password,
    )
    agent = engine.store.issue("First", ["default"])["agent"]
    engine.store.record(agent, "SendMessage", "success", 1, {})
    engine.store.record_event(agent, "task", "searching", "Read")
    paths = ("/admin/api/a2a/calls", "/admin/api/a2a/events",
             f"/admin/api/a2a/agents/{agent['id']}")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://testserver"
        ) as client,
    ):
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": password}
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        for path in paths:
            for headers in ({}, {"x-csrf-token": csrf, "origin": "https://other.example"}):
                assert (await client.delete(path, headers=headers)).status_code == 401
        assert len(engine.store.calls()["calls"]) == len(engine.store.events()["events"]) == 1
        assert len(engine.store.agents()) == 1
        for path in paths:
            assert (await client.delete(path, headers={
                "x-csrf-token": csrf, "origin": "https://testserver",
            })).status_code == 200
        assert engine.store.agents() == []
        assert engine.store.stats()["total_calls"] == 1


@pytest.mark.asyncio
async def test_delete_active_agent_signals_worker_and_prevents_resurrection(settings_factory):
    app, engine = app_for(settings_factory)
    issued = engine.store.issue("Running", ["default"])
    agent = issued["agent"]
    started, release = Event(), Event()

    def slow_retrieve(_agent, _command):
        started.set()
        assert release.wait(5)
        return {"answer": "late result"}

    engine.retrieve = slow_retrieve
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        try:
            sent = await client.post("/a2a", headers={
                "Authorization": "Bearer " + issued["token"], "A2A-Version": "1.0",
            }, json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
                "message": {"messageId": str(uuid4()), "role": "ROLE_USER",
                            "parts": [{"data": {"action": "search", "query": "limits"}}]},
                "configuration": {"returnImmediately": True},
            }})
            task_id = sent.json()["result"]["task"]["id"]
            assert await asyncio.to_thread(started.wait, 2)
            engine.store.record_event(agent, task_id, "searching", "Working")
            deleted = await client.delete(f"/admin/api/a2a/agents/{agent['id']}")
            assert deleted.status_code == 200
            assert deleted.json()["deleted_tasks"] == deleted.json()["deleted_events"] == 1
            assert engine._signals[task_id].is_set()
            release.set()
            for _ in range(100):
                if task_id not in engine._signals:
                    break
                await asyncio.sleep(0.01)
            assert task_id not in engine._signals
            assert engine.store.list_tasks(agent["id"], {})["tasks"] == []
            assert engine.store.events()["events"] == []
            assert engine.store.calls()["calls"][0]["agent_id"] == agent["id"]
            assert engine.store.stats()["total_calls"] == 1
        finally:
            release.set()
