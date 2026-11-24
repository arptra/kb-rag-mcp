from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from test_confluence import config, registry

from corporate_kb.confluence.http import register_confluence_routes
from corporate_kb.confluence.images import GigaCodeImageExtractor, ImageExtraction
from corporate_kb.confluence.models import SyncCancelled

AUTH_URL = "https://auth.example.test/authorize?state=private-state&code=private-code"


def test_image_extractor_forwards_browser_auth_callbacks_but_cache_does_not_prompt(tmp_path):
    path = tmp_path / "source.svg"
    path.write_text("<svg/>")
    events = []
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        kwargs["authentication_url"](AUTH_URL)
        kwargs["authentication_complete"]()
        return SimpleNamespace(payload={"status": "ok", "analyzed_files": ["attachment.svg"]})

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    callbacks = {
        "authentication_url": lambda url: events.append(("required", url)),
        "authentication_complete": lambda: events.append(("complete", None)),
    }
    first = extractor.extract(path, **callbacks)
    assert events == [("required", AUTH_URL), ("complete", None)]
    assert extractor.extract(path, **callbacks) == first
    assert len(calls) == 1
    assert events == [("required", AUTH_URL), ("complete", None)]


@pytest.mark.parametrize(
    ("outcome", "expected_status"),
    [
        ("complete", "completed"),
        ("no_completion_callback", "completed"),
        ("error", "completed_with_warnings"),
        ("cancel", "cancelled"),
        ("cancel_error", "cancelled"),
    ],
)
def test_manual_images_expose_live_browser_auth_and_clear_it_on_every_exit(
    tmp_path, monkeypatch, caplog, outcome, expected_status
):
    observed = []

    class Extractor:
        identity = "auth-test"

        def extract(self, path, **kwargs):
            kwargs["progress"](f"GigaCode waits for browser authentication: {AUTH_URL}")
            kwargs["authentication_url"](AUTH_URL)
            kwargs["progress"](f"GigaCode stderr | Open {AUTH_URL}")
            waiting = instance.job_status(job["id"])
            observed.append(waiting)
            if outcome == "complete":
                kwargs["authentication_complete"]()
                resumed = instance.job_status(job["id"])
                assert resumed["status"] == "running"
                assert resumed["phase"] == "images"
                assert resumed["progress"]["authentication_required"] is False
                assert resumed["progress"]["authentication_url"] is None
            elif outcome == "error":
                raise RuntimeError("extraction failed after authentication request")
            elif outcome in {"cancel", "cancel_error"}:
                instance.cancel_job(job["id"])
                if outcome == "cancel_error":
                    raise RuntimeError("process stopped while awaiting authentication")
                raise SyncCancelled("Image extraction cancelled")
            return ImageExtraction(status="ok", analyzed_files=[path.name])

    instance, client, _, _, _ = registry(tmp_path, image_extractor=Extractor())
    client.page_ids = lambda root: iter([root])
    monkeypatch.setattr(instance, "RETRY_ROUNDS", 0)
    monkeypatch.setattr(instance, "RETRY_DELAY_SECONDS", 0)
    source = instance.upsert_source(config().model_dump(), "secret-pat")
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    job = instance.queue_image_analysis(source["id"])
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(job["id"])
    assert len(observed) == 1, finished
    waiting = observed[0]
    assert waiting["status"] == "running"
    assert waiting["phase"] == "authentication"
    assert waiting["progress"]["authentication_required"] is True
    assert waiting["progress"]["authentication_url"] == AUTH_URL
    assert datetime.fromisoformat(waiting["progress"]["authentication_requested_at"])
    assert finished["status"] == expected_status, finished
    for snapshot in (finished, instance.job_status(job["id"])):
        assert snapshot["progress"]["authentication_required"] is False
        assert snapshot["progress"]["authentication_url"] is None
        assert snapshot["progress"]["authentication_requested_at"] is None
    journal = instance.job_log(job["id"])
    assert AUTH_URL in json.dumps(journal)
    assert "authentication" in caplog.text.lower()
    assert "auth.example.test" not in caplog.text
    assert "private-state" not in caplog.text
    assert "private-code" not in caplog.text
    assert "secret-pat" not in caplog.text


