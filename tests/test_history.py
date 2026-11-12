import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.models import SearchFilters
from corporate_kb.service import KnowledgeService


def write_page(root: Path, version: int, text: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.md").write_text(
        f"---\nsource_id: confluence:team:123\npage_id: confluence:team:123\n"
        f"page_version: {version}\nsource_updated_at: '2026-01-0{version}T12:00:00Z'\n"
        f"status: current\n---\n# Payment policy\n\n{text}\n",
        encoding="utf-8",
    )


def test_latest_history_as_of_and_exact_old_references_survive_rebuild(settings_factory):
    settings = settings_factory()
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    write_page(settings.knowledge_dir, 1, "Legacy payment window was ten hours.")
    service.build_index()
    old_hit = service.search("payment window", top_k=1)[0]
    write_page(settings.knowledge_dir, 2, "Current payment window is two hours.")
    service.build_index(force=True, reuse_unchanged=True)
    tools = KnowledgeTools(service)

    latest = tools.search_versions(query="payment window")
    assert latest["results"][0]["source_version"] == "2"
    assert latest["results"][0]["is_latest"] is True
    historical = tools.search_versions(query="payment window", version_scope="history", top_k=5)
    assert {item["source_version"] for item in historical["results"]} == {"1", "2"}
    assert historical["results"][0]["is_latest"] is True
    earlier = tools.search_versions(
        query="payment window", version_scope="as_of", as_of="2026-01-01T23:00:00Z"
    )
    assert [item["source_version"] for item in earlier["results"]] == ["1"]
    assert earlier["coverage"]["complete"] is False
    assert service.get_chunk(old_hit.chunk_id).is_latest is False
    assert "ten hours" in service.get_document(old_hit.document_id).content
    assert tools.get_document(page_id="confluence:team:123")["source_version"] == "2"
    assert (
        tools.get_document(page_id="confluence:team:123", source_version="1")["is_latest"] is False
    )
    assert service.stats().document_count == 1
    assert len(service.store.list_documents(SearchFilters(), limit=10)) == 1

    # Deleting disposable index data does not destroy captured revisions or checkpoints.
    shutil.rmtree(settings.cache_dir)
    restarted = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    restarted.build_index(reuse_unchanged=True)
    assert len(restarted.history.list_versions("confluence:team:123")) == 2
    assert "ten hours" in restarted.get_chunk(old_hit.chunk_id).text


