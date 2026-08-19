from __future__ import annotations

import time

from corporate_kb.catalog import RagCatalog
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService
from corporate_kb.usage import UsageTracker


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
    assert {item["label"] for item in catalog.graph_overview()["services"]} == {
        "payments-service"
    }
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
    restored_index = next(
        item for item in reloaded.payload()["indexes"] if item["id"] == index.id
    )
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
        item
        for item in catalog.payload()["repositories"]
        if item["name"] == "Source only"
    )
    assert imported["openspec_path"] is None
    assert imported["document_count"] == 0
    service_map = catalog.service_map()
    assert service_map["services"][0]["id"] == "source-only-service"
    assert service_map["services"][0]["entrypoints"][0]["operation"] == "GET /status"