def test_scheduler_restart_discards_stale_browser_authorization_link(tmp_path, monkeypatch):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    instance._authentication_required(job["id"], AUTH_URL, page_id="1", attachment_id="asset-1")
    restarted, _, _, _, _ = registry(tmp_path)
    # Observe crash recovery before the worker can start another extraction.
    monkeypatch.setattr(restarted, "_loop", lambda: None)
    restarted.start()
    try:
        resumed = restarted.job_status(job["id"])
        assert resumed["status"] == "queued"
        assert resumed["progress"]["authentication_required"] is False
        assert resumed["progress"]["authentication_url"] is None
        assert resumed["progress"]["authentication_requested_at"] is None
    finally:
        restarted.stop()


def test_waiting_job_stays_visible_ahead_of_newer_history(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        db.execute(
            "INSERT INTO jobs(id,source_id,index_id,status,message,created_at) "
            "VALUES('newer',?,'target','completed','Done','2099-01-01T00:00:00Z')",
            (source["id"],),
        )
    instance._authentication_required(job["id"], AUTH_URL, page_id="1", attachment_id="asset-1")
    assert instance.jobs(limit=1)[0]["id"] == job["id"]
    assert instance.jobs(source_id=source["id"], limit=1)[0]["id"] == job["id"]


@pytest.mark.asyncio
async def test_only_manager_sees_browser_auth_link_in_live_jobs_and_operation_log(tmp_path, caplog):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    with caplog.at_level(logging.INFO):
        instance._authentication_required(job["id"], AUTH_URL, page_id="1", attachment_id="asset-1")
        for message in (
            f"GigaCode waits for browser authentication: {AUTH_URL}",
            f"GigaCode stderr | Open {AUTH_URL}",
            f"GigaCode stdout (non-JSON) | Open {AUTH_URL}",
            "GigaCode stderr | " + json.dumps({"authentication_url": AUTH_URL}),
            f"GigaCode result: is_error=True; error=Login failed: {AUTH_URL}",
            f"GigaCode result: is_error=True; error=Login failed: {AUTH_URL.upper()}",
            "GigaCode result: is_error=True; error=Login failed: " + AUTH_URL.replace("/", r"\/"),
        ):
            instance._image_progress(job["id"], message, attachment_id="asset-1")
    assert "auth.example.test" not in caplog.text
    assert "private-state" not in caplog.text
    assert "private-code" not in caplog.text
    for event in instance.job_log(job["id"])["items"]:
        assert "auth.example.test" not in event["message"].lower()
    server = FastMCP("confluence-browser-auth-test")

    async def is_manager(request):
        return request.headers.get("x-test-role") == "manager"

    async def is_reader(request):
        return request.headers.get("x-test-role") == "reader"

    register_confluence_routes(
        server,
        instance,
        reader_authorized=is_reader,
        manager_authorized=is_manager,
        indexes=lambda: [{"id": "target"}],
        gigacode=SimpleNamespace(status=lambda: {"configured": True}),
    )
    paths = ["", "/jobs", f"/jobs/{job['id']}", f"/jobs/{job['id']}/log"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.http_app()), base_url="http://testserver"
    ) as client:
        for suffix in paths:
            url = "/admin/api/confluence" + suffix
            manager = await client.get(url, headers={"x-test-role": "manager"})
            assert manager.status_code == 200, manager.text
            assert manager.headers["cache-control"] == "no-store"
            assert AUTH_URL in manager.text
            reader = await client.get(url, headers={"x-test-role": "reader"})
            assert reader.status_code == 200, reader.text
            assert reader.headers["cache-control"] == "no-store"
            assert "auth.example.test" not in reader.text.lower()
            assert "private-state" not in reader.text
            assert "private-code" not in reader.text
            if not suffix.endswith("/log"):
                payload = reader.json()
                visible_job = payload["jobs"][0] if "jobs" in payload else payload
                assert visible_job["progress"]["authentication_required"] is True
                assert visible_job["progress"].get("authentication_url") is None
            anonymous = await client.get(url)
            assert anonymous.status_code == 401
            assert "auth.example.test" not in anonymous.text
    # Redacting a reader's HTTP response must not erase the administrator's state.
    assert instance.job_status(job["id"])["progress"]["authentication_url"] == AUTH_URL
    assert AUTH_URL in json.dumps(instance.job_log(job["id"]))