def test_visibility_blocks_current_history_and_exact_reads(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Sensitive payment policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    old = service.search("payment", top_k=1)[0]
    service.set_page_visibility("confluence:team:123", False)
    assert service.search("payment") == []
    assert service.search_versions("payment", filters=SearchFilters(version_scope="history")) == []
    assert service.history.list_versions("confluence:team:123") == []
    assert service.history.coverage("confluence:team:123")["revision_count"] == 0
    with pytest.raises(KeyError):
        service.get_chunk(old.chunk_id)
    with pytest.raises(KeyError):
        service.get_document(old.document_id)
    service.set_page_visibility("confluence:team:123", True)
    assert service.search("payment")


def test_legacy_baseline_does_not_invent_source_date(settings_factory):
    settings = settings_factory()
    settings.knowledge_dir.mkdir()
    (settings.knowledge_dir / "legacy.md").write_text("# Legacy\n\nOld policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    document = service.list_documents()[0]
    assert document.source_updated_at is None
    assert document.history_baseline is True
    assert service.history.read(document.page_id, as_of="2000-01-01T00:00:00Z") is None


class InterruptedProvider(HashEmbeddingProvider):
    def __init__(self, *, fail_call=None):
        super().__init__(64)
        self.calls = 0
        self.text_count = 0
        self.fail_call = fail_call

    def embed_documents(self, texts):
        self.calls += 1
        if self.calls == self.fail_call:
            raise RuntimeError("worker interrupted")
        self.text_count += len(texts)
        return super().embed_documents(texts)


def test_completed_embedding_batches_survive_interruption(settings_factory):
    settings = settings_factory(embedding_checkpoint_batch_size=2)
    settings.knowledge_dir.mkdir()
    for number in range(5):
        (settings.knowledge_dir / f"{number}.md").write_text(
            f"# Document {number}\n\nUnique content {number}."
        )
    failed_provider = InterruptedProvider(fail_call=2)
    failed = KnowledgeService(settings, provider=failed_provider)
    with pytest.raises(RuntimeError, match="interrupted"):
        failed.build_index(reuse_unchanged=True)
    assert failed_provider.text_count == 2
    resumed_provider = InterruptedProvider()
    resumed = KnowledgeService(settings, provider=resumed_provider)
    events = []
    resumed.build_index(reuse_unchanged=True, progress=events.append)
    assert resumed_provider.text_count == 3
    assert [event["completed"] for event in events if event["stage"] == "embedding"] == [0, 2, 4, 5]
    # Changing title metadata affects embedding text even if source paragraph is unchanged.
    assert resumed.stats().document_count == 5


def test_new_historical_backfill_is_indexed_in_bounded_batches(settings_factory):
    settings = settings_factory(embedding_checkpoint_batch_size=1)
    write_page(settings.knowledge_dir, 2, "Current transfer policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    current = service.list_documents()[0]
    historic = current.model_copy(
        update={
            "document_id": "backfill-one",
            "revision_id": "backfill-one",
            "source_version": "1",
            "source_updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            "content_hash": "earlier-hash",
            "content": "Earlier transfer policy has a special legacy exception.",
        }
    )
    service.archive_document(historic)
    assert service.history.coverage(identity=service.provider.cache_identity)["pending_chunks"] > 0
    service.build_index(force=True, reuse_unchanged=True)
    hits = service.search_versions(
        "special legacy exception", filters=SearchFilters(version_scope="history")
    )
    assert any(item.document_id == "backfill-one" for item in hits)
    assert service.history.coverage(identity=service.provider.cache_identity)["pending_chunks"] == 0


def test_invalid_version_query_is_rejected(settings_factory):
    tools = KnowledgeTools(KnowledgeService(settings_factory(), provider=HashEmbeddingProvider(64)))
    with pytest.raises(ValueError, match="as_of"):
        tools.search_versions(query="policy", version_scope="as_of")
    with pytest.raises(ValueError, match="updated_from"):
        tools.search_versions(query="policy", updated_from="2026-02-01", updated_to="2026-01-01")


def test_multiple_export_files_publish_one_latest_page(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Earlier policy.")
    (settings.knowledge_dir / "page.md").rename(settings.knowledge_dir / "old-export.md")
    write_page(settings.knowledge_dir, 2, "Latest policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    assert service.stats().document_count == 1
    assert service.list_documents()[0].source_version == "2"
    assert len(service.history.list_versions("confluence:team:123")) == 2


def test_attachment_snapshot_does_not_retroactively_change_as_of(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Original diagram has one node.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    path = settings.knowledge_dir / "page.md"
    raw = path.read_text().replace(
        "status: current", "status: current\nsnapshot_updated_at: '2026-01-03T12:00:00Z'"
    )
    path.write_text(raw.replace("one node", "three nodes"))
    service.build_index(force=True, reuse_unchanged=True)
    historic = service.read_page("confluence:team:123", as_of="2026-01-02T12:00:00Z")
    assert "one node" in historic.content
    current = service.read_page("confluence:team:123")
    assert current.source_version == "1"
    assert current.source_updated_at.day == 1
    assert current.revision_updated_at.day == 3


class HistoryMatchProvider(HashEmbeddingProvider):
    def embed_documents(self, texts):
        matrix = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            matrix[row, 0 if "legacy needle" in text else 1] = 1
        return matrix

    def embed_query(self, text):
        return self.embed_documents([text])[0]


def test_history_top_one_keeps_old_match_and_points_to_current_revision(settings_factory):
    settings = settings_factory()
    settings.knowledge_dir.mkdir()
    for number in range(1, 27):
        stamp = (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=number)).isoformat()
        body = "legacy needle retained" if number < 26 else "Current replacement policy."
        (settings.knowledge_dir / f"page-{number}.md").write_text(
            "---\nsource_id: confluence:team:123\npage_id: confluence:team:123\n"
            f"page_version: {number}\nsource_updated_at: '{stamp}'\n"
            f"status: current\n---\n# Policy\n\n{body}\n"
        )
    service = KnowledgeService(settings, provider=HistoryMatchProvider(64))
    service.build_index()
    tools = KnowledgeTools(service)
    result = tools.search_versions(query="legacy needle", version_scope="history", top_k=1)
    assert result["result_count"] == 1
    historical = result["results"][0]
    assert historical["is_latest"] is False
    current = historical["latest_revision"]
    assert current["source_version"] == "26"
    assert "Current replacement" in tools.get_document(current["document_id"])["content"]
    service.set_page_visibility("confluence:team:123", False)
    assert service.history.latest_references(["confluence:team:123"]) == {}


def test_existing_cache_is_archived_before_changed_model_rebuild(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Former policy retained from pre-history cache.")
    first = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    first.build_index()
    old_hit = first.search("Former policy", top_k=1)[0]
    # Model the installed pre-history version: a cache exists without a durable archive.
    shutil.rmtree(settings.history_dir)
    write_page(settings.knowledge_dir, 2, "Current policy uses a new embedding configuration.")
    changed = KnowledgeService(settings, provider=HashEmbeddingProvider(128))
    changed.build_index()
    assert "Former policy" in changed.get_chunk(old_hit.chunk_id).text
    assert len(changed.history.list_versions("confluence:team:123")) == 2
    assert changed.history.coverage(identity=changed.provider.cache_identity)["pending_chunks"] == 0


def test_loading_pre_history_cache_migrates_without_reembedding(settings_factory, caplog):
    settings = settings_factory(auto_index=False)
    write_page(settings.knowledge_dir, 1, "Original policy cached before migration.")
    first = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    first.build_index()
    shutil.rmtree(settings.history_dir)
    provider = InterruptedProvider(fail_call=1)
    loaded = KnowledgeService(settings, provider=provider)
    with caplog.at_level("INFO", logger="corporate_kb.service"):
        loaded.load_cached_index()
    assert len(loaded.history.list_versions("confluence:team:123")) == 1
    assert loaded.history.coverage(identity=provider.cache_identity)["pending_chunks"] == 0
    assert provider.calls == 0
    assert "History migration: documents 1/1" in caplog.text
    assert "History migration: chunks" in caplog.text
    assert "Reusing cached vectors without model inference" in caplog.text
    assert "Activating current document revisions" in caplog.text

    # A following process must use the completed archive without a second import.
    restarted = KnowledgeService(settings, provider=provider)
    caplog.clear()
    with caplog.at_level("INFO", logger="corporate_kb.service"):
        restarted.load_cached_index()
    assert "import skipped" in caplog.text
    assert "Importing cached snapshot" not in caplog.text
    assert provider.calls == 0
