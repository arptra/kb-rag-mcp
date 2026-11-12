import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import numpy as np
import pytest

from corporate_kb.history import HistoryStore, embedding_key, normalize_document, version_fields
from corporate_kb.models import Chunk, Document


def snapshot(count=7):
    documents = [
        Document(
            document_id=f"document-{number}",
            title=f"Document {number}",
            source_path=f"{number}.md",
            source_type="markdown",
            source_id=f"page-{number}",
            content=f"Document content {number}",
            content_hash=f"content-{number}",
            loaded_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        for number in range(2)
    ]
    chunks = []
    for number in range(count):
        document = documents[number % 2]
        chunks.append(
            Chunk(
                chunk_id=f"chunk-{number}",
                document_id=document.document_id,
                chunk_index=number,
                title=document.title,
                heading_path="Section",
                text=f"Chunk content {number}",
                embedding_text=f"Document context and chunk content {number}",
                token_count=8,
                source_path=document.source_path,
                **version_fields(normalize_document(document)),
            )
        )
    vectors = np.arange(count * 4, dtype=np.float32).reshape(count, 4)
    return documents, chunks, vectors


def test_import_uses_one_connection_and_preserves_readonly_cached_vectors(tmp_path, monkeypatch):
    history = HistoryStore(tmp_path / "archive", "index")
    documents, chunks, vectors = snapshot(13)
    np.save(tmp_path / "cached.npy", vectors)
    cached = np.load(tmp_path / "cached.npy", mmap_mode="r")
    connect = sqlite3.connect
    connections = []
    statements = []

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    events = []
    history.import_snapshot(
        documents,
        chunks,
        cached,
        "cached-model",
        batch_size=3,
        progress=lambda *event: events.append(event),
    )
    assert len(connections) == 1
    assert np.array_equal(cached, vectors)
    assert not cached.flags.writeable
    assert [event[1] for event in events if event[0] == "chunks"] == [0, 3, 6, 9, 12, 13]
    assert history.read("page-0") is None  # Import does not publish a partial snapshot.
    history.activate(documents)
    assert history.read("page-0").document_id == "document-0"
    assert history.get_document("document-0").history_baseline is True
    archived = history.cached_embeddings("cached-model", [c.embedding_text for c in chunks], 4)
    for chunk, vector in zip(chunks, vectors, strict=True):
        assert np.array_equal(archived[embedding_key("cached-model", chunk.embedding_text)], vector)
    # Later lookups still use independent connections, without rerunning schema DDL.
    assert sum("CREATE TABLE IF NOT EXISTS revisions" in statement for statement in statements) == 1


def test_interrupted_import_resumes_without_replacing_completed_vectors(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot()

    def interrupt(phase, completed, total):
        if phase == "chunks" and completed == 4:
            raise RuntimeError("process interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        history.import_snapshot(
            documents, chunks, vectors, "cached-model", batch_size=2, progress=interrupt
        )
    assert history.contains_chunk_embeddings([c.chunk_id for c in chunks[:4]], "cached-model")
    assert not history.contains_chunk_embeddings([c.chunk_id for c in chunks], "cached-model")

    resumed = HistoryStore(tmp_path, "index")
    retry_vectors = vectors.copy()
    retry_vectors[:4] = -99
    resumed.import_snapshot(documents, chunks, retry_vectors, "cached-model", batch_size=2)
    assert resumed.contains_chunk_embeddings([c.chunk_id for c in chunks], "cached-model")
    archived = resumed.cached_embeddings("cached-model", [c.embedding_text for c in chunks], 4)
    for chunk, vector in zip(chunks, vectors, strict=True):
        assert np.array_equal(archived[embedding_key("cached-model", chunk.embedding_text)], vector)


def test_failed_chunk_batch_is_atomic_and_can_be_retried(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(4)
    invalid_chunks = [*chunks[:3], chunks[3].model_copy(update={"document_id": "missing"})]
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        history.import_snapshot(documents, invalid_chunks, vectors, "cached-model", batch_size=2)
    assert history.get_chunk(chunks[0].chunk_id) is not None
    assert history.get_chunk(chunks[2].chunk_id) is None
    assert history.cached_embeddings("cached-model", [chunks[2].embedding_text], 4) == {}
    history.import_snapshot(documents, chunks, vectors, "cached-model", batch_size=2)
    assert history.contains_chunk_embeddings([c.chunk_id for c in chunks], "cached-model")


def test_import_fills_missing_vectors_for_already_archived_chunks(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(3)
    for document in documents:
        history.archive_document(document)
    history.archive_chunks(chunks)
    history.save_embeddings("cached-model", chunks[:1], vectors[:1])
    history.import_snapshot(documents, chunks, vectors, "cached-model", batch_size=2)
    assert not history.has_pending_chunks("cached-model")
    assert history.coverage(identity="cached-model")["revision_count"] == 2


def test_import_preserves_existing_revision_payload_and_current_pointer(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(1)
    history.archive_document(documents[0], make_current=True)
    changed_metadata = documents[0].model_copy(update={"title": "Later title"})
    history.import_snapshot([changed_metadata, documents[1]], chunks, vectors, "cached-model")
    assert history.get_document(documents[0].document_id).title == documents[0].title
    assert history.read("page-0").is_latest is True
    conflicting_revision = documents[0].model_copy(update={"content_hash": "other-content"})
    with pytest.raises(ValueError, match="revision ID"):
        history.import_snapshot([conflicting_revision], [], np.empty((0, 4)), "cached-model")
    assert history.get_document(documents[0].document_id).content_hash == documents[0].content_hash


@pytest.mark.parametrize("kind", ["document", "chunk"])
def test_conflicting_duplicate_ids_roll_back_their_batch(tmp_path, kind):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(1)
    if kind == "document":
        documents.append(documents[0].model_copy(update={"content_hash": "different-content"}))
    else:
        chunks.append(chunks[0].model_copy(update={"text": "Different content"}))
        vectors = np.vstack([vectors, vectors])
    with pytest.raises(ValueError, match="ID cannot be reused"):
        history.import_snapshot(documents, chunks, vectors, "cached-model")
    if kind == "document":
        assert history.get_document("document-0") is None
    else:
        assert history.get_chunk("chunk-0") is None
        assert history.cached_embeddings("cached-model", [chunks[0].embedding_text], 4) == {}


def test_identical_duplicate_ids_are_idempotent_within_one_import(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(1)
    history.import_snapshot(
        [*documents, documents[0]],
        [*chunks, chunks[0]],
        np.vstack([vectors, vectors]),
        "cached-model",
    )
    assert history.coverage(identity="cached-model")["revision_count"] == 2
    assert not history.has_pending_chunks("cached-model")


def test_import_rejects_dimension_reuse_without_replacing_archive(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(1)
    history.import_snapshot(documents, chunks, vectors, "cached-model")
    with pytest.raises(ValueError, match="different dimension"):
        history.import_snapshot(documents, chunks, vectors[:, :2], "cached-model")
    archived = history.cached_embeddings("cached-model", [chunks[0].embedding_text], 4)
    assert np.array_equal(
        archived[embedding_key("cached-model", chunks[0].embedding_text)], vectors[0]
    )


def test_nonfinite_batch_does_not_lose_preceding_checkpoints(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = snapshot(4)
    invalid = vectors.copy()
    invalid[3, 0] = np.nan
    with pytest.raises(ValueError, match="Invalid embedding checkpoint"):
        history.import_snapshot(documents, chunks, invalid, "cached-model", batch_size=2)
    assert history.get_chunk("chunk-0") is not None
    assert history.get_chunk("chunk-2") is None
    history.import_snapshot(documents, chunks, vectors, "cached-model", batch_size=2)
    assert not history.has_pending_chunks("cached-model")


def test_concurrent_stores_initialize_and_migrate_a_legacy_archive(tmp_path):
    stores = [HistoryStore(tmp_path, "shared-index") for _ in range(4)]
    stores[0].root.mkdir(parents=True)
    with sqlite3.connect(stores[0].path) as connection:
        connection.execute(
            "CREATE TABLE revisions (document_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, "
            "source_version TEXT, source_updated_at TEXT, fetched_at TEXT NOT NULL, "
            "payload TEXT NOT NULL, is_latest INTEGER NOT NULL DEFAULT 0)"
        )
    barrier = Barrier(len(stores))

    def initialize(store):
        barrier.wait(timeout=10)
        return store.coverage()["revision_count"]

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        assert list(pool.map(initialize, stores)) == [0, 0, 0, 0]
    with sqlite3.connect(stores[0].path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(revisions)")}
    assert "revision_updated_at" in columns
