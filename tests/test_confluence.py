from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.images import GigaCodeImageExtractor, ImageExtraction
from corporate_kb.confluence.models import Attachment, ConfluenceError, Page, Schedule, SourceConfig
from corporate_kb.confluence.registry import ConfluenceRegistry
from corporate_kb.loaders.filesystem import FileSystemDocumentLoader


def config(**values):
    return SourceConfig.model_validate(
        {
            "name": "Documentation",
            "index_id": "target",
            "base_url": "https://docs.example.com",
            "root_page_url": "https://docs.example.com/pages/1/Root",
            "analyze_images": False,
            "schedule": {"kind": "manual"},
            **values,
        }
    )


class FakeClient:
    def __init__(self):
        self.cancel = threading.Event()
        self.version = 1
        self.asset_version = 1
        self.page_calls = []
        self.denied = False
        self.discovery_error = False

    def close(self):
        pass

    def root_id(self):
        return "1"

    def page_ids(self, root):
        yield root
        if self.discovery_error:
            raise ConfluenceError("network failed")
        yield "2"

    def page(self, page_id, *, body=True, version=None):
        self.page_calls.append((page_id, body, version))
        if self.denied and page_id == "2":
            raise ConfluenceError("Page access denied", status_code=403)
        v = version or self.version
        return Page(
            id=page_id,
            title=f"Page {page_id}",
            version=v,
            updated_at=f"2026-01-0{v}T00:00:00Z",
            parent_id="1" if page_id == "2" else None,
            url=f"https://docs.example.com/pages/{page_id}",
            body=f"<p>revision {v}</p>" if body else "",
        )

    def attachments(self, page_id):
        return [
            Attachment(
                id=f"asset-{page_id}",
                title="../../unsafe.png",
                version=self.asset_version,
                media_type="image/png",
                download_url="https://docs.example.com/download",
            )
        ]

    def download(self, attachment):
        return f"image-version-{attachment.version}".encode()

    def versions(self, page_id):
        return list(range(self.version, 0, -1))


def registry(tmp_path, client=None, **kwargs):
    published, archived, visibility = [], [], []
    client = client or FakeClient()
    instance = ConfluenceRegistry(
        tmp_path / "state",
        index_exists=lambda index: index == "target",
        knowledge_root=lambda index: tmp_path / "knowledge" / index,
        publish=lambda index, **kwargs: published.append(index) or {"status": "completed"},
        archive_document=lambda index, document: archived.append((index, document)),
        page_visibility=lambda *args: visibility.append(args),
        client_factory=lambda conf, token: client,
        **kwargs,
    )
    return instance, client, published, archived, visibility


def test_registry_binding_secrets_noop_history_and_attachment_updates(tmp_path):
    instance, client, published, archived, _ = registry(tmp_path)
    with pytest.raises(ValueError, match="existing"):
        instance.upsert_source(config(index_id="missing").model_dump(), "top-secret")
    source = instance.upsert_source(config().model_dump(), "top-secret")
    assert "top-secret" not in json.dumps(source)
    assert b"top-secret" not in instance.database_path.read_bytes()
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert first["status"] == "completed"
    assert len(published) == 1 and len(archived) == 2
    assert all(index == "target" for index, _ in archived)
    loaded = FileSystemDocumentLoader().load_directory(tmp_path / "knowledge" / "target")
    assert {item.document_id for item in loaded} == {doc.document_id for _, doc in archived}
    assert len(list((tmp_path / "knowledge" / "target").rglob("*.md"))) == 2
    assert not (tmp_path / "unsafe.png").exists()
    client.page_calls.clear()
    second = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert second["result"]["changed"] == 0
    assert len(published) == 1
    assert all(not body for _, body, _ in client.page_calls)
    client.asset_version = 2
    third = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert third["result"]["changed"] == 2
    assert instance.get_source(source["id"])["revision_count"] == 4
    client.version = 3
    instance.upsert_source({"include_history": True}, source_id=source["id"])
    fourth = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert fourth["result"]["archived_versions"] == 2
    assert any(doc.metadata["page_version"] == 2 for _, doc in archived)
    assert len({doc.metadata["revision_id"] for _, doc in archived}) == len(archived)


