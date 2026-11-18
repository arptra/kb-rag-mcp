"""Current-page serving and durable embedding checkpoints after history retirement."""

import shutil
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.models import SearchFilters
from corporate_kb.service import KnowledgeService


def write_page(root, version, text):
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.md").write_text(
        f"---\nsource_id: confluence:team:123\npage_id: confluence:team:123\n"
        f"page_version: {version}\nsource_updated_at: '2026-01-0{version}T12:00:00Z'\n"
        f"status: current\n---\n# Payment policy\n\n{text}\n",
        encoding="utf-8",
    )


def seed_legacy_archive(service):
    snapshot = service.cache.load_snapshot()
    assert snapshot is not None
    service.history.import_snapshot(
        snapshot.documents,
        snapshot.chunks,
        snapshot.embeddings,
        snapshot.manifest.embedding_cache_identity,
    )
    service.history.activate(snapshot.documents)


def test_old_selectors_always_return_current_without_exposing_archived_exact_ids(settings_factory):
    settings = settings_factory()
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    write_page(settings.knowledge_dir, 1, "Legacy payment window was ten hours.")
    service.build_index()
    old_hit = service.search("payment window", top_k=1)[0]
    seed_legacy_archive(service)
    write_page(settings.knowledge_dir, 2, "Current payment window is two hours.")
    service.build_index(force=True, reuse_unchanged=True)
    tools = KnowledgeTools(service)

    for selectors in (
        {},
        {"version_scope": "history"},
        {"version_scope": "as_of", "as_of": "2026-01-01T23:00:00Z"},
    ):
        result = tools.search_versions(query="payment window", **selectors)
        assert result["version_scope"] == "latest"
        assert result["as_of"] is None
        assert {hit["source_version"] for hit in result["results"]} == {"2"}
        assert all(hit["is_latest"] for hit in result["results"])
    for selectors in ({}, {"source_version": "1"}, {"as_of": "2000-01-01"}):
        assert (
            tools.get_document(page_id="confluence:team:123", **selectors)["source_version"] == "2"
        )
    with pytest.raises(KeyError):
        service.get_chunk(old_hit.chunk_id)
    with pytest.raises(KeyError):
        service.get_document(old_hit.document_id)
    # Old files are retained for backup compatibility, but new snapshots are not added.
    assert len(service.history.list_versions("confluence:team:123")) == 1
    assert service.stats().document_count == 1

    shutil.rmtree(settings.cache_dir)
    restarted = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    restarted.build_index(reuse_unchanged=True)
    assert restarted.read_page("confluence:team:123", source_version="1").source_version == "2"
    with pytest.raises(KeyError):
        restarted.get_document(old_hit.document_id)


