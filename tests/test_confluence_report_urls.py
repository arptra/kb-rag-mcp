"""Failure report links belong to the connection used by the recorded job."""

from __future__ import annotations

import threading

import pytest

from corporate_kb.confluence.models import ConfluenceError, SourceConfig
from corporate_kb.confluence.registry import ConfluenceRegistry


class FailedPageClient:
    def root_id(self):
        return "1"

    def children(self, content_id, *, kind="page"):
        if content_id == "1":
            yield "2", "page"

    def page(self, page_id, *, body=True):
        raise ConfluenceError("Page unavailable", status_code=404)

    def close(self):
        pass


class FailedRootClient(FailedPageClient):
    def root_id(self):
        raise ConfluenceError("Root lookup unavailable", status_code=503)


def setup_registry(tmp_path, client, *, cloud=False):
    registry = ConfluenceRegistry(
        tmp_path / "state",
        index_exists=lambda index: True,
        knowledge_root=lambda index: tmp_path / "knowledge",
        publish=lambda *args, **kwargs: {"status": "completed"},
        client_factory=lambda config, token: client,
    )
    config = SourceConfig(
        name="Source",
        index_id="target",
        base_url="https://original.atlassian.net"
        if cloud
        else "https://original.example/confluence",
        root_page_url="1",
        username="reader@example.com" if cloud else None,
        analyze_images=False,
        schedule={"kind": "manual"},
    )
    source = registry.upsert_source(config.model_dump(), "private-token")
    return registry, config, source


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(ConfluenceRegistry, "RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize("cloud", [False, True])
def test_page_failure_report_keeps_original_urls_after_source_edit(tmp_path, cloud):
    registry, config, source = setup_registry(tmp_path, FailedPageClient(), cloud=cloud)
    job = registry.run_job(registry.queue_sync(source["id"])["id"])
    before = registry.job_failures(job["id"])
    original_base = config.base_url + ("/wiki" if cloud else "")
    assert {item["url"] for item in before["items"]} == {
        f"{original_base}/pages/viewpage.action?pageId=1",
        f"{original_base}/pages/viewpage.action?pageId=2",
    }

    registry.upsert_source(
        {"base_url": "https://replacement.example/new-context", "root_page_url": "99"},
        token="replacement-server-token",
        source_id=source["id"],
    )

    assert registry.job_failures(job["id"]) == before


@pytest.mark.parametrize("cloud", [False, True])
def test_failed_numeric_root_has_absolute_original_confluence_url(tmp_path, cloud):
    registry, config, source = setup_registry(tmp_path, FailedRootClient(), cloud=cloud)
    job = registry.run_job(registry.queue_sync(source["id"])["id"])
    item = registry.job_failures(job["id"])["items"][0]
    original_base = config.base_url + ("/wiki" if cloud else "")
    assert item["page_id"] == "@root"
    assert item["url"] == f"{original_base}/pages/viewpage.action?pageId=1"

    registry.upsert_source({"root_page_url": "99"}, source_id=source["id"])

    assert registry.job_failures(job["id"])["items"][0]["url"] == item["url"]


def test_discovery_retry_requeues_branch_after_enqueue_write_failure(tmp_path, monkeypatch):
    client = FailedPageClient()
    registry, config, source = setup_registry(tmp_path, client)
    job = registry.queue_sync(source["id"])
    original = registry._enqueue_item
    failures = []

    def fail_once(job_id, task_kind, page_id, content_kind="page", *, url=""):
        if task_kind == "discovery" and page_id == "2" and not failures:
            failures.append(page_id)
            raise OSError("Temporary write failure")
        return original(job_id, task_kind, page_id, content_kind, url=url)

    monkeypatch.setattr(registry, "_enqueue_item", fail_once)

    registry._discover_pages(job["id"], config, client, threading.Event())

    with registry._db() as connection:
        branch = connection.execute(
            "SELECT status,url FROM sync_items WHERE job_id=? AND task_kind='discovery' "
            "AND page_id='2'",
            (job["id"],),
        ).fetchone()
    assert branch is not None and branch["status"] == "done"
    assert branch["url"] == "https://original.example/confluence/pages/viewpage.action?pageId=2"
    assert registry._queue_counts(job["id"])["discovery_complete"]
