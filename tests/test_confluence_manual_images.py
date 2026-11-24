"""Manual image jobs reuse the durable queue without changing automatic sync policy."""

from __future__ import annotations

import json
import logging
from collections import Counter
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from test_confluence import FakeClient, config, registry

from corporate_kb.access.http import AccessControl
from corporate_kb.confluence.http import register_confluence_routes
from corporate_kb.confluence.images import ImageExtraction
from corporate_kb.confluence.models import SyncCancelled
from corporate_kb.confluence.registry import ConfluenceRegistry

AUTH_URL = "https://auth.example.test/login?state=private-image-login-state"
TOKEN = "manual-images-private-confluence-token"
EVIDENCE = "Manual diagram evidence: service A calls service B"


class RecordingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.downloads = []
        self.discovery_calls = 0

    def root_id(self):
        self.discovery_calls += 1
        return super().root_id()

    def download(self, attachment):
        self.downloads.append((attachment.id, attachment.version))
        return super().download(attachment)


class Extractor:
    identity = "manual-image-test"

    def __init__(self):
        self.calls = []
        self.failures = Counter()
        self.on_extract = None

    def extract(self, path, **kwargs):
        self.calls.append(path.name)
        if self.on_extract:
            self.on_extract(path, kwargs)
        if self.failures[path.name]:
            self.failures[path.name] -= 1
            raise RuntimeError("private extractor diagnostics")
        return ImageExtraction(status="ok", analyzed_files=[path.name], visible_text=EVIDENCE)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(ConfluenceRegistry, "RETRY_DELAY_SECONDS", 0)


def imported_source(tmp_path, *, extractor=None, client=None):
    extractor = extractor or Extractor()
    client = client or RecordingClient()
    instance, _, published, _, visibility = registry(
        tmp_path,
        client=client,
        image_extractor=extractor,
    )
    source = instance.upsert_source(
        config(
            analyze_images=False,
            schedule={"kind": "interval", "interval_minutes": 120},
        ).model_dump(),
        TOKEN,
    )
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert first["status"] == "completed"
    assert extractor.calls == []
    return instance, client, extractor, source, published, visibility


def current_text(instance, source_id, page_id="1"):
    return instance._current_path("target", source_id, page_id).read_text()


def test_manual_images_publish_to_bound_index_without_changing_source_or_discovering_tree(tmp_path):
    instance, client, extractor, source, published, _ = imported_source(tmp_path)
    before = instance.get_source(source["id"])
    downloaded = list(client.downloads)
    discovery_calls = client.discovery_calls
    assert EVIDENCE not in current_text(instance, source["id"])

    queued = instance.queue_image_analysis(source["id"])
    assert queued["type"] == "confluence"
    assert queued["operation"] == queued["progress"]["operation"] == "analyze_images"
    job = instance.run_job(queued["id"])

    assert job["status"] == "completed", job
    assert job["operation"] == "analyze_images"
    assert len(extractor.calls) == 2
    assert client.discovery_calls == discovery_calls
    assert client.downloads == downloaded
    assert published == ["target", "target"]
    assert all(EVIDENCE in current_text(instance, source["id"], page) for page in ("1", "2"))
    after = instance.get_source(source["id"])
    for key in (
        "analyze_images",
        "schedule",
        "next_check_at",
        "last_checked_at",
        "last_success_at",
    ):
        assert after[key] == before[key]
    assert after["analyze_images"] is False
    events = instance.job_log(job["id"], limit=1000)["items"]
    assert any(event["stage"] == "images" for event in events)
    assert any(event["stage"] == "publication" for event in events)
    assert TOKEN not in json.dumps([job, events])


