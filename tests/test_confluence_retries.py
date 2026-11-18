from __future__ import annotations

import json
import logging
import threading
from collections import Counter

import pytest

from corporate_kb.confluence.models import (
    Attachment,
    ConfluenceError,
    Page,
    SourceConfig,
    SyncCancelled,
)
from corporate_kb.confluence.registry import ConfluenceRegistry


class RetryClient:
    def __init__(self):
        self.cancel = threading.Event()
        self.ids = ["1", "2", "3"]
        self.version = 1
        self.summary_calls = []
        self.failures = Counter()
        self.history_failures = set()
        self.attachment_failures = set()
        self.discovery_calls = 0
        self.discovery_error = False
        self.on_summary = None

    def close(self):
        pass

    def root_id(self):
        return "1"

    def page_ids(self, root):
        self.discovery_calls += 1
        yield root
        if self.discovery_error:
            raise ConfluenceError("Page discovery unavailable", status_code=503)
        yield from self.ids[1:]

    def page(self, page_id, *, body=True, version=None):
        if not body:
            self.summary_calls.append(page_id)
            if self.on_summary:
                self.on_summary(page_id)
            if self.failures[page_id]:
                self.failures[page_id] -= 1
                raise AttributeError("confidential-body pat-secret https://secret.example/?token=x")
        return Page(
            id=page_id,
            title=f"Page {page_id}",
            version=version or self.version,
            updated_at=f"2026-01-0{version or self.version}T00:00:00Z",
            parent_id=None if page_id == "1" else "1",
            url=f"https://docs.example.com/pages/{page_id}",
            body=f"<p>revision {version or self.version}</p>" if body else "",
        )

    def attachments(self, page_id):
        if page_id in self.attachment_failures:
            raise ConfluenceError("Attachment listing unavailable", status_code=404)
        return []

    def versions(self, page_id):
        if page_id in self.history_failures:
            raise ConfluenceError("Version listing unavailable", status_code=404)
        return list(range(self.version, 0, -1))


class BranchClient(RetryClient):
    def __init__(self):
        super().__init__()
        self.children_calls = []
        self.broken_branch = False

    def children(self, content_id, kind="page"):
        self.children_calls.append(content_id)
        if content_id == "1":
            yield "2", "page"
            yield "3", "page"
        elif content_id == "2":
            if self.broken_branch:
                raise ConfluenceError("Child listing unavailable", status_code=404)
            yield "4", "page"
        elif content_id == "3":
            yield "5", "page"


def make_registry(tmp_path, client):
    published, visibility = [], []
    instance = ConfluenceRegistry(
        tmp_path / "state",
        index_exists=lambda index: index == "target",
        knowledge_root=lambda index: tmp_path / "knowledge" / index,
        publish=lambda index, **kwargs: published.append(index) or {"status": "completed"},
        page_visibility=lambda *args: visibility.append(args),
        client_factory=lambda config, token: client,
    )
    return instance, published, visibility


