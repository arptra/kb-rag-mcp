from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path

import pytest

from corporate_kb.catalog import RagCatalog
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.index_runner import IndexBuildCancelled, IndexBuildProcessRunner
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService
from corporate_kb.usage import UsageTracker
from service_map import ServiceMapBuildCancelled, ServiceMapProcessRunner


def _wait_for_job(catalog: RagCatalog, job_id: str) -> dict[str, object]:
    current: dict[str, object] = {}
    for _ in range(500):
        current = next(item for item in catalog.payload()["jobs"] if item["id"] == job_id)
        if current["status"] not in {"queued", "running", "cancelling"}:
            return current
        time.sleep(0.01)
    raise AssertionError(f"Job did not finish: {job_id} ({current})")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_fake_gigacode(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys

if "--version" in sys.argv:
    print("0.99.0-catalog-test")
    raise SystemExit(0)

prompt = sys.stdin.read()
if "STATIC ANALYSIS" not in prompt or "read-only repository analyst" not in prompt:
    raise SystemExit(7)
print("Open authentication URL: https://auth.example/gigacode/device", file=sys.stderr)
print(json.dumps({
    "type": "system",
    "subtype": "session_start",
    "session_id": "catalog-gigacode-session",
    "model": "catalog-fake-gigacode",
}))
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "session_id": "catalog-gigacode-session",
    "model": "catalog-fake-gigacode",
    "is_error": False,
    "duration_ms": 23,
    "usage": {"total_tokens": 88},
    "structured_result": {
        "markdown": (
            "# GigaCode analyzed lifecycle service\\n\\nThe read-only GigaCode repository "
            "scan observed GET /gigacode/status in GigaCodeController.java and did not invent "
            "unsupported runtime behavior.\\n"
        ),
        "analyzed_files": ["src/main/java/example/GigaCodeController.java"],
        "blocking_unknowns": ["Production traffic behavior is unknown."],
    },
}))
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_catalog_uploads_and_pages_documents_for_one_index(settings_factory) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Manual knowledge")

    uploaded = catalog.upload_documents(
        index.id,
        documents=[
            {"path": "runbook.md", "content": "# Worker runbook\n\nRestart the worker."},
            {"path": "limits.json", "content": '{"owner": "limits-service"}'},
        ],
    )
    finished = _wait_for_job(catalog, uploaded["job"]["id"])
    assert finished["status"] == "completed"
    assert uploaded["file_count"] == 2
    assert (Path(index.knowledge_dir) / "uploads" / "runbook.md").is_file()

    first_page = catalog.index_documents(index.id, limit=1)
    assert first_page["total"] == 2
    assert first_page["has_more"] is True
    assert len(first_page["documents"]) == 1
    second_page = catalog.index_documents(index.id, offset=1, limit=1)
    assert second_page["has_more"] is False
    by_query = catalog.index_documents(index.id, query="worker", limit=20)
    assert [item["title"] for item in by_query["documents"]] == ["Worker runbook"]
    assert by_query["documents"][0]["origin"] == "upload"
    detail = catalog.index_document(
        index.id,
        by_query["documents"][0]["document_id"],
    )
    assert detail["content"] == "# Worker runbook\n\nRestart the worker."
    assert detail["index"] == {"id": index.id, "name": "Manual knowledge"}
    assert detail["content_bytes"] == len(detail["content"].encode("utf-8"))

    with pytest.raises(ValueError, match="safe relative path"):
        catalog.upload_documents(
            index.id,
            documents=[{"path": "../escape.md", "content": "unsafe"}],
        )
    with pytest.raises(ValueError, match="Binary-looking"):
        catalog.upload_documents(
            index.id,
            documents=[{"path": "binary.txt", "content": "bad\x00data"}],
        )