def test_manual_results_survive_automatic_disabled_sync_and_updated_page(tmp_path):
    instance, client, extractor, source, _, _ = imported_source(tmp_path)
    assert (
        instance.run_job(instance.queue_image_analysis(source["id"])["id"])["status"] == "completed"
    )
    calls = list(extractor.calls)
    client.page_calls.clear()

    unchanged = instance.run_job(instance.queue_sync(source["id"])["id"])

    assert unchanged["status"] == "completed"
    assert unchanged["result"]["changed"] == 0
    assert all(not body for _, body, _ in client.page_calls)
    assert EVIDENCE in current_text(instance, source["id"])
    client.version = 2
    updated = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert updated["status"] == "completed"
    assert updated["result"]["changed"] == 2
    assert "revision 2" in current_text(instance, source["id"])
    assert EVIDENCE in current_text(instance, source["id"])
    assert extractor.calls == calls
    assert instance.get_source(source["id"])["analyze_images"] is False


def test_manual_svg_evidence_survives_page_version_change_without_reanalysis(tmp_path):
    class InlineSvgClient(RecordingClient):
        def page_ids(self, root):
            yield root

        def attachments(self, page_id):
            return []

        def page(self, page_id, *, body=True, version=None):
            page = super().page(page_id, body=body, version=version)
            if body:
                page.body += '<svg xmlns="http://www.w3.org/2000/svg"><text>A → B</text></svg>'
            return page

    instance, client, extractor, source, _, _ = imported_source(
        tmp_path,
        client=InlineSvgClient(),
    )
    manual = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert manual["status"] == "completed"
    assert len(extractor.calls) == 1
    assert EVIDENCE in current_text(instance, source["id"])
    client.version = 2

    updated = instance.run_job(instance.queue_sync(source["id"])["id"])

    assert updated["status"] == "completed"
    assert updated["result"]["changed"] == 1
    assert "revision 2" in current_text(instance, source["id"])
    assert EVIDENCE in current_text(instance, source["id"])
    assert len(extractor.calls) == 1
    assert client.downloads == []
    with instance._db() as db:
        assets = db.execute(
            "SELECT version,sha256,extraction_json FROM assets ORDER BY version"
        ).fetchall()
    assert [row["version"] for row in assets] == [1, 2]
    assert assets[0]["sha256"] == assets[1]["sha256"]
    assert json.loads(assets[1]["extraction_json"])["result"]["status"] == "ok"


@pytest.mark.parametrize("same_bytes", [True, False])
def test_new_attachment_version_reuses_manual_evidence_only_for_identical_bytes(
    tmp_path, same_bytes
):
    class VersionedImageClient(RecordingClient):
        def page_ids(self, root):
            yield root

        def download(self, attachment):
            self.downloads.append((attachment.id, attachment.version))
            version = 1 if same_bytes else attachment.version
            return f"diagram-binary-version-{version}".encode()

    instance, client, extractor, source, _, _ = imported_source(
        tmp_path,
        client=VersionedImageClient(),
    )
    manual = instance.run_job(instance.queue_image_analysis(source["id"])["id"])
    assert manual["status"] == "completed"
    assert len(extractor.calls) == 1
    client.asset_version = 2

    updated = instance.run_job(instance.queue_sync(source["id"])["id"])

    assert updated["status"] == "completed"
    assert updated["result"]["changed"] == 1
    assert (EVIDENCE in current_text(instance, source["id"])) is same_bytes
    assert len(extractor.calls) == 1  # Automatic recognition remains disabled.
    assert client.downloads == [("asset-1", 1), ("asset-1", 2)]


def test_successful_results_are_reused_on_repeated_manual_job(tmp_path):
    instance, client, extractor, source, published, _ = imported_source(tmp_path)
    assert (
        instance.run_job(instance.queue_image_analysis(source["id"])["id"])["status"] == "completed"
    )
    calls, downloads = list(extractor.calls), list(client.downloads)

    repeated = instance.run_job(instance.queue_image_analysis(source["id"])["id"])

    assert repeated["status"] == "completed"
    assert repeated["result"]["changed"] == 0
    assert extractor.calls == calls
    assert client.downloads == downloads
    assert published == ["target", "target"]
    assert EVIDENCE in current_text(instance, source["id"])


def test_manual_images_require_imported_current_pages_and_configured_extractor(tmp_path):
    instance, _, _, _, _ = registry(tmp_path, image_extractor=Extractor())
    source = instance.upsert_source(config().model_dump(), TOKEN)
    with pytest.raises(ValueError):
        instance.queue_image_analysis(source["id"])
    assert instance.jobs() == []
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    before = instance.jobs()
    instance.image_extractor = None
    with pytest.raises(RuntimeError):
        instance.queue_image_analysis(source["id"])
    assert instance.jobs() == before