def add_source(instance, **values):
    config = SourceConfig.model_validate(
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
    return instance.upsert_source(config.model_dump(), "pat-secret")


def synchronize(instance, source):
    return instance.run_job(instance.queue_sync(source["id"])["id"])


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr(ConfluenceRegistry, "RETRY_DELAY_SECONDS", 0)


def test_page_failure_is_retried_after_remaining_pages_and_recovers(tmp_path):
    client = RetryClient()
    client.failures["1"] = 1
    instance, published, _ = make_registry(tmp_path, client)
    source = add_source(instance)

    completed = synchronize(instance, source)

    assert ConfluenceRegistry.RETRY_ROUNDS == 2
    assert completed["status"] == "completed"
    assert client.summary_calls == ["1", "2", "3", "1"]
    assert completed["result"]["recovered_pages"] == 1
    assert completed["result"]["failed_pages"] == completed["result"]["failed_items"] == 0
    assert completed["result"]["changed"] == 3
    assert instance.job_failures(completed["id"])["items"] == []
    assert len(instance.source_pages(source["id"])) == 3
    assert published == ["target"]


def test_persistent_page_failure_is_reported_after_three_attempts_without_leaking(tmp_path, caplog):
    client = RetryClient()
    client.failures["1"] = 20
    instance, published, _ = make_registry(tmp_path, client)
    source = add_source(instance)

    with caplog.at_level(logging.INFO):
        completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    assert client.summary_calls == ["1", "2", "3", "1", "1"]
    assert completed["result"]["failed_pages"] == completed["result"]["failed_items"] == 1
    assert completed["result"]["recovered_pages"] == 0
    assert completed["result"]["changed"] == 2
    failures = instance.job_failures(completed["id"])
    assert len(failures["items"]) == 1
    failed = failures["items"][0]
    assert failed["page_id"] == "1" and failed["task_kind"] == "page"
    assert failed["attempts"] == 3 and failed["status"] == "failed"
    assert failed["error_type"] == "AttributeError"
    assert failed["stage"] and failed["error"] and failed["url"]
    assert {"title", "status_code"} <= failed.keys()
    observed = caplog.text + json.dumps(
        [completed, failures, instance.job_log(completed["id"])], ensure_ascii=False
    )
    for secret in ("confidential-body", "pat-secret", "secret.example", "token=x"):
        assert secret not in observed
    assert {page["page_id"] for page in instance.source_pages(source["id"])} == {"2", "3"}
    assert published == ["target"]


def test_history_404_keeps_latest_page_and_other_pages_are_published(tmp_path):
    client = RetryClient()
    client.version = 2
    client.history_failures.add("1")
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance, include_history=True)

    completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    pages = {page["page_id"]: page for page in instance.source_pages(source["id"])}
    assert set(pages) == {"1", "2", "3"}
    assert all(page["state"] == "current" and page["version"] == 2 for page in pages.values())
    assert "revision 2" in instance._current_path("target", source["id"], "1").read_text()
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    assert len(failures) == 1
    assert failures[0]["page_id"] == "1" and failures[0]["status_code"] == 404
    assert failures[0]["stage"] == "history" and failures[0]["attempts"] == 3
    assert completed["result"]["failed_pages"] == 1
    assert published == ["target"]


def test_attachment_list_404_preserves_previously_indexed_page(tmp_path):
    client = RetryClient()
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance)
    assert synchronize(instance, source)["status"] == "completed"
    original = instance._current_path("target", source["id"], "1").read_bytes()
    visibility.clear()
    client.version = 2
    client.attachment_failures.add("1")

    completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    pages = {page["page_id"]: page for page in instance.source_pages(source["id"])}
    assert pages["1"]["state"] == "current" and pages["1"]["version"] == 1
    assert pages["2"]["version"] == pages["3"]["version"] == 2
    assert instance._current_path("target", source["id"], "1").read_bytes() == original
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    assert len(failures) == 1 and failures[0]["page_id"] == "1"
    assert failures[0]["status_code"] == 404 and failures[0]["attempts"] == 3
    assert "attachment" in failures[0]["stage"]
    assert published == ["target", "target"]


@pytest.mark.parametrize("failure_count", [1, 20])
def test_failed_attachment_download_is_retried_and_not_silently_skipped(tmp_path, failure_count):
    class DownloadClient(RetryClient):
        def __init__(self):
            super().__init__()
            self.download_calls = Counter()

        def attachments(self, page_id):
            return [
                Attachment(
                    id=page_id,
                    title="diagram.png",
                    version=1,
                    media_type="image/png",
                    download_url=f"https://docs.example.com/download/{page_id}",
                )
            ]

        def download(self, attachment):
            self.download_calls[attachment.id] += 1
            if attachment.id == "1" and self.download_calls["1"] <= failure_count:
                raise ConfluenceError("Attachment download unavailable", status_code=503)
            return b"archived-image-bytes"

    client = DownloadClient()
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance)

    completed = synchronize(instance, source)

    assert client.summary_calls[:3] == ["1", "2", "3"]
    assert client.download_calls["1"] == min(failure_count + 1, 3)
    assert client.download_calls["2"] == client.download_calls["3"] == 1
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    if failure_count == 1:
        assert completed["status"] == "completed"
        assert completed["result"]["recovered_pages"] == 1
        assert completed["result"]["failed_pages"] == 0
        assert failures == []
    else:
        assert completed["status"] == "completed_with_warnings"
        assert completed["result"]["failed_pages"] == 1
        assert len(failures) == 1 and failures[0]["page_id"] == "1"
        assert failures[0]["attempts"] == 3 and failures[0]["status_code"] == 503
        assert "attachment" in failures[0]["stage"]
    assert published == ["target"]