def test_catalog_creates_index_and_imports_local_openspec(settings_factory, tmp_path) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "base.md").write_text(
        "# Base\n\nPrimary knowledge.",
        encoding="utf-8",
    )
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    default_tools = KnowledgeTools(default_service, usage=usage)
    catalog = RagCatalog(settings, default_service, default_tools, usage)

    index = catalog.create_index(name="Payments platform", description="OpenSpec state")
    assert index.status == "ready"
    assert index.document_count == 0

    repository = tmp_path / "payments-repository"
    openspec = repository / "openspec" / "current"
    openspec.mkdir(parents=True)
    (openspec / "limits.md").write_text(
        "# Limits state\n\nlimits-service owns payment limits.",
        encoding="utf-8",
    )
    job = catalog.start_repository_ingestion(
        name="payments-service",
        git_url=str(repository),
        index_id=index.id,
    )
    for _ in range(200):
        payload = catalog.payload()
        current = next(item for item in payload["jobs"] if item["id"] == job.id)
        if current["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    refreshed = next(item for item in catalog.payload()["indexes"] if item["id"] == index.id)
    assert refreshed["document_count"] == 1
    assert refreshed["source_count"] == 1
    result = catalog.tools_for(index.id).search(query="payment limits", top_k=1)
    assert result["results"][0]["source_path"].endswith("openspec/current/limits.md")
    assert catalog.graph_overview()["node_count"] >= 2
    assert {item["label"] for item in catalog.graph_overview()["services"]} == {"payments-service"}
    assert settings.service_map_path.is_file()
    assert catalog.service_map_overview()["service_count"] == 1
    service_map = catalog.service_map()
    assert {item["name"] for item in service_map["services"]} == {"payments-service"}

    settings.service_map_path.unlink()
    reloaded = RagCatalog(settings, default_service, default_tools, usage)
    restored_job = next(item for item in reloaded.payload()["jobs"] if item["id"] == job.id)
    assert restored_job["status"] == "completed"
    assert reloaded.service_map_overview()["service_count"] == 1


def test_catalog_marks_interrupted_jobs_failed_after_restart(settings_factory) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    default_tools = KnowledgeTools(default_service, usage=usage)
    catalog = RagCatalog(settings, default_service, default_tools, usage)
    index = catalog.create_index(name="Interrupted index")
    job = catalog._new_job("repository", index_id=index.id, message="Repository import queued")
    catalog._update_index(index.id, status="indexing")
    catalog._update_job(job.id, status="running", message="Cloning repository")

    reloaded = RagCatalog(settings, default_service, default_tools, usage)
    restored_job = next(item for item in reloaded.payload()["jobs"] if item["id"] == job.id)
    restored_index = next(item for item in reloaded.payload()["indexes"] if item["id"] == index.id)
    assert restored_job["status"] == "failed"
    assert restored_job["message"] == "Interrupted by server restart"
    assert restored_index["status"] == "error"


def test_catalog_maps_repository_without_openspec(settings_factory, tmp_path) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    default_tools = KnowledgeTools(default_service, usage=usage)
    catalog = RagCatalog(settings, default_service, default_tools, usage)
    index = catalog.create_index(name="Source-only services")

    repository = tmp_path / "source-only-repository"
    resources = repository / "src" / "main" / "resources"
    sources = repository / "src" / "main" / "java" / "example"
    resources.mkdir(parents=True)
    sources.mkdir(parents=True)
    (resources / "application.properties").write_text(
        "spring.application.name=source-only-service\n",
        encoding="utf-8",
    )
    (sources / "StatusController.java").write_text(
        """
package example;
@RestController
@RequestMapping("/status")
public class StatusController {
  @GetMapping
  public String status() { return "ok"; }
}
""",
        encoding="utf-8",
    )

    job = catalog.start_repository_ingestion(
        name="Source only",
        git_url=str(repository),
        index_id=index.id,
    )
    for _ in range(200):
        payload = catalog.payload()
        current = next(item for item in payload["jobs"] if item["id"] == job.id)
        if current["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    imported = next(
        item for item in catalog.payload()["repositories"] if item["name"] == "Source only"
    )
    assert imported["openspec_path"] is None
    assert imported["document_count"] == 0
    service_map = catalog.service_map()
    assert service_map["services"][0]["id"] == "source-only-service"
    assert service_map["services"][0]["entrypoints"][0]["operation"] == "GET /status"


def test_failed_repository_import_remains_visible_and_retryable(
    settings_factory,
    tmp_path,
    monkeypatch,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Retry failed repository")
    repository = tmp_path / "unfinished-repository"
    repository.mkdir()
    original_find_openspecs = catalog._find_openspecs
    attempts = 0

    def fail_first_scan(checkout, *, cancel_event=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("unfinished repository scan failed")
        return original_find_openspecs(checkout, cancel_event=cancel_event)

    monkeypatch.setattr(catalog, "_find_openspecs", fail_first_scan)
    failed_job = catalog.start_repository_ingestion(
        name="Unfinished repository",
        git_url=str(repository),
        index_id=index.id,
    )

    failed = _wait_for_job(catalog, failed_job.id)
    assert failed["status"] == "failed"
    imported = next(
        item
        for item in catalog.payload()["repositories"]
        if item["name"] == "Unfinished repository"
    )
    assert failed_job.target_id == imported["id"]

    retry_job = catalog.start_repository_refresh(imported["id"])
    retried = _wait_for_job(catalog, retry_job.id)

    assert retried["status"] == "completed"
    assert retry_job.target_id == imported["id"]
    refreshed = next(
        item for item in catalog.payload()["repositories"] if item["id"] == imported["id"]
    )
    assert refreshed["document_count"] == 0


def test_system_ssot_session_serves_sources_and_accepts_client_generated_documents(
    settings_factory,
    tmp_path,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Generated system SSOT")
    manual_ssot = Path(index.knowledge_dir) / "ssot" / "primary-lifecycle-service.md"
    _write(
        manual_ssot,
        "---\ndocument_type: ssot\nservice: primary-lifecycle-service\nstatus: current\n"
        "---\n\n# Human-reviewed SSOT\n\nThis manual document must never be overwritten.\n",
    )
    repository = tmp_path / "generated-ssot-repository"
    _write(repository / "settings.gradle", "include 'primary', 'fallback'\n")
    _write(repository / "primary" / "build.gradle", "plugins { id 'java' }\n")
    _write(
        repository / "primary" / "src" / "main" / "resources" / "application.properties",
        "spring.application.name=primary-lifecycle-service\n",
    )
    _write(
        repository
        / "primary"
        / "src"
        / "main"
        / "java"
        / "example"
        / "LifecycleController.java",
        """
package example;
@RestController
public class LifecycleController {
  @GetMapping("/lifecycle/status")
  public String status() { return "ok"; }
}
""",
    )
    _write(repository / "fallback" / "build.gradle", "plugins { id 'java' }\n")
    _write(
        repository / "fallback" / "src" / "main" / "resources" / "application.properties",
        "spring.application.name=fallback-lifecycle-service\n",
    )
    _write(
        repository
        / "fallback"
        / "src"
        / "main"
        / "java"
        / "example"
        / "FallbackController.java",
        """
package example;
@RestController
public class FallbackController {
  @PostMapping("/fallback/retry")
  public void retry() {}
}
""",
    )
    imported = catalog.start_repository_ingestion(
        name="Generated lifecycle repository",
        git_url=str(repository),
        index_id=index.id,
    )
    assert _wait_for_job(catalog, imported.id)["status"] == "completed"

    options = catalog.ssot_generation_request()
    assert options["status"] == "selection_required"
    assert options["workflow"]["server_llm_required"] is False
    assert index.id in {item["id"] for item in options["indexes"]}
    assert imported.target_id in {item["id"] for item in options["repositories"]}

    queued = catalog.ssot_generation_request(
        action="prepare",
        index_id=index.id,
        repository_ids=[str(imported.target_id)],
    )
    job_id = queued["job"]["id"]
    completed = _wait_for_job(catalog, job_id)

    assert completed["status"] == "completed"
    assert completed["result"]["phase"] == "awaiting_client_generation"
    assert completed["result"]["server_llm_used"] is False
    assert completed["result"]["target_count"] == 2

    context = catalog.ssot_generation_request(
        action="context",
        job_id=job_id,
        service_id="primary-lifecycle-service",
    )
    assert context["status"] == "context_ready"
    controller = next(
        item
        for item in context["source_manifest"]["files"]
        if item["path"].endswith("LifecycleController.java")
    )
    source = catalog.ssot_generation_request(
        action="read_file",
        job_id=job_id,
        repository_id=str(imported.target_id),
        file_path=controller["path"],
    )
    assert '@GetMapping("/lifecycle/status")' in source["content"]

    primary_body = (
        "# Generated lifecycle service\n\n"
        "## Functionality\n\n"
        "The client-side model observed the lifecycle status API in "
        "LifecycleController.java and records its behavior with source evidence.\n"
    )
    first = catalog.ssot_generation_request(
        action="submit",
        job_id=job_id,
        service_id="primary-lifecycle-service",
        content=primary_body,
        finalize=False,
    )
    assert first["status"] == "saved"
    second = catalog.ssot_generation_request(
        action="submit",
        job_id=job_id,
        service_id="fallback-lifecycle-service",
        content=(
            "# Generated fallback lifecycle service\n\n"
            "## Functionality\n\n"
            "The client-side model observed the fallback retry API in "
            "FallbackController.java and records the remaining behavior as unknown.\n"
        ),
        finalize=True,
    )
    assert second["status"] == "indexing"
    assert _wait_for_job(catalog, second["index_job"]["id"])["status"] == "completed"

    generated_path = Path(index.knowledge_dir) / "ssot/generated/primary-lifecycle-service.md"
    content = generated_path.read_text(encoding="utf-8")
    assert generated_path.name == "primary-lifecycle-service.md"
    assert 'document_type: "ssot"' in content
    assert 'service: "primary-lifecycle-service"' in content
    assert 'generated_by: "kb_generate_system_ssot/client-agent"' in content
    assert "client-side model observed" in content
    fallback_path = Path(index.knowledge_dir) / "ssot/generated/fallback-lifecycle-service.md"
    assert "client-side model observed" in fallback_path.read_text(encoding="utf-8")
    assert "Human-reviewed SSOT" in manual_ssot.read_text(encoding="utf-8")

    search = catalog.tools_for(index.id).search(query="client-side lifecycle API", top_k=3)
    assert any(
        item["source_path"] == "ssot/generated/primary-lifecycle-service.md"
        for item in search["results"]
    )
    polled = catalog.ssot_generation_request(action="status", job_id=job_id)
    assert polled["status"] == "completed"
    assert polled["job"]["result"]["files"] == [
        "ssot/generated/fallback-lifecycle-service.md",
        "ssot/generated/primary-lifecycle-service.md",
    ]


def test_ssot_workflow_clones_and_handles_an_unfinished_repository(
    settings_factory,
    tmp_path,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Unfinished sources")
    repository = tmp_path / "unfinished-repository"
    repository.mkdir()
    _write(repository / "README.md", "# Started but not implemented\n")

    cloned = catalog.ssot_generation_request(
        action="clone",
        index_id=index.id,
        repository_name="Unfinished repository",
        git_url=str(repository),
    )
    clone_job = _wait_for_job(catalog, cloned["job"]["id"])
    assert clone_job["status"] == "completed"
    repository_id = clone_job["target_id"]

    prepared = catalog.ssot_generation_request(
        action="prepare",
        index_id=index.id,
        repository_ids=[repository_id],
        refresh_analysis=False,
    )
    session = _wait_for_job(catalog, prepared["job"]["id"])
    assert session["status"] == "completed"
    assert session["result"]["target_count"] == 1
    target = session["result"]["targets"][0]
    assert target["kind"] in {"repository", "service"}
    assert target["module_state"] != "complete"

    context = catalog.ssot_generation_request(
        action="context",
        job_id=session["id"],
        service_id=target["id"],
    )
    assert context["source_manifest"]["file_count"] == 1
    assert context["initial_source_files"][0]["path"] == "README.md"
    assert "Started but not implemented" in context["initial_source_files"][0]["content"]


def test_gigacode_mode_scans_repository_generates_ssot_and_rebuilds_index(
    settings_factory,
    tmp_path,
) -> None:
    gigacode = _write_fake_gigacode(tmp_path / "gigacode")
    settings = settings_factory(gigacode_command=str(gigacode))
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="GigaCode generated SSOT")
    repository = tmp_path / "gigacode-repository"
    _write(repository / "build.gradle", "plugins { id 'java' }\n")
    _write(
        repository / "src/main/resources/application.properties",
        "spring.application.name=gigacode-lifecycle-service\n",
    )
    _write(
        repository / "src/main/java/example/GigaCodeController.java",
        """
package example;
@RestController
public class GigaCodeController {
  @GetMapping("/gigacode/status")
  public String status() { return "ok"; }
}
""",
    )
    imported = catalog.start_repository_ingestion(
        name="GigaCode repository",
        git_url=str(repository),
        index_id=index.id,
    )
    assert _wait_for_job(catalog, imported.id)["status"] == "completed"

    options = catalog.ssot_generation_request(action="options")
    assert options["workflow"]["gigacode"]["available"] is True
    queued = catalog.ssot_generation_request(
        action="prepare",
        index_id=index.id,
        repository_ids=[str(imported.target_id)],
        refresh_analysis=False,
        generation_mode="gigacode",
    )
    completed = _wait_for_job(catalog, queued["job"]["id"])

    assert completed["status"] == "completed"
    assert completed["result"]["phase"] == "indexed"
    assert completed["result"]["generation_mode"] == "gigacode"
    assert completed["result"]["gigacode_used"] is True
    assert completed["result"]["gigacode_runs"][0]["session_id"] == "catalog-gigacode-session"
    generated = Path(index.knowledge_dir) / completed["result"]["files"][0]
    content = generated.read_text(encoding="utf-8")
    assert 'generated_by: "kb_generate_system_ssot/gigacode"' in content
    assert "GET /gigacode/status" in content
    search = catalog.tools_for(index.id).search(query="GigaCode status endpoint", top_k=3)
    assert any("GET /gigacode/status" in item["excerpt"] for item in search["results"])
    log = catalog.job_log(queued["job"]["id"])["log"]
    assert "GigaCode starting" in log
    assert "GigaCode authentication URL" in log
    assert "https://auth.example/gigacode/device" in log
    assert "GigaCode result" in log


def test_catalog_indexes_all_module_openspec_roots(settings_factory, tmp_path) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Multi module docs")
    repository = tmp_path / "multi-doc-repository"
    for module, title in (("orders", "Order contract"), ("payments", "Payment contract")):
        path = repository / module / "openspec" / "current.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"# {title}\n\n{title} details.", encoding="utf-8")

    job = catalog.start_repository_ingestion(
        name="Multi docs",
        git_url=str(repository),
        index_id=index.id,
    )
    for _ in range(300):
        current = next(item for item in catalog.payload()["jobs"] if item["id"] == job.id)
        if current["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert current["status"] == "completed"
    imported = next(
        item for item in catalog.payload()["repositories"] if item["name"] == "Multi docs"
    )
    assert len(imported["openspec_paths"]) == 2
    assert imported["document_count"] == 2
    order_result = catalog.tools_for(index.id).search(query="Order contract", top_k=2)
    payment_result = catalog.tools_for(index.id).search(query="Payment contract", top_k=2)
    assert any("orders/current.md" in item["source_path"] for item in order_result["results"])
    assert any("payments/current.md" in item["source_path"] for item in payment_result["results"])


def test_catalog_cancels_running_graph_job_without_blocking_api(
    settings_factory,
    monkeypatch,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    entered = threading.Event()

    def cancellable_build(
        self, repositories, *, cancel=None, progress=None, checkpoint=None
    ):
        del self, repositories, progress, checkpoint
        assert cancel is not None
        entered.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise ServiceMapBuildCancelled("cancelled in test")

    monkeypatch.setattr(ServiceMapProcessRunner, "build", cancellable_build)
    job = catalog.start_graph_build()
    assert entered.wait(timeout=2)

    cancellation = catalog.cancel_job(job.id)
    assert cancellation.status == "cancelling"
    for _ in range(200):
        current = next(item for item in catalog.payload()["jobs"] if item["id"] == job.id)
        if current["status"] == "cancelled":
            break
        time.sleep(0.01)

    assert current["status"] == "cancelled"
    assert current["completed_at"] is not None


def test_catalog_cancels_running_index_process(
    settings_factory,
    monkeypatch,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    entered = threading.Event()

    def cancellable_build(self, *, cancel=None):
        del self
        assert cancel is not None
        entered.set()
        while not cancel.is_set():
            time.sleep(0.01)
        raise IndexBuildCancelled("cancelled in test")

    monkeypatch.setattr(IndexBuildProcessRunner, "build", cancellable_build)
    job = catalog.start_index_build("default")
    assert entered.wait(timeout=2)

    cancellation = catalog.cancel_job(job.id)
    assert cancellation.status == "cancelling"
    for _ in range(200):
        current = next(item for item in catalog.payload()["jobs"] if item["id"] == job.id)
        if current["status"] == "cancelled":
            break
        time.sleep(0.01)

    assert current["status"] == "cancelled"
    refreshed = next(item for item in catalog.payload()["indexes"] if item["id"] == "default")
    assert refreshed["status"] == "ready"


def test_catalog_service_lifecycle_analysis_archive_and_ssot(settings_factory, tmp_path) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    settings.ssot_skill_path.mkdir(parents=True)
    (settings.ssot_skill_path / "SKILL.md").write_text(
        "---\nname: build-service-ssot\ndescription: Test SSOT skill.\n---\n",
        encoding="utf-8",
    )
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )
    index = catalog.create_index(name="Lifecycle index")
    repository = tmp_path / "lifecycle-repository"
    resources = repository / "src" / "main" / "resources"
    sources = repository / "src" / "main" / "java" / "example"
    openspec = repository / "openspec"
    resources.mkdir(parents=True)
    sources.mkdir(parents=True)
    openspec.mkdir(parents=True)
    (resources / "application.properties").write_text(
        "spring.application.name=lifecycle-service\n",
        encoding="utf-8",
    )
    (sources / "LifecycleController.java").write_text(
        """
package example;
@RestController
public class LifecycleController {
  @GetMapping("/lifecycle")
  public String lifecycle() { return "ok"; }
}
""",
        encoding="utf-8",
    )
    (openspec / "overview.md").write_text(
        "# Lifecycle service\n\nTechnical source documentation.",
        encoding="utf-8",
    )

    imported_job = catalog.start_repository_ingestion(
        name="Lifecycle repository",
        git_url=str(repository),
        index_id=index.id,
    )
    assert _wait_for_job(catalog, imported_job.id)["status"] == "completed"
    assert (settings.analysis_archive_dir / "latest.json").is_file()

    analysis_job = catalog.start_service_analysis("lifecycle-service")
    assert _wait_for_job(catalog, analysis_job.id)["status"] == "completed"
    assert "Analysis archived at" in catalog.job_log(analysis_job.id)["log"]

    bundle = catalog.create_ssot_bundle("lifecycle-service")
    bundle_path = catalog.ssot_bundle_path(bundle["bundle_id"])
    with zipfile.ZipFile(bundle_path) as archive:
        assert {
            "analysis/full-analysis.json",
            "analysis/service-analysis.json",
            "PROMPT.md",
            "skill/SKILL.md",
        }.issubset(archive.namelist())

    imported = catalog.import_ssot(
        service_id="lifecycle-service",
        index_id=index.id,
        content=(
            "---\nservice: lifecycle-service\ndocument_type: ssot\nstatus: draft\n---\n\n"
            "# Lifecycle service\n\nThis source-derived draft records the observed lifecycle API "
            "and remains pending human review."
        ),
    )
    assert Path(imported["path"]).is_file()
    assert _wait_for_job(catalog, imported["job"]["id"])["status"] == "completed"

    service_delete = catalog.start_service_delete("lifecycle-service")
    assert _wait_for_job(catalog, service_delete.id)["status"] == "completed"
    assert catalog.service_map_overview()["service_count"] == 0

    repository_record = catalog.payload()["repositories"][0]
    documents = (
        Path(index.knowledge_dir) / "repositories" / repository_record["id"]
    )
    assert documents.is_dir()
    repository_delete = catalog.start_repository_delete(repository_record["id"])
    assert _wait_for_job(catalog, repository_delete.id)["status"] == "completed"
    assert catalog.payload()["repository_count"] == 0
    assert not documents.exists()


def test_catalog_failure_log_contains_full_traceback(settings_factory, monkeypatch) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    default_service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    default_service.build_index(force=True)
    usage = UsageTracker()
    catalog = RagCatalog(
        settings,
        default_service,
        KnowledgeTools(default_service, usage=usage),
        usage,
    )

    def fail_analysis(self, repositories, *, cancel=None, progress=None, checkpoint=None):
        del self, repositories, cancel, progress, checkpoint
        raise RuntimeError("synthetic analysis failure")

    monkeypatch.setattr(ServiceMapProcessRunner, "build", fail_analysis)
    job = catalog.start_graph_build()
    failed = _wait_for_job(catalog, job.id)
    assert failed["status"] == "failed"
    log = catalog.job_log(job.id)["log"]
    assert "Traceback (most recent call last)" in log
    assert "RuntimeError: synthetic analysis failure" in log
