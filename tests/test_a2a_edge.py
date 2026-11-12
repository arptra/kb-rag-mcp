from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from corporate_kb.a2a.service import A2AService, RpcError
from corporate_kb.a2a.store import A2AStore


def request(data=None, **configuration):
    return {
        "message": {
            "messageId": str(uuid4()),
            "role": "ROLE_USER",
            "parts": [{"data": data} if data is not None else {"text": "limits"}],
        },
        "configuration": configuration,
    }


@pytest.fixture
def setup_agent(tmp_path):
    store = A2AStore(tmp_path)
    issued = store.issue("Scoped", ["allowed"])
    agent = store.authenticate(issued["token"])
    service = A2AService(store, SimpleNamespace(has_index=lambda value: value == "allowed"))
    service.retrieve = lambda agent, command: {"answer": "private indexed content"}
    yield store, service, agent
    service.stop()


def assert_capacity_available(service):
    acquired = []
    try:
        for _ in range(32):
            assert service._capacity.acquire(blocking=False)
            acquired.append(True)
        assert not service._capacity.acquire(blocking=False)
    finally:
        for _ in acquired:
            service._capacity.release()


@pytest.mark.parametrize("change", ["rotate", "expire", "revoke", "scope"])
def test_credentials_changed_during_blocking_read_never_return_artifacts(setup_agent, change):
    store, service, agent = setup_agent
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"answer": "private indexed content"}

    service.retrieve = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(service.dispatch, agent, "SendMessage", request())
        try:
            assert entered.wait(2)
            if change == "rotate":
                fresh = store.rotate(agent["id"])
                assert fresh["agent"]["credential_version"] == agent["credential_version"] + 1
            elif change == "revoke":
                store.revoke(agent["id"])
            else:
                with store.db() as db:
                    if change == "expire":
                        db.execute("UPDATE agents SET expires_at=?", (time.time() - 1,))
                    else:
                        db.execute("UPDATE agents SET index_ids='[\"different\"]'")
        finally:
            release.set()
        with pytest.raises(PermissionError):
            response.result(3)
    task = store.list_tasks(agent["id"], {})["tasks"][0]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "artifacts" not in task
    assert_capacity_available(service)