def test_discovery_failure_does_not_remove_pages_and_403_quarantines(tmp_path):
    instance, client, _, _, visibility = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    instance.run_job(instance.queue_sync(source["id"])["id"])
    client.discovery_error = True
    failed = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert failed["status"] == "failed"
    assert all(page["state"] == "current" for page in instance.source_pages(source["id"]))
    client.discovery_error, client.denied = False, True
    partial = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert partial["status"] == "completed_with_warnings"
    assert visibility[-1] == ("target", f"confluence:{source['id']}:2", False)
    assert instance.source_pages(source["id"])[1]["state"] == "access_lost"
    with instance._db() as db:
        asset = db.execute(
            "SELECT id FROM assets WHERE source_id=? AND page_id='2'", (source["id"],)
        ).fetchone()[0]
    with pytest.raises(KeyError):
        instance.asset_path(asset, source_id=source["id"])


def test_page_move_refreshes_tree_position_and_preserves_source_version(tmp_path):
    instance, client, published, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    instance.run_job(instance.queue_sync(source["id"])["id"])
    original = client.page

    def moved(page_id, **kwargs):
        page = original(page_id, **kwargs)
        return page.model_copy(update={"parent_id": "new-parent"}) if page_id == "2" else page

    client.page = moved
    done = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert done["result"]["unchanged"] == 1 and len(published) == 2
    moved_page = instance.source_pages(source["id"])[1]
    assert moved_page["parent_id"] == "new-parent" and moved_page["version"] == 1


def test_jobs_deduplicate_cancel_cursor_logs_and_resume_after_publish_failure(tmp_path):
    instance, _, published, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    queued = instance.queue_sync(source["id"])
    assert instance.queue_sync(source["id"])["id"] == queued["id"]
    assert instance.cancel_job(queued["id"])["status"] == "cancelled"
    queued = instance.queue_sync(source["id"])
    real_publish = instance.publish
    instance.publish = lambda index, **kwargs: (_ for _ in ()).throw(
        RuntimeError("synthetic build failure")
    )
    assert instance.run_job(queued["id"])["status"] == "failed"
    assert instance.get_source(source["id"])["revision_count"] == 2
    instance.publish = real_publish
    done = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert done["status"] == "completed" and len(published) == 1
    assert instance.get_source(source["id"])["revision_count"] == 2
    first = instance.job_log(done["id"], limit=2)
    second = instance.job_log(done["id"], cursor=first["next_cursor"], limit=2)
    assert first["has_more"] and second["items"][0]["cursor"] > first["next_cursor"]


def test_schedule_timezone_and_pause_persist(tmp_path):
    now = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
    assert (
        Schedule(kind="daily", time="03:00", timezone="Europe/Moscow").next_at(now)
        == "2026-11-13T00:00:00+00:00"
    )
    assert (
        Schedule(kind="weekly", weekday=0, time="03:00", timezone="Europe/Moscow").next_at(now)
        == "2026-11-16T00:00:00+00:00"
    )
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    instance.set_paused(source["id"], True)
    reopened, _, _, _, _ = registry(tmp_path)
    assert reopened.get_source(source["id"])["paused"]


