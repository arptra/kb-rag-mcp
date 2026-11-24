"""NumPy-backed knowledge store held entirely in process memory."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from corporate_kb.embeddings.base import matrix_is_finite
from corporate_kb.history import utc_datetime, version_fields
from corporate_kb.models import Chunk, Document, SearchFilters, SearchResult
from corporate_kb.stores.fulltext import ChunkTextIndex, fuse_ranks


@dataclass(frozen=True)
class _Snapshot:
    documents: dict[str, Document]
    chunks: list[Chunk]
    chunk_index: dict[str, int]
    embeddings: NDArray[np.float32]
    text_index: ChunkTextIndex


class InMemoryKnowledgeStore:
    def __init__(self) -> None:
        self._snapshot = _Snapshot({}, [], {}, np.empty((0, 0), dtype=np.float32), ChunkTextIndex())

    @property
    def document_count(self) -> int:
        return len(self._snapshot.documents)

    @property
    def chunk_count(self) -> int:
        return len(self._snapshot.chunks)

    @property
    def embedding_dimension(self) -> int:
        return int(self._snapshot.embeddings.shape[1])

    def replace_index(
        self,
        documents: list[Document],
        chunks: list[Chunk],
        embeddings: NDArray[np.float32],
    ) -> None:
        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise ValueError(f"Embeddings shape {matrix.shape} does not match {len(chunks)} chunks")
        if not matrix_is_finite(matrix):
            raise ValueError("Embeddings contain NaN or infinite values")
        if len({document.document_id for document in documents}) != len(documents):
            raise ValueError("Duplicate document_id in replacement index")
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("Duplicate chunk_id in replacement index")
        document_ids = {document.document_id for document in documents}
        if any(chunk.document_id not in document_ids for chunk in chunks):
            raise ValueError("Chunk references an unknown document")

        # Publish all lookup structures together. An in-flight search can finish on
        # its original snapshot while the replacement builds its own FTS postings.
        self._snapshot = _Snapshot(
            {document.document_id: document for document in documents}, list(chunks),
            {chunk.chunk_id: i for i, chunk in enumerate(chunks)}, matrix, ChunkTextIndex(),
        )

    def search(
        self,
        query_vector: NDArray[np.float32],
        *,
        top_k: int,
        min_score: float | None,
        filters: SearchFilters,
        text: str | None = None,
        exclude: Callable[[Chunk], bool] | None = None,
    ) -> list[SearchResult]:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        if min_score is not None and not math.isfinite(min_score):
            raise ValueError("min_score must be finite")
        snapshot = self._snapshot
        chunks = snapshot.chunks
        dimension = snapshot.embeddings.shape[1]
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if query.shape != (dimension,):
            raise ValueError(
                f"Query embedding dimension {query.shape[0]} does not match index dimension "
                f"{dimension}"
            )
        if not np.isfinite(query).all():
            raise ValueError("Query embedding contains NaN or infinite values")
        if not chunks:
            return []

        active_filters = filters.active()
        excluded_pages = set(filters.excluded_page_ids)
        if (
            exclude is not None
            or active_filters
            or excluded_pages
            or filters.page_id
            or filters.updated_from
            or filters.updated_to
            or filters.version_scope == "latest"
        ):
            candidate_indices = np.asarray(
                [
                    index
                    for index, chunk in enumerate(chunks)
                    if self._matches(chunk, filters, active_filters, excluded_pages)
                    and (exclude is None or not exclude(chunk))
                ],
                dtype=np.intp,
            )
        else:
            candidate_indices = np.arange(len(chunks), dtype=np.intp)
        if candidate_indices.size == 0:
            return []

        # With an all-current index the filter commonly selects every row.
        # Fancy indexing would copy the entire embedding matrix on each question.
        matrix = snapshot.embeddings
        if candidate_indices.size != matrix.shape[0]:
            matrix = matrix[candidate_indices]
        scores = matrix @ query
        eligible_positions = np.arange(scores.shape[0], dtype=np.intp)
        if min_score is not None:
            eligible_positions = np.flatnonzero(scores >= min_score).astype(np.intp, copy=False)
        if eligible_positions.size == 0:
            return []

        lexical = []
        if text:
            eligible = np.zeros(len(chunks), dtype=np.bool_)
            eligible[candidate_indices[eligible_positions]] = True
            lexical = snapshot.text_index.search(text, chunks, eligible, top_k)

        if eligible_positions.size > top_k:
            top_positions = np.argpartition(-scores[eligible_positions], top_k - 1)[:top_k]
            eligible_positions = eligible_positions[top_positions]

        ranked = [
            (
                int(candidate_indices[position]),
                float(scores[position]),
            )
            for position in eligible_positions
        ]
        ranked.sort(key=lambda item: (-item[1], chunks[item[0]].chunk_id))
        selected = ranked
        if lexical:
            order = fuse_ranks([index for index, _ in ranked], lexical)
            selected = [(i, float(scores[np.searchsorted(candidate_indices, i)]))
                        for i in order[:top_k]]
        results: list[SearchResult] = []
        for rank, (chunk_index, score) in enumerate(selected, start=1):
            if not math.isfinite(score):
                raise ValueError("Cosine similarity produced a non-finite score")
            chunk = chunks[chunk_index]
            results.append(
                SearchResult(
                    rank=rank,
                    score=float(score),
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    title=chunk.title,
                    heading_path=chunk.heading_path,
                    text=chunk.text,
                    source_path=chunk.source_path,
                    source_url=chunk.source_url,
                    metadata=dict(chunk.metadata),
                    **version_fields(chunk),
                )
            )
        return results

    def get_document(self, document_id: str) -> Document | None:
        return self._snapshot.documents.get(document_id)

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        snapshot = self._snapshot
        index = snapshot.chunk_index.get(chunk_id)
        return snapshot.chunks[index] if index is not None else None

    def list_documents(self, filters: SearchFilters, *, limit: int) -> list[Document]:
        documents, _total = self.browse_documents(
            filters,
            query="",
            offset=0,
            limit=limit,
        )
        return documents

    def browse_documents(
        self,
        filters: SearchFilters,
        *,
        query: str,
        offset: int,
        limit: int,
    ) -> tuple[list[Document], int]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        needle = query.strip().casefold()
        active_filters = filters.active()
        excluded_pages = set(filters.excluded_page_ids)
        documents = [
            document
            for document in self._snapshot.documents.values()
            if self._matches_document(document, filters, active_filters, excluded_pages)
            and (
                not needle
                or needle in document.title.casefold()
                or needle in document.source_path.casefold()
                or needle in document.source_id.casefold()
            )
        ]
        ordered = sorted(documents, key=lambda item: (item.source_path, item.document_id))
        return ordered[offset : offset + limit], len(ordered)

    @staticmethod
    def _value_matches(actual: object, expected: str) -> bool:
        if isinstance(actual, str):
            return actual == expected
        if isinstance(actual, list):
            return expected in {str(value) for value in actual}
        return False

    @classmethod
    def _matches(
        cls, chunk: Chunk, filters: SearchFilters, active: dict[str, str], excluded: set[str],
    ) -> bool:
        if not cls._matches_version(chunk, filters, excluded):
            return False
        return all(
            cls._value_matches(
                chunk.source_path.rsplit(".", 1)[-1]
                if key == "source_type"
                else chunk.metadata.get(key),
                expected,
            )
            if key == "source_type" and "source_type" not in chunk.metadata
            else cls._value_matches(chunk.metadata.get(key), expected)
            for key, expected in active.items()
        )

    @classmethod
    def _matches_document(
        cls, document: Document, filters: SearchFilters, active: dict[str, str], excluded: set[str],
    ) -> bool:
        if not cls._matches_version(document, filters, excluded):
            return False
        return all(
            cls._value_matches(
                document.source_type if key == "source_type" else document.metadata.get(key),
                expected,
            )
            for key, expected in active.items()
        )

    @staticmethod
    def _matches_version(
        item: Chunk | Document, filters: SearchFilters, excluded: set[str],
    ) -> bool:
        if item.page_id in excluded:
            return False
        if filters.version_scope == "latest" and not item.is_latest:
            return False
        if filters.page_id and item.page_id != filters.page_id:
            return False
        if not (filters.updated_from or filters.updated_to):
            return True
        updated = utc_datetime(item.source_updated_at)
        if filters.updated_from and (
            updated is None or updated < utc_datetime(filters.updated_from)
        ):
            return False
        return not (
            filters.updated_to and (updated is None or updated > utc_datetime(filters.updated_to))
        )