def test_visibility_blocks_all_current_entry_points(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Sensitive payment policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    hit = service.search("payment", top_k=1)[0]
    service.set_page_visibility("confluence:team:123", False)
    assert service.search("payment") == []
    assert service.search_versions("payment", filters=SearchFilters(version_scope="history")) == []
    assert service.list_documents() == []
    with pytest.raises(KeyError):
        service.get_chunk(hit.chunk_id)
    with pytest.raises(KeyError):
        service.get_document(hit.document_id)
    with pytest.raises(KeyError):
        service.read_page("confluence:team:123", as_of="2000-01-01")
    service.set_page_visibility("confluence:team:123", True)
    assert service.search("payment")


def test_document_without_source_date_does_not_invent_one(settings_factory):
    settings = settings_factory()
    settings.knowledge_dir.mkdir()
    (settings.knowledge_dir / "legacy.md").write_text("# Legacy\n\nCurrent policy.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    document = service.list_documents()[0]
    assert document.source_updated_at is None
    assert document.history_baseline is True
    assert (
        service.read_page(document.page_id, as_of="2000-01-01").document_id == document.document_id
    )
    with service.history._db() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] > 0


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
    assert resumed.stats().document_count == 5


def test_old_pending_history_is_not_embedded_or_used_to_invalidate_current_cache(settings_factory):
    settings = settings_factory(embedding_checkpoint_batch_size=1)
    write_page(settings.knowledge_dir, 2, "Current transfer policy.")
    provider = InterruptedProvider()
    service = KnowledgeService(settings, provider=provider)
    service.build_index()
    current = service.list_documents()[0]
    historic = current.model_copy(
        update={
            "document_id": "legacy-backfill",
            "revision_id": "legacy-backfill",
            "source_version": "1",
            "source_updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            "content_hash": "earlier-hash",
            "content": "Earlier special legacy exception.",
        }
    )
    service.history.archive_document(historic)
    service.history.archive_chunks(service.chunker.chunk(historic))
    assert service.history.has_pending_chunks(provider.cache_identity)
    calls_before = provider.calls
    assert service.build_index().loaded_from_cache
    events = []
    service.build_index(force=True, reuse_unchanged=True, progress=events.append)
    assert provider.calls == calls_before
    assert not any(event["stage"] == "history_embedding" for event in events)
    assert {
        hit.source_version
        for hit in service.search_versions(
            "legacy exception", filters=SearchFilters(version_scope="history")
        )
    } == {"2"}
    # The compatibility hook no longer captures document text or chunks.
    service.archive_document(historic.model_copy(update={"document_id": "ignored-backfill"}))
    assert service.history.get_document("ignored-backfill") is None


def test_current_updated_date_filter_validation_remains(settings_factory):
    tools = KnowledgeTools(KnowledgeService(settings_factory(), provider=HashEmbeddingProvider(64)))
    with pytest.raises(ValueError, match="updated_from"):
        tools.search_versions(query="policy", updated_from="2026-02-01", updated_to="2026-01-01")


def test_multiple_export_files_only_embed_one_latest_page(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Earlier policy.")
    (settings.knowledge_dir / "page.md").rename(settings.knowledge_dir / "old-export.md")
    write_page(settings.knowledge_dir, 2, "Latest policy.")
    provider = InterruptedProvider()
    service = KnowledgeService(settings, provider=provider)
    service.build_index()
    assert service.stats().document_count == 1
    assert service.list_documents()[0].source_version == "2"
    assert provider.text_count == 1
    assert service.history.list_versions("confluence:team:123") == []


def test_attachment_update_returns_current_content_even_with_old_date_selector(settings_factory):
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
    current = service.read_page("confluence:team:123", as_of="2026-01-02T12:00:00Z")
    assert "three nodes" in current.content
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


def test_old_relevant_text_cannot_displace_the_current_page(settings_factory):
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
    result = KnowledgeTools(service).search_versions(query="legacy needle", version_scope="history")
    assert result["result_count"] == 1
    assert result["results"][0]["source_version"] == "26"
    assert result["results"][0]["is_latest"] is True


def test_changed_model_rebuild_does_not_archive_the_previous_cache(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Former policy retained from pre-history cache.")
    first = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    first.build_index()
    old_hit = first.search("Former policy", top_k=1)[0]
    shutil.rmtree(settings.history_dir)
    write_page(settings.knowledge_dir, 2, "Current policy uses a new embedding configuration.")
    changed = KnowledgeService(settings, provider=HashEmbeddingProvider(128))
    changed.build_index()
    with pytest.raises(KeyError):
        changed.get_chunk(old_hit.chunk_id)
    assert changed.history.list_versions("confluence:team:123") == []


def test_loading_prepared_cache_skips_archive_migration_and_inference(settings_factory, caplog):
    settings = settings_factory(auto_index=False)
    write_page(settings.knowledge_dir, 1, "Original policy cached before migration.")
    first = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    first.build_index()
    shutil.rmtree(settings.history_dir)
    provider = InterruptedProvider(fail_call=1)
    for _ in range(2):
        loaded = KnowledgeService(settings, provider=provider)
        with caplog.at_level("INFO", logger="corporate_kb.service"):
            loaded.load_cached_index()
        assert not loaded.history.path.exists()
        assert provider.calls == 0
        assert loaded.stats().document_count == 1
    assert "History migration" not in caplog.text
    assert "Importing cached snapshot" not in caplog.text


def test_removed_page_never_falls_back_to_legacy_archive(settings_factory):
    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Removed page.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    old = service.list_documents()[0]
    seed_legacy_archive(service)
    (settings.knowledge_dir / "page.md").unlink()
    service.build_index(force=True)
    assert service.list_documents() == []
    with pytest.raises(KeyError):
        service.read_page(old.page_id)
    with pytest.raises(KeyError):
        service.get_document(old.document_id)


def test_old_cache_with_duplicate_page_versions_serves_only_latest(settings_factory):
    settings = settings_factory(auto_index=False)
    write_page(settings.knowledge_dir, 1, "Obsolete duplicate export.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    old = service.cache.load_snapshot()
    assert old is not None
    old_vectors = old.embeddings.copy()
    write_page(settings.knowledge_dir, 2, "Current duplicate export.")
    service.build_index(force=True)
    new = service.cache.load_snapshot()
    assert new is not None
    combined_vectors = np.vstack([old_vectors, new.embeddings])
    combined_docs, combined_chunks = old.documents + new.documents, old.chunks + new.chunks
    service.cache.save(
        documents=combined_docs,
        chunks=combined_chunks,
        embeddings=combined_vectors,
        manifest=new.manifest.model_copy(
            update={
                "document_count": len(combined_docs),
                "chunk_count": len(combined_chunks),
                "embeddings_shape": list(combined_vectors.shape),
            }
        ),
    )
    loaded = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    stats = loaded.load_cached_index()
    assert stats.document_count == 1
    assert stats.chunk_count == len(new.chunks)
    assert {hit.source_version for hit in loaded.search("duplicate export")} == {"2"}
    with pytest.raises(KeyError):
        loaded.get_document(old.documents[0].document_id)
    with pytest.raises(KeyError):
        loaded.get_chunk(old.chunks[0].chunk_id)


def test_legacy_prepared_vectors_are_reused_without_document_archival(settings_factory):
    settings = settings_factory(embedding_checkpoint_batch_size=2)
    settings.knowledge_dir.mkdir()
    for number in range(3):
        (settings.knowledge_dir / f"{number}.md").write_text(
            f"# Page {number}\n\nContent {number}."
        )
    first = KnowledgeService(settings, provider=InterruptedProvider())
    first.build_index()
    shutil.rmtree(settings.history_dir)
    (settings.knowledge_dir / "new.md").write_text("# New page\n\nNew content.")
    provider = InterruptedProvider()
    updated = KnowledgeService(settings, provider=provider)
    updated.build_index(reuse_unchanged=True)
    assert provider.text_count == 1
    assert updated.stats().document_count == 4
    with updated.history._db() as db:
        assert db.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
