"""Checkpoint reuse remains bounded and preserves links in pre-existing archives."""

import sqlite3
from datetime import UTC, datetime

import numpy as np
import pytest

from corporate_kb.history import HistoryStore, embedding_key
from corporate_kb.models import Chunk, Document


def archive_snapshot(count):
    document = Document(
        document_id="document",
        title="Document",
        source_path="document.md",
        source_type="markdown",
        source_id="page",
        content="Document content",
        content_hash="content",
        loaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    chunks = [
        Chunk(
            chunk_id=f"chunk-{number}",
            document_id=document.document_id,
            chunk_index=number,
            title=document.title,
            heading_path="Section",
            text=f"Chunk {number}",
            embedding_text=f"Document context {number}",
            token_count=3,
            source_path=document.source_path,
        )
        for number in range(count)
    ]
    vectors = np.arange(count * 4, dtype=np.float32).reshape(count, 4)
    return [document], chunks, vectors


def test_existing_archive_gets_foreign_key_index_without_changing_links(tmp_path):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = archive_snapshot(12)
    history.import_snapshot(documents, chunks, vectors, "model")
    with sqlite3.connect(history.path) as db:
        db.execute("DROP INDEX chunk_embeddings_key")
        before = db.execute("SELECT * FROM chunk_embeddings ORDER BY chunk_id").fetchall()

    # Reopen an installation created before the index existed.
    reopened = HistoryStore(tmp_path, "index")
    assert len(reopened.cached_embeddings("model", [c.embedding_text for c in chunks], 4)) == 12
    with sqlite3.connect(history.path) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM chunk_embeddings WHERE embedding_key=?",
            (embedding_key("model", chunks[0].embedding_text),),
        ).fetchall()
        assert any("USING INDEX chunk_embeddings_key" in row[3] for row in plan)
        assert db.execute("SELECT * FROM chunk_embeddings ORDER BY chunk_id").fetchall() == before
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("link_chunks", [False, True])
def test_checkpoint_update_preserves_parent_rows_and_legacy_links(tmp_path, link_chunks):
    history = HistoryStore(tmp_path, "index")
    documents, chunks, vectors = archive_snapshot(4)
    history.import_snapshot(documents, chunks, vectors, "model")
    with sqlite3.connect(history.path) as db:
        before = db.execute("SELECT key,rowid FROM embeddings ORDER BY key").fetchall()
        links = db.execute("SELECT * FROM chunk_embeddings ORDER BY chunk_id").fetchall()

    updated = vectors + 100
    history.save_embeddings("model", chunks, updated, link_chunks=link_chunks)
    with sqlite3.connect(history.path) as db:
        # REPLACE deletes the referenced parent and changes its rowid. An update
        # must leave that identity intact and avoid checking every legacy link.
        assert db.execute("SELECT key,rowid FROM embeddings ORDER BY key").fetchall() == before
        assert db.execute("SELECT * FROM chunk_embeddings ORDER BY chunk_id").fetchall() == links
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    cached = history.cached_embeddings("model", [c.embedding_text for c in chunks], 4)
    for chunk, vector in zip(chunks, updated, strict=True):
        np.testing.assert_array_equal(cached[embedding_key("model", chunk.embedding_text)], vector)


def test_lookup_batches_deduplicates_and_skips_corrupt_vectors(tmp_path, monkeypatch):
    history = HistoryStore(tmp_path, "index")
    _, chunks, vectors = archive_snapshot(1003)
    history.save_embeddings("model", chunks, vectors, link_chunks=False)
    keys = [embedding_key("model", chunk.embedding_text) for chunk in chunks]
    with sqlite3.connect(history.path) as db:
        db.execute("UPDATE embeddings SET dimension=5 WHERE key=?", (keys[0],))
        db.execute(
            "UPDATE embeddings SET vector=? WHERE key=?",
            (np.array([np.nan] * 4, dtype=np.float32).tobytes(), keys[1]),
        )
        db.execute("UPDATE embeddings SET vector=? WHERE key=?", (b"bad", keys[2]))
        db.execute("UPDATE embeddings SET vector=? WHERE key=?", ("not a blob", keys[3]))
        db.execute(
            "UPDATE embeddings SET vector=? WHERE key=?",
            (np.zeros(3, dtype=np.float32).tobytes(), keys[4]),
        )
    statements = []
    connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        # Model older SQLite builds with the historical variable limit.
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    texts = [chunk.embedding_text for chunk in chunks]
    cached = history.cached_embeddings("model", texts + texts + ["absent"], 4)
    assert len(cached) == 998
    assert all(key not in cached for key in keys[:5])
    np.testing.assert_array_equal(cached[keys[-1]], vectors[-1])
    reads = [statement for statement in statements if "SELECT key,dimension,vector" in statement]
    assert len(reads) == 3
    assert all(" WHERE key IN (" in statement for statement in reads)


def test_empty_lookup_does_not_initialize_database(tmp_path):
    history = HistoryStore(tmp_path, "index")
    assert history.cached_embeddings("model", [], 4) == {}
    assert not history.path.exists()


def test_invalid_checkpoint_does_not_overwrite_reusable_vectors(tmp_path):
    history = HistoryStore(tmp_path, "index")
    _, chunks, vectors = archive_snapshot(2)
    history.save_embeddings("model", chunks, vectors, link_chunks=False)
    for invalid in (vectors[:1], np.full((2, 4), np.inf, dtype=np.float32)):
        with pytest.raises(ValueError, match="Invalid embedding checkpoint"):
            history.save_embeddings("model", chunks, invalid, link_chunks=False)
    cached = history.cached_embeddings("model", [c.embedding_text for c in chunks], 4)
    for chunk, vector in zip(chunks, vectors, strict=True):
        np.testing.assert_array_equal(cached[embedding_key("model", chunk.embedding_text)], vector)
