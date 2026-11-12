from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from corporate_kb.cache.manager import CACHE_SCHEMA_VERSION, CacheManager, CacheManifest
from corporate_kb.models import Chunk, Document


def cache_data() -> tuple[list[Document], list[Chunk], np.ndarray, CacheManifest]:
    document = Document(
        document_id="doc",
        title="Doc",
        source_path="doc.md",
        source_type="markdown",
        source_id="doc.md",
        content="Text",
        content_hash="hash",
        metadata={"status": "current"},
        loaded_at=datetime.now(UTC),
    )
    chunk = Chunk(
        chunk_id="chunk",
        document_id="doc",
        chunk_index=0,
        title="Doc",
        heading_path="Doc",
        text="Text",
        embedding_text="Document: Doc\n\nText",
        token_count=1,
        source_path="doc.md",
        metadata={"status": "current"},
    )
    embeddings = np.asarray([[1.0, 0.0]], dtype=np.float32)
    manifest = CacheManifest(
        cache_schema_version=CACHE_SCHEMA_VERSION,
        created_at=datetime.now(UTC),
        embedding_provider="hash",
        embedding_model="hash-v1",
        embedding_dimension=2,
        embedding_cache_identity="identity",
        query_instruction="instruction",
        chunk_size=10,
        chunk_overlap=2,
        chunk_hard_max=12,
        knowledge_hash="knowledge",
        document_count=1,
        chunk_count=1,
        embeddings_shape=[1, 2],
    )
    return [document], [chunk], embeddings, manifest


def load(manager: CacheManager, **overrides: object):
    arguments: dict[str, object] = {
        "knowledge_hash": "knowledge",
        "embedding_cache_identity": "identity",
        "chunking": {"chunk_size": 10, "chunk_overlap": 2, "chunk_hard_max": 12},
    }
    arguments.update(overrides)
    return manager.load(**arguments)  # type: ignore[arg-type]


def test_cache_round_trip_is_pickle_free(tmp_path: Path) -> None:
    manager = CacheManager(tmp_path / "cache")
    documents, chunks, embeddings, manifest = cache_data()
    manager.save(documents=documents, chunks=chunks, embeddings=embeddings, manifest=manifest)

    cached = load(manager)

    assert cached is not None
    np.testing.assert_array_equal(cached.embeddings, embeddings)
    assert cached.documents[0].document_id == "doc"
    assert sorted(path.name for path in manager.snapshot_directory().iterdir()) == [
        "chunks.json",
        "documents.json",
        "embeddings.npy",
        "manifest.json",
    ]
    assert not list(manager.cache_dir.glob("*.pickle"))
    assert not list(manager.cache_dir.glob("*.pkl"))


def test_cache_rejects_knowledge_and_model_identity_mismatches(tmp_path: Path) -> None:
    manager = CacheManager(tmp_path / "cache")
    documents, chunks, embeddings, manifest = cache_data()
    manager.save(documents=documents, chunks=chunks, embeddings=embeddings, manifest=manifest)

    assert load(manager, knowledge_hash="changed") is None
    assert load(manager, embedding_cache_identity="other") is None


def test_cache_rejects_corrupt_manifest(tmp_path: Path) -> None:
    manager = CacheManager(tmp_path / "cache")
    manager.cache_dir.mkdir(parents=True)
    manager.manifest_path.write_text("{broken", encoding="utf-8")

    assert load(manager) is None


def test_cache_rejects_wrong_embedding_shape(tmp_path: Path) -> None:
    manager = CacheManager(tmp_path / "cache")
    documents, chunks, embeddings, manifest = cache_data()
    manager.save(documents=documents, chunks=chunks, embeddings=embeddings, manifest=manifest)
    with manager.embeddings_path.open("wb") as handle:
        np.save(handle, np.ones((2, 2), dtype=np.float32), allow_pickle=False)

    assert load(manager) is None


def test_failed_generation_publication_keeps_previous_snapshot(tmp_path: Path, monkeypatch) -> None:
    manager = CacheManager(tmp_path / "cache")
    documents, chunks, embeddings, manifest = cache_data()
    manager.save(documents=documents, chunks=chunks, embeddings=embeddings, manifest=manifest)
    original = manager.snapshot_directory()

    def fail(_generation):
        raise OSError("interrupted before pointer swap")

    monkeypatch.setattr(manager, "publish_generation", fail)
    with pytest.raises(OSError):
        manager.save(
            documents=documents, chunks=chunks, embeddings=embeddings * 2, manifest=manifest
        )
    assert CacheManager(manager.cache_dir).snapshot_directory() == original
    np.testing.assert_array_equal(load(CacheManager(manager.cache_dir)).embeddings, embeddings)


def test_legacy_flat_cache_loads_as_a_baseline_then_migrates(tmp_path: Path) -> None:
    manager = CacheManager(tmp_path / "cache")
    documents, chunks, embeddings, manifest = cache_data()
    manager._save_flat(
        documents=documents,
        chunks=chunks,
        embeddings=embeddings,
        manifest=manifest.model_copy(update={"cache_schema_version": 1}),
    )
    restored = load(manager)
    assert restored.documents[0].history_baseline
    assert restored.documents[0].source_updated_at is None
    manager.save(
        documents=restored.documents,
        chunks=restored.chunks,
        embeddings=restored.embeddings,
        manifest=manifest,
    )
    assert manager.snapshot_directory() != manager.cache_dir