def test_index_progress_preserves_real_page_and_embedding_counters(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")

    def publish(index, *, progress, **kwargs):
        progress({"stage": "embedding", "completed": 8, "total": 8, "reused": 6, "computed": 2})
        progress({"stage": "publication", "message": "Generation published"})
        return {"status": "completed"}

    instance.publish = publish
    job = instance.run_job(instance.queue_sync(source["id"])["id"])
    progress = job["progress"]
    assert progress["pages_done"] == progress["pages_total"] == 2
    assert progress["completed"] == progress["total"] == 8
    assert progress["reused"] == 6 and progress["computed"] == 2
    assert progress["index_stage"] == "publication"
    events = instance.job_log(job["id"])["items"]
    assert any(event["details"].get("computed") == 2 for event in events)


def test_disconnect_keeps_image_evidence_but_not_denied_page_assets(tmp_path):
    instance, client, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    instance.run_job(instance.queue_sync(source["id"])["id"])
    client.denied = True
    instance.run_job(instance.queue_sync(source["id"])["id"])
    with instance._db() as db:
        assets = dict(
            db.execute("SELECT page_id,id FROM assets WHERE source_id=?", (source["id"],))
        )
    instance.delete_source(source["id"])
    assert instance.list_sources() == []
    assert (
        instance.asset_path(assets["1"], source_id=source["id"], version=1).read_bytes()
        == b"image-version-1"
    )
    with pytest.raises(KeyError):
        instance.asset_path(assets["2"], source_id=source["id"])
    with pytest.raises(KeyError):
        instance.asset_path(assets["1"], source_id="different-source")


def test_cloud_pagination_history_and_credentials_never_leave_origin():
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/direct-children"):
            if "/pages/1/" not in request.url.path:
                return httpx.Response(200, json={"results": []})
            if "cursor" in request.url.params:
                return httpx.Response(200, json={"results": [{"id": "3", "type": "page"}]})
            return httpx.Response(
                200,
                json={
                    "results": [{"id": "2", "type": "page"}],
                    "_links": {"next": "/wiki/api/v2/pages/1/direct-children?cursor=next"},
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "1",
                "title": "Root",
                "version": {"number": 2, "createdAt": "2026-01-01T00:00:00Z"},
                "body": {"storage": {"value": "<p>older</p>"}},
            },
        )

    cfg = config(
        base_url="https://team.atlassian.net/wiki",
        root_page_url="https://team.atlassian.net/wiki/spaces/ABC/pages/1/Root",
        username="reader@example.com",
    )
    client = ConfluenceClient(cfg, "secret-token", transport=httpx.MockTransport(handle))
    assert list(client.page_ids(client.root_id())) == ["1", "2", "3"]
    assert client.page("1", version=2).body == "<p>older</p>"
    assert requests[-1].url.params["version"] == "2"
    assert all(request.headers["Authorization"].startswith("Basic ") for request in requests)
    with pytest.raises(ConfluenceError, match="untrusted"):
        list(client._paginate("https://attacker.example/steal"))
    client.close()


def test_rate_limit_retry_and_cloud_media_redirect_strips_authorization():
    seen = []

    def handle(request):
        seen.append(request)
        if request.url.host == "api.media.atlassian.com":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=b"image")
        if request.url.path.endswith("download"):
            return httpx.Response(
                302, headers={"Location": "https://api.media.atlassian.com/file/123/binary"}
            )
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": "1", "version": {"number": 1}})

    cfg = config(
        base_url="https://team.atlassian.net/wiki", root_page_url="1", username="reader@example.com"
    )
    client = ConfluenceClient(cfg, "secret", transport=httpx.MockTransport(handle))
    assert client.page("1").version == 1
    data = client.download(
        Attachment(id="a", title="a.png", download_url="https://team.atlassian.net/wiki/download")
    )
    assert data == b"image"
    client.close()


def test_dc_context_path_and_bounded_download():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=b"123456")

    cfg = config(
        base_url="https://docs.example.com/confluence", root_page_url="1", max_attachment_bytes=4
    )
    client = ConfluenceClient(cfg, "secret", transport=httpx.MockTransport(handle))
    with pytest.raises(ConfluenceError, match="size limit"):
        client.download(Attachment(id="a", title="a.png", download_url="/download/attachments/a"))
    assert paths == ["/confluence/download/attachments/a"]
    client.close()


def test_gigacode_images_validated_cached_and_no_credentials_in_prompt(tmp_path):
    path = tmp_path / "source.png"
    path.write_bytes(b"not-real-image-fixture")
    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        assert kwargs["allow_edits"] is False
        assert (kwargs["checkout"] / "attachment.png").is_file()
        return SimpleNamespace(
            payload={
                "schema_version": 1,
                "status": "ok",
                "kind": "diagram",
                "visible_text": "API → DB",
                "description": "A connection",
                "nodes": [{"id": "a", "label": "API"}, {"id": "b", "label": "DB"}],
                "relations": [{"from": "a", "to": "b", "label": "query", "direction": "directed"}],
                "analyzed_files": ["attachment.png"],
            }
        )

    extractor = GigaCodeImageExtractor(SimpleNamespace(run_json=run), tmp_path / "images")
    assert "API" in extractor.extract(path).markdown()
    assert "DB" in extractor.extract(path).markdown()
    assert len(calls) == 1


def test_gigacode_rejects_fabricated_input_and_edges(tmp_path):
    path = tmp_path / "source.png"
    path.write_bytes(b"fixture")
    result = {"status": "ok", "analyzed_files": ["different.png"]}
    runner = SimpleNamespace(run_json=lambda **kwargs: SimpleNamespace(payload=result))
    extractor = GigaCodeImageExtractor(runner, tmp_path / "images")
    with pytest.raises(ValueError, match="unexpected"):
        extractor.extract(path)
    result.update(analyzed_files=["attachment.png"], relations=[{"from": "none", "to": "none"}])
    with pytest.raises(ValueError, match="unknown node"):
        extractor.extract(path)


