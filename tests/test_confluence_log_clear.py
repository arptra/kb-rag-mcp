from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from test_confluence import config, registry

from corporate_kb.access.http import AccessControl
from corporate_kb.confluence.http import register_confluence_routes


def operational_state(instance):
    with instance._db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("sources", "jobs", "pages", "revisions", "assets", "sync_items")
        }


def test_clear_source_logs_preserves_all_operational_data_and_other_sources(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    other = instance.upsert_source(config(name="Other source").model_dump(), "other-secret")
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    second = instance.run_job(instance.queue_sync(source["id"])["id"])
    unrelated = instance.run_job(instance.queue_sync(other["id"])["id"])
    expected_count = sum(len(instance.job_log(job["id"])["items"]) for job in (first, second))
    assert expected_count > 0
    unrelated_log = instance.job_log(unrelated["id"])
    before = operational_state(instance)
    files_before = {
        path: path.read_bytes()
        for directory in (tmp_path / "knowledge", instance.root / "assets")
        for path in directory.rglob("*")
        if path.is_file()
    }
    assert files_before

    cleared = instance.clear_source_logs(source["id"])

    assert cleared == {
        "status": "cleared", "source_id": source["id"], "deleted_events": expected_count,
    }
    assert instance.job_log(first["id"])["items"] == []
    assert instance.job_log(second["id"])["items"] == []
    assert instance.job_log(unrelated["id"]) == unrelated_log
    assert operational_state(instance) == before
    assert {path: path.read_bytes() for path in files_before} == files_before
    assert instance.clear_source_logs(source["id"])["deleted_events"] == 0


def test_clear_logs_during_sync_keeps_running_job_and_new_events_visible(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    publishing, resume = threading.Event(), threading.Event()

    def publish(index_id, **kwargs):
        publishing.set()
        assert resume.wait(10)
        return {"status": "completed"}

    instance.publish = publish
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation = executor.submit(instance.run_job, job["id"])
        try:
            assert publishing.wait(5)
            running = instance.job_status(job["id"])
            assert running["status"] == "running"
            old_cursor = instance.job_log(job["id"])["next_cursor"]
            assert old_cursor > 0
            before = operational_state(instance)

            assert instance.clear_source_logs(source["id"])["deleted_events"] > 0

            assert operational_state(instance) == before
            assert instance.job_status(job["id"]) == running
            assert not instance._cancel[job["id"]].is_set()
            assert instance.job_log(job["id"], cursor=old_cursor) == {
                "items": [], "next_cursor": old_cursor, "has_more": False,
            }
        finally:
            resume.set()
        completed = operation.result(timeout=5)

    assert completed["status"] == "completed"
    new_events = instance.job_log(job["id"], cursor=old_cursor)
    assert new_events["items"]
    assert all(item["cursor"] > old_cursor for item in new_events["items"])
    assert any(item["stage"] == "complete" for item in new_events["items"])
    reopened, _, _, _, _ = registry(tmp_path)
    assert reopened.job_log(job["id"])["items"] == new_events["items"]


def test_clear_unknown_source_is_rejected_without_deleting_events(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    instance._log(job["id"], "fetch", "Existing event")
    before = instance.job_log(job["id"])
    with pytest.raises(KeyError, match="Unknown Confluence source"):
        instance.clear_source_logs("missing")
    assert instance.job_log(job["id"]) == before


@pytest.mark.asyncio
async def test_clear_logs_requires_administrator_and_existing_csrf_policy(
    tmp_path, settings_factory,
):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    instance._log(job["id"], "fetch", "Existing event")
    before = instance.job_log(job["id"])
    password = "confluence-clear-test-admin-password"
    access = AccessControl(settings_factory(
        access_db_path=tmp_path / "access.sqlite3",
        access_bootstrap_admin_password=password,
    ))
    server = FastMCP("confluence-log-clear-test")
    access.register(server)

    async def manager(request):
        return access.administrator(request) is not None

    async def reader(request):
        return request.headers.get("x-test-reader") == "yes"

    register_confluence_routes(
        server,
        instance,
        reader_authorized=reader,
        manager_authorized=manager,
        indexes=lambda: [{"id": "target"}],
        gigacode=SimpleNamespace(status=lambda: {"available": False}),
    )
    app = server.http_app(path="/mcp", host_origin_protection=False)
    path = f"/admin/api/confluence/sources/{source['id']}/logs/clear"
    log_path = f"/admin/api/confluence/jobs/{job['id']}/log"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        assert (await client.post(path)).status_code == 401
        reader_headers = {"x-test-reader": "yes"}
        assert (await client.get(log_path, headers=reader_headers)).status_code == 200
        assert (await client.post(path, headers=reader_headers)).status_code == 401
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": password}
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert (await client.post(path)).status_code == 401
        assert (await client.post(path, headers={"x-csrf-token": "wrong"})).status_code == 401
        assert (await client.post(path, headers={
            "x-csrf-token": csrf, "origin": "https://other.example",
        })).status_code == 401
        assert (await client.get(path)).status_code == 405
        assert instance.job_log(job["id"]) == before

        headers = {"x-csrf-token": csrf, "origin": "https://testserver"}
        cleared = await client.post(path, headers=headers)
        assert cleared.status_code == 200
        assert cleared.headers["Cache-Control"] == "no-store"
        assert cleared.json() == {
            "status": "cleared", "source_id": source["id"], "deleted_events": 1,
        }
        assert (await client.get(log_path)).json()["items"] == []
        assert (await client.post(path, headers=headers)).json()["deleted_events"] == 0
        assert (await client.post(
            "/admin/api/confluence/sources/missing/logs/clear", headers=headers,
        )).status_code == 404
        instance._log(job["id"], "fetch", "New event")
        fresh = (await client.get(log_path, params={"cursor": before["next_cursor"]})).json()
        assert [item["message"] for item in fresh["items"]] == ["New event"]
        assert fresh["next_cursor"] > before["next_cursor"]