def test_manual_jobs_are_durable_and_do_not_reclassify_legacy_sync_jobs(tmp_path):
    instance, client, extractor, source, _, _ = imported_source(tmp_path)
    legacy = instance.queue_sync(source["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET progress_json='{}' WHERE id=?", (legacy["id"],))
    assert instance.job_status(legacy["id"])["operation"] == "sync"
    assert instance.run_job(legacy["id"])["status"] == "completed"
    queued = instance.queue_image_analysis(source["id"])

    reopened, _, published, _, _ = registry(tmp_path, client=client, image_extractor=extractor)
    assert reopened.job_status(queued["id"])["operation"] == "analyze_images"
    job = reopened.run_job(queued["id"])

    assert job["status"] == "completed"
    assert job["operation"] == "analyze_images"
    assert len(extractor.calls) == 2
    assert published == ["target"]


def test_same_active_operation_is_idempotent_but_other_operation_conflicts(tmp_path):
    instance, _, _, source, _, _ = imported_source(tmp_path)
    image_job = instance.queue_image_analysis(source["id"])
    assert instance.queue_image_analysis(source["id"])["id"] == image_job["id"]
    with pytest.raises(RuntimeError):
        instance.queue_sync(source["id"])
    instance.cancel_job(image_job["id"])
    sync_job = instance.queue_sync(source["id"])
    assert instance.queue_sync(source["id"])["id"] == sync_job["id"]
    with pytest.raises(RuntimeError):
        instance.queue_image_analysis(source["id"])
    assert len([job for job in instance.jobs() if job["status"] == "queued"]) == 1


def test_due_schedule_does_not_starve_manual_images_and_runs_afterwards(tmp_path, monkeypatch):
    instance, _, _, source, _, _ = imported_source(tmp_path)
    queued = instance.queue_image_analysis(source["id"])
    with instance._db() as db:
        db.execute(
            "UPDATE sources SET next_check_at='2000-01-01T00:00:00Z' WHERE id=?", (source["id"],)
        )
    completed = []
    run_job = instance.run_job

    def one_iteration(job_id):
        result = run_job(job_id)
        completed.append(result)
        instance._stop.set()
        return result

    monkeypatch.setattr(instance, "run_job", one_iteration)
    monkeypatch.setattr(
        instance._wake,
        "wait",
        lambda **kwargs: pytest.fail("A queued operation was not dispatched"),
    )
    instance._loop()
    assert completed[0]["id"] == queued["id"]
    assert completed[0]["operation"] == "analyze_images"
    assert instance.get_source(source["id"])["next_check_at"] == "2000-01-01T00:00:00Z"
    instance._stop.clear()
    instance._loop()
    assert len(completed) == 2
    assert completed[1]["operation"] == "sync"
    assert all(job["status"] == "completed" for job in completed)
    assert instance.get_source(source["id"])["next_check_at"] != "2000-01-01T00:00:00Z"


def test_manual_images_do_not_resurrect_noncurrent_pages_or_remove_unvisited_pages(tmp_path):
    instance, client, extractor, source, _, visibility = imported_source(tmp_path)
    with instance._db() as db:
        db.execute(
            "UPDATE pages SET state='access_lost' WHERE source_id=? AND page_id='2'",
            (source["id"],),
        )
    client.page_calls.clear()
    visibility.clear()

    job = instance.run_job(instance.queue_image_analysis(source["id"])["id"])

    assert job["status"] == "completed"
    assert len(extractor.calls) == 1
    assert {page for page, _, _ in client.page_calls} == {"1"}
    pages = {page["page_id"]: page for page in instance.source_pages(source["id"])}
    assert pages["1"]["state"] == "current"
    assert pages["2"]["state"] == "access_lost"
    assert all(page_id != f"confluence:{source['id']}:2" for _, page_id, _ in visibility)


@pytest.mark.parametrize("failures,expected", [(1, "completed"), (20, "completed_with_warnings")])
def test_manual_image_failures_retry_other_pages_and_report_unresolved(
    tmp_path, failures, expected
):
    instance, _, extractor, source, published, _ = imported_source(tmp_path)
    with instance._db() as db:
        asset = db.execute("SELECT path FROM assets WHERE page_id='1'").fetchone()
    bad_path = (instance.root / asset["path"]).name
    extractor.failures[bad_path] = failures

    job = instance.run_job(instance.queue_image_analysis(source["id"])["id"])

    assert job["status"] == expected
    assert EVIDENCE in current_text(instance, source["id"], "2")
    assert extractor.calls.count(bad_path) == min(failures + 1, 3)
    assert len(extractor.calls) == min(failures + 1, 3) + 1
    assert published == ["target", "target"]
    unresolved = instance.job_failures(job["id"])["items"]
    assert len(unresolved) == int(failures > 2)
    if unresolved:
        assert unresolved[0]["page_id"] == "1"
        assert unresolved[0]["attempts"] == 3


@pytest.mark.parametrize("cancelled", [False, True])
def test_manual_images_expose_browser_login_and_clear_it_on_finish(tmp_path, caplog, cancelled):
    instance, _, extractor, source, _, _ = imported_source(tmp_path)
    job = instance.queue_image_analysis(source["id"])
    observed = []

    def on_extract(path, callbacks):
        callbacks["authentication_url"](AUTH_URL)
        observed.append(instance.job_status(job["id"]))
        if cancelled:
            instance.cancel_job(job["id"])
            raise SyncCancelled("Manual image analysis cancelled")
        callbacks["authentication_complete"]()

    extractor.on_extract = on_extract
    with caplog.at_level(logging.INFO):
        finished = instance.run_job(job["id"])
    assert observed
    assert all(item["operation"] == "analyze_images" for item in observed)
    assert all(item["progress"]["authentication_url"] == AUTH_URL for item in observed)
    assert finished["status"] == ("cancelled" if cancelled else "completed")
    assert finished["progress"]["authentication_required"] is False
    assert finished["progress"]["authentication_url"] is None
    assert AUTH_URL not in caplog.text and TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_manual_images_http_requires_admin_csrf_and_handles_duplicate_and_conflict(
    tmp_path,
    settings_factory,
):
    instance, _, _, source, _, _ = imported_source(tmp_path)
    password = "confluence-manual-images-admin-test-password"
    access = AccessControl(
        settings_factory(
            access_db_path=tmp_path / "access.sqlite3",
            access_bootstrap_admin_password=password,
        )
    )
    server = FastMCP("confluence-manual-images-test")
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
        gigacode=SimpleNamespace(status=lambda: {"available": True}),
    )
    app = server.http_app(path="/mcp", host_origin_protection=False)
    prefix = "/admin/api/confluence"
    path = f"{prefix}/sources/{source['id']}/images/analyze"
    original_jobs = instance.jobs()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    ) as client:
        assert (await client.post(path)).status_code == 401
        assert (await client.post(path, headers={"x-test-reader": "yes"})).status_code == 401
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": password}
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        for headers in (
            {},
            {"x-csrf-token": "wrong"},
            {"x-csrf-token": csrf, "origin": "https://other.example"},
        ):
            assert (await client.post(path, headers=headers)).status_code == 401
        assert instance.jobs() == original_jobs
        headers = {"x-csrf-token": csrf, "origin": "https://testserver"}
        response = await client.post(path, headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        queued = response.json()["job"]
        assert queued["operation"] == "analyze_images"
        assert TOKEN not in response.text
        assert (await client.post(path, headers=headers)).json()["job"]["id"] == queued["id"]
        assert (
            await client.post(f"{prefix}/sources/{source['id']}/sync", headers=headers)
        ).status_code == 409
        assert (
            await client.post(f"{prefix}/sources/missing/images/analyze", headers=headers)
        ).status_code == 404
        instance.cancel_job(queued["id"])
        instance.delete_source(source["id"])
        assert (await client.post(path, headers=headers)).status_code == 404
