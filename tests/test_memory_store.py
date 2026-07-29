from datetime import UTC, datetime

import numpy as np
import pytest

from corporate_kb.models import Chunk, Document, SearchFilters
from corporate_kb.stores.memory import InMemoryKnowledgeStore


def document(document_id: str, path: str, **metadata: object) -> Document:
    return Document(
        document_id=document_id,
        title=document_id,
        source_path=path,
        source_type="markdown",
        source_id=path,
        content="content",
        content_hash=f"hash-{document_id}",
        metadata=dict(metadata),
        loaded_at=datetime.now(UTC),
    )


def chunk(document_item: Document, chunk_id: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        document_id=document_item.document_id,
        chunk_index=0,
        title=document_item.title,
        heading_path=document_item.title,
        text=f"text-{chunk_id}",
        embedding_text=f"embedding-{chunk_id}",
        token_count=1,
        source_path=document_item.source_path,
        metadata=document_item.metadata,
    )


def populated_store() -> InMemoryKnowledgeStore:
    first = document("first", "first.md", service="limits", status="current", tags=["a", "b"])
    second = document("second", "second.md", service="payments", status="obsolete", tags="b")
    store = InMemoryKnowledgeStore()
    store.replace_index(
        [first, second],
        [chunk(first, "c1"), chunk(second, "c2")],
        np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
    )
    return store


def test_cosine_ranking_top_k_and_min_score() -> None:
    store = populated_store()
    results = store.search(
        np.asarray([0.9, 0.1], dtype=np.float32),
        top_k=1,
        min_score=0.5,
        filters=SearchFilters(),
    )

    assert [result.document_id for result in results] == ["first"]
    assert results[0].rank == 1


def test_metadata_filters_support_strings_and_lists() -> None:
    store = populated_store()
    by_service = store.search(
        np.asarray([1.0, 1.0], dtype=np.float32),
        top_k=5,
        min_score=None,
        filters=SearchFilters(service="payments"),
    )
    documents = store.list_documents(SearchFilters(status="current"), limit=10)

    assert [result.document_id for result in by_service] == ["second"]
    assert [item.document_id for item in documents] == ["first"]
    assert store._value_matches(["a", "b"], "b")
    assert store._value_matches("b", "b")


def test_empty_store_and_wrong_dimension() -> None:
    store = InMemoryKnowledgeStore()
    store.replace_index([], [], np.empty((0, 2), dtype=np.float32))
    assert (
        store.search(
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=5,
            min_score=None,
            filters=SearchFilters(),
        )
        == []
    )
    with pytest.raises(ValueError, match="dimension"):
        store.search(
            np.asarray([1.0], dtype=np.float32),
            top_k=5,
            min_score=None,
            filters=SearchFilters(),
        )


def test_non_finite_vectors_are_rejected() -> None:
    store = populated_store()
    with pytest.raises(ValueError, match="NaN"):
        store.search(
            np.asarray([np.nan, 0.0], dtype=np.float32),
            top_k=1,
            min_score=None,
            filters=SearchFilters(),
        )
    with pytest.raises(ValueError, match="min_score"):
        store.search(
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=1,
            min_score=np.inf,
            filters=SearchFilters(),
        )
    with pytest.raises(ValueError, match="top_k"):
        store.search(
            np.asarray([1.0, 0.0], dtype=np.float32),
            top_k=0,
            min_score=None,
            filters=SearchFilters(),
        )