def test_explicit_cancel_is_not_recorded_as_a_page_failure(tmp_path):
    client = RetryClient()
    client.failures["1"] = 1
    instance, published, _ = make_registry(tmp_path, client)
    source = add_source(instance)
    queued = instance.queue_sync(source["id"])

    def cancel_on_second_page(page_id):
        if page_id == "2":
            instance.cancel_job(queued["id"])
            raise SyncCancelled("Stopped by user")

    client.on_summary = cancel_on_second_page
    cancelled = instance.run_job(queued["id"])

    assert cancelled["status"] == "cancelled"
    assert client.summary_calls == ["1", "2"]
    failures = instance.job_failures(queued["id"])["items"]
    assert len(failures) == 1 and failures[0]["page_id"] == "1"
    assert failures[0]["error_type"] != "SyncCancelled"
    assert published == []


@pytest.mark.parametrize("resume_failures", [0, 2])
def test_shutdown_and_new_registry_resume_durable_retry_queue(tmp_path, resume_failures):
    client = RetryClient()
    client.failures["1"] = 1
    instance, _, _ = make_registry(tmp_path, client)
    source = add_source(instance)
    queued = instance.queue_sync(source["id"])

    def shutdown_on_third_page(page_id):
        if page_id == "3":
            instance._stop.set()
            client.cancel.set()
            raise SyncCancelled("Server shutting down")

    client.on_summary = shutdown_on_third_page
    interrupted = instance.run_job(queued["id"])
    assert interrupted["status"] == "queued"
    pending = instance.job_failures(queued["id"])["items"]
    assert len(pending) == 1 and pending[0]["page_id"] == "1"
    assert pending[0]["attempts"] == 1 and pending[0]["status"] == "retry"

    next_client = RetryClient()
    next_client.failures["3"] = resume_failures
    reopened, published, _ = make_registry(tmp_path, next_client)
    assert reopened.job_failures(queued["id"])["items"] == pending
    completed = reopened.run_job(queued["id"])

    assert completed["status"] == "completed"
    assert "2" not in next_client.summary_calls
    assert set(next_client.summary_calls) == {"1", "3"}
    assert next_client.summary_calls.count("3") == resume_failures + 1
    assert completed["result"]["recovered_pages"] == 1 + bool(resume_failures)
    assert completed["result"]["failed_pages"] == 0
    assert len(reopened.source_pages(source["id"])) == 3
    assert reopened.job_failures(queued["id"])["items"] == []
    assert published == ["target"]


def test_shutdown_during_final_deferred_attempt_retains_remaining_attempt(tmp_path):
    client = RetryClient()
    client.failures["1"] = 2
    instance, _, _ = make_registry(tmp_path, client)
    source = add_source(instance)
    queued = instance.queue_sync(source["id"])

    def interrupt_final_attempt(page_id):
        if page_id == "1" and client.summary_calls.count("1") == 3:
            instance._stop.set()
            client.cancel.set()
            raise SyncCancelled("Server shutting down during the final deferred attempt")

    client.on_summary = interrupt_final_attempt
    interrupted = instance.run_job(queued["id"])
    assert interrupted["status"] == "queued"
    pending = instance.job_failures(queued["id"])["items"]
    assert len(pending) == 1 and pending[0]["page_id"] == "1"
    assert pending[0]["attempts"] == 2 and pending[0]["status"] == "retry"

    next_client = RetryClient()
    reopened, published, _ = make_registry(tmp_path, next_client)
    completed = reopened.run_job(queued["id"])

    assert completed["status"] == "completed"
    assert next_client.summary_calls == ["1"]
    assert completed["result"]["recovered_pages"] == 1
    assert completed["result"]["changed"] == 3
    assert completed["result"]["failed_items"] == 0
    assert reopened.job_failures(queued["id"])["items"] == []
    assert published == ["target"]


