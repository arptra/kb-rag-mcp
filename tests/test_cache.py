from datetime import UTC, datetime
from pathlib import Path

import numpy as np

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
    assert sorted(path.name for path in manager.cache_dir.iterdir()) == [
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