def test_scheduler_recovers_interrupted_job_and_asset_scope(tmp_path):
    instance, _, _, _, _ = registry(tmp_path)
    source = instance.upsert_source(config().model_dump(), "secret")
    other = instance.upsert_source(config(name="Other").model_dump(), "different")
    job = instance.queue_sync(source["id"])
    with instance._db() as db:
        db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    restarted, _, published, _, _ = registry(tmp_path)
    restarted.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if restarted.job_status(job["id"])["status"] == "completed":
                break
            time.sleep(0.01)
        assert restarted.job_status(job["id"])["status"] == "completed"
    finally:
        restarted.stop()
    assert published == ["target"]
    with restarted._db() as db:
        asset_id = db.execute(
            "SELECT id FROM assets WHERE source_id=?", (source["id"],)
        ).fetchone()[0]
    with pytest.raises(KeyError):
        restarted.asset_path(asset_id, source_id=other["id"])
    assert restarted.asset_path(asset_id, source_id=source["id"]).is_file()


def test_image_failure_retains_original_and_retries_then_model_change_invalidates(tmp_path):
    calls = []

    class Extractor:
        identity = "model-v1"

        def extract(self, path, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("temporary extraction problem")
            return ImageExtraction(
                status="ok", visible_text="diagram evidence", analyzed_files=[path.name]
            )

    extractor = Extractor()
    instance, _, _, _, _ = registry(tmp_path, image_extractor=extractor)
    source = instance.upsert_source(config(analyze_images=True).model_dump(), "secret")
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert first["status"] == "completed_with_warnings"
    assert instance.get_source(source["id"])["asset_count"] == 2
    assert instance.run_job(instance.queue_sync(source["id"])["id"])["status"] == "completed"
    assert len(calls) == 3
    extractor.identity = "model-v2"
    instance.run_job(instance.queue_sync(source["id"])["id"])
    assert len(calls) == 5


def test_connection_check_is_read_only_and_index_lock_wait_is_cancellable(tmp_path):
    lock = threading.Lock()
    instance, _, _, _, _ = registry(tmp_path, index_guard=lambda index: lock)
    assert instance.test_connection(config().model_dump(), "secret")["status"] == "connected"
    assert instance.list_sources() == []
    source = instance.upsert_source(config().model_dump(), "secret")
    job = instance.queue_sync(source["id"])
    lock.acquire()
    worker = threading.Thread(target=instance.run_job, args=(job["id"],))
    worker.start()
    try:
        deadline = time.monotonic() + 2
        while instance.job_status(job["id"])["status"] == "queued" and time.monotonic() < deadline:
            time.sleep(0.01)
        instance.cancel_job(job["id"])
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert instance.job_status(job["id"])["status"] == "cancelled"
    finally:
        lock.release()
        worker.join(timeout=1)


def test_real_history_service_keeps_one_revision_per_snapshot(settings_factory, tmp_path):
    from corporate_kb.service import KnowledgeService

    settings = settings_factory(
        knowledge_dir=tmp_path / "documents",
        history_dir=tmp_path / "history",
        cache_dir=tmp_path / "cache",
    )
    settings.knowledge_dir.mkdir()
    service = KnowledgeService(settings)
    service.build_index(force=True)
    client = FakeClient()
    client.attachments = lambda page_id: []
    instance = ConfluenceRegistry(
        tmp_path / "sync",
        index_exists=lambda index: index == "target",
        knowledge_root=lambda index: settings.knowledge_dir,
        publish=lambda index, **kwargs: service.build_index(force=True, reuse_unchanged=True),
        archive_document=lambda index, document: service.archive_document(document),
        page_visibility=lambda index, page_id, visible: service.set_page_visibility(
            page_id, visible
        ),
        client_factory=lambda conf, token: client,
    )
    source = instance.upsert_source(config().model_dump(), "secret")
    first = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert first["status"] == "completed", first
    logical = f"confluence:{source['id']}:2"
    client.version = 2
    second = instance.run_job(instance.queue_sync(source["id"])["id"])
    assert second["status"] == "completed", second
    assert service.read_page(logical).source_version == "2"
    assert service.read_page(logical, source_version="1").source_version == "1"
    with service.history._db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM revisions WHERE page_id=?", (logical,)).fetchone()[0]
            == 2
        )
    client.denied = True
    instance.run_job(instance.queue_sync(source["id"])["id"])
    with pytest.raises(KeyError):
        service.read_page(logical, source_version="1")