def test_partial_history_progress_is_published_when_latest_page_is_unchanged(tmp_path):
    class PartialHistoryClient(RetryClient):
        def __init__(self):
            super().__init__()
            self.historical_calls = []

        def versions(self, page_id):
            return [3, 2, 1] if page_id == "1" else [3]

        def page(self, page_id, *, body=True, version=None):
            if version is not None:
                self.historical_calls.append((page_id, version))
                if page_id == "1" and version == 1:
                    raise ConfluenceError(
                        "Historical revision temporarily unavailable", status_code=503
                    )
            return super().page(page_id, body=body, version=version)

    client = PartialHistoryClient()
    client.version = 3
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance)
    assert synchronize(instance, source)["status"] == "completed"
    original = instance._current_path("target", source["id"], "1").read_bytes()
    instance.upsert_source({"include_history": True}, source_id=source["id"])
    visibility.clear()

    completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    assert completed["result"]["changed"] == 0
    assert completed["result"]["archived_versions"] == 1
    assert client.historical_calls.count(("1", 2)) == 1
    assert client.historical_calls.count(("1", 1)) == 3
    assert instance.get_source(source["id"])["revision_count"] == 4
    assert instance._current_path("target", source["id"], "1").read_bytes() == original
    assert all(page["state"] == "current" for page in instance.source_pages(source["id"]))
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    assert len(failures) == 1 and failures[0]["stage"] == "history"
    assert failures[0]["status_code"] == 503 and failures[0]["attempts"] == 3
    assert published == ["target", "target"]


def test_partial_legacy_discovery_processes_found_pages_and_preserves_old_pages(tmp_path):
    client = RetryClient()
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance)
    assert synchronize(instance, source)["status"] == "completed"
    client.discovery_error = True
    client.discovery_calls = 0
    client.summary_calls.clear()
    client.version = 2
    visibility.clear()

    completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    assert client.discovery_calls == 3
    pages = {page["page_id"]: page for page in instance.source_pages(source["id"])}
    assert pages["1"]["version"] == 2
    assert pages["2"]["version"] == pages["3"]["version"] == 1
    assert all(page["state"] == "current" for page in pages.values())
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    assert len(failures) == 1 and failures[0]["task_kind"] == "discovery"
    assert failures[0]["attempts"] == 3 and failures[0]["status_code"] == 503
    assert published == ["target", "target"]


def test_failed_discovery_branch_does_not_block_siblings_or_remove_old_descendants(tmp_path):
    client = BranchClient()
    instance, published, visibility = make_registry(tmp_path, client)
    source = add_source(instance)
    assert synchronize(instance, source)["status"] == "completed"
    assert {page["page_id"] for page in instance.source_pages(source["id"])} == {
        "1",
        "2",
        "3",
        "4",
        "5",
    }
    client.version = 2
    client.broken_branch = True
    client.children_calls.clear()
    visibility.clear()

    completed = synchronize(instance, source)

    assert completed["status"] == "completed_with_warnings"
    assert client.children_calls.count("2") == 3
    assert "3" in client.children_calls and "5" in client.children_calls
    pages = {page["page_id"]: page for page in instance.source_pages(source["id"])}
    assert pages["4"]["state"] == "current" and pages["4"]["version"] == 1
    assert pages["5"]["state"] == "current" and pages["5"]["version"] == 2
    assert pages["2"]["state"] == "current" and pages["2"]["version"] == 2
    assert all(is_visible for _, _, is_visible in visibility)
    failures = instance.job_failures(completed["id"])["items"]
    assert len(failures) == 1
    assert failures[0]["page_id"] == "2" and failures[0]["task_kind"] == "discovery"
    assert failures[0]["status_code"] == 404 and failures[0]["attempts"] == 3
    assert published == ["target", "target"]