def test_async_result_is_a_snapshot_and_rotation_stops_publication(setup_agent):
    store, service, agent = setup_agent
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"answer": "private indexed content"}

    service.retrieve = slow
    answer = service.dispatch(agent, "SendMessage", request(returnImmediately=True))
    try:
        assert entered.wait(2)
        assert answer["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
        store.rotate(agent["id"])
    finally:
        release.set()
    service.stop()
    assert answer["task"]["status"]["state"] == "TASK_STATE_SUBMITTED"
    task = store.task(agent["id"], answer["task"]["id"])
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "artifacts" not in task


def test_rotation_between_access_check_and_database_commit_is_atomic(setup_agent, monkeypatch):
    store, service, agent = setup_agent
    original = store.save_task

    def rotate_at_publication(agent_id, task, **kwargs):
        if kwargs.get("credential_version") is not None:
            store.rotate(agent_id)
        return original(agent_id, task, **kwargs)

    monkeypatch.setattr(store, "save_task", rotate_at_publication)
    with pytest.raises(PermissionError):
        service.dispatch(agent, "SendMessage", request())
    task = store.list_tasks(agent["id"], {})["tasks"][0]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "artifacts" not in task


def test_cancel_remains_terminal_when_worker_finishes(setup_agent):
    store, service, agent = setup_agent
    entered, release = Event(), Event()

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return {"answer": "private indexed content"}

    service.retrieve = slow
    task = service.dispatch(agent, "SendMessage", request(returnImmediately=True))["task"]
    try:
        assert entered.wait(2)
        canceled = service.dispatch(agent, "CancelTask", {"id": task["id"]})
        assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    finally:
        release.set()
    service.stop()
    saved = store.task(agent["id"], task["id"])
    assert saved["status"]["state"] == "TASK_STATE_CANCELED"
    assert "artifacts" not in saved
    assert_capacity_available(service)


def test_idempotent_retry_works_even_when_all_worker_slots_are_used(setup_agent):
    _, service, agent = setup_agent
    params = request()
    task = service.dispatch(agent, "SendMessage", params)["task"]
    for _ in range(32):
        assert service._capacity.acquire(blocking=False)
    try:
        repeated = service.dispatch(agent, "SendMessage", params)["task"]
        assert repeated["id"] == task["id"]
        with pytest.raises(RpcError, match="busy"):
            service.dispatch(agent, "SendMessage", request())
        params["message"]["parts"] = [{"text": "different"}]
        with pytest.raises(ValueError, match="already used"):
            service.dispatch(agent, "SendMessage", params)
    finally:
        for _ in range(32):
            service._capacity.release()


def test_executor_failure_finishes_task_and_releases_capacity(setup_agent):
    store, service, agent = setup_agent
    service.stop()
    with pytest.raises(RpcError, match="unavailable"):
        service.dispatch(agent, "SendMessage", request(returnImmediately=True))
    task = store.list_tasks(agent["id"], {})["tasks"][0]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert_capacity_available(service)


def test_database_failure_still_releases_capacity(setup_agent, monkeypatch):
    _, service, agent = setup_agent

    def fail(*args, **kwargs):
        raise OSError("database unavailable")

    monkeypatch.setattr(service.store, "save_task", fail)
    with pytest.raises(OSError):
        service.dispatch(agent, "SendMessage", request())
    assert_capacity_available(service)


@pytest.mark.parametrize(
    "command",
    [
        {"action": []},
        {"action": "search", "query": "x", "version_scope": []},
        {"action": "search", "query": "x", "page_id": {"nested": "x"}},
        {"action": "search", "query": "x", "version_scope": "as_of", "as_of": "2025-01-01"},
        {"action": "search", "query": "x", "updated_from": "invalid"},
        {
            "action": "search",
            "query": "x",
            "updated_from": "2026-01-01T00:00:00Z",
            "updated_to": "2025-01-01T00:00:00Z",
        },
        {"action": "document", "page_id": "p", "source_version": 1},
        {"action": "document", "page_id": "p", "max_tokens": True},
        {"action": "document", "page_id": "p", "max_tokens": -1},
        {"action": "document", "page_id": "p", "document_id": "d"},
        {
            "action": "document",
            "page_id": "p",
            "source_version": "1",
            "as_of": "2025-01-01T00:00:00Z",
        },
    ],
)
def test_malformed_commands_are_rejected_before_creating_task(setup_agent, command):
    store, service, agent = setup_agent
    with pytest.raises(ValueError):
        service.dispatch(agent, "SendMessage", request(command))
    assert store.list_tasks(agent["id"], {})["totalSize"] == 0


def test_second_empty_data_part_is_rejected(setup_agent):
    _, service, agent = setup_agent
    params = request()
    params["message"]["parts"] = [{"data": {}}, {"data": {}}, {"text": "query"}]
    with pytest.raises(ValueError, match="one object"):
        service.dispatch(agent, "SendMessage", params)


@pytest.mark.parametrize(
    "params",
    [
        {"includeArtifacts": "false"},
        {"pageSize": True},
        {"pageToken": False},
        {"contextId": []},
        {"status": False},
        {"status": "invented"},
        {"statusTimestampAfter": "2025-01-01"},
    ],
)
def test_list_tasks_rejects_ambiguous_values(setup_agent, params):
    _, service, agent = setup_agent
    with pytest.raises(ValueError):
        service.dispatch(agent, "ListTasks", params)


def test_versions_exclude_large_asset_metadata(setup_agent):
    _, service, agent = setup_agent

    class Version:
        def model_dump(self, *, mode, exclude):
            return {
                key: value
                for key, value in {
                    "page_id": "page",
                    "source_version": "7",
                    "content": "body",
                    "metadata": {"assets": [{"extracted_text": "very large image text"}]},
                }.items()
                if key not in exclude
            }

    service.catalog.service_for = lambda index: SimpleNamespace(
        history=SimpleNamespace(list_versions=lambda *args, **kwargs: [Version()])
    )
    result = A2AService.retrieve(service, agent, {"action": "versions", "page_id": "page"})
    assert result["indexes"][0]["versions"] == [{"page_id": "page", "source_version": "7"}]


def test_agent_index_scope_requires_an_array(tmp_path):
    store = A2AStore(tmp_path)
    with pytest.raises(ValueError):
        store.issue("Bad", "allowed")


def test_rejected_tasks_are_immutable(setup_agent):
    store, service, agent = setup_agent
    task = service.dispatch(agent, "SendMessage", request())["task"]
    with store.db() as db:
        db.execute("UPDATE tasks SET state='TASK_STATE_REJECTED' WHERE id=?", (task["id"],))
    assert not store.save_task(agent["id"], task)
