"""NumPy-backed knowledge store held entirely in process memory."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from corporate_kb.models import Chunk, Document, SearchFilters, SearchResult


class InMemoryKnowledgeStore:
    def __init__(self) -> None:
        self._documents: dict[str, Document] = {}
        self._chunks: list[Chunk] = []
        self._chunk_index: dict[str, int] = {}
        self._embeddings = np.empty((0, 0), dtype=np.float32)

    @property
    def document_count(self) -> int:
        return len(self._documents)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def embedding_dimension(self) -> int:
        return self._embeddings.shape[1] if self._embeddings.ndim == 2 else 0

    def replace_index(
        self,
        documents: list[Document],
        chunks: list[Chunk],
        embeddings: NDArray[np.float32],
    ) -> None:
        matrix = np.asarray(embeddings, dtype=np.float32)
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise ValueError(f"Embeddings shape {matrix.shape} does not match {len(chunks)} chunks")
        if not np.isfinite(matrix).all():
            raise ValueError("Embeddings contain NaN or infinite values")
        if len({document.document_id for document in documents}) != len(documents):
            raise ValueError("Duplicate document_id in replacement index")
        if len({chunk.chunk_id for chunk in chunks}) != len(chunks):
            raise ValueError("Duplicate chunk_id in replacement index")
        document_ids = {document.document_id for document in documents}
        if any(chunk.document_id not in document_ids for chunk in chunks):
            raise ValueError("Chunk references an unknown document")

        self._documents = {document.document_id: document for document in documents}
        self._chunks = list(chunks)
        self._chunk_index = {chunk.chunk_id: index for index, chunk in enumerate(chunks)}
        self._embeddings = matrix.copy()

    def search(
        self,
        query_vector: NDArray[np.float32],
        *,
        top_k: int,
        min_score: float | None,
        filters: SearchFilters,
    ) -> list[SearchResult]:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        if min_score is not None and not math.isfinite(min_score):
            raise ValueError("min_score must be finite")
        query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
        if query.shape != (self.embedding_dimension,):
            raise ValueError(
                f"Query embedding dimension {query.shape[0]} does not match index dimension "
                f"{self.embedding_dimension}"
            )
        if not np.isfinite(query).all():
            raise ValueError("Query embedding contains NaN or infinite values")
        if not self._chunks:
            return []

        candidate_indices = [
            index for index, chunk in enumerate(self._chunks) if self._matches(chunk, filters)
        ]
        if not candidate_indices:
            return []
        index_array = np.asarray(candidate_indices, dtype=np.intp)
        scores = self._embeddings[index_array] @ query
        ranked = sorted(
            zip(candidate_indices, scores.tolist(), strict=True),
            key=lambda item: (-item[1], self._chunks[item[0]].chunk_id),
        )
        selected = [item for item in ranked if min_score is None or item[1] >= min_score][:top_k]
        results: list[SearchResult] = []
        for rank, (chunk_index, score) in enumerate(selected, start=1):
            if not math.isfinite(score):
                raise ValueError("Cosine similarity produced a non-finite score")
            chunk = self._chunks[chunk_index]
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
                )
            )
        return results

    def get_document(self, document_id: str) -> Document | None:
        return self._documents.get(document_id)

    def list_documents(self, filters: SearchFilters, *, limit: int) -> list[Document]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        documents = [
            document
            for document in self._documents.values()
            if self._matches_document(document, filters)
        ]
        return sorted(documents, key=lambda item: (item.source_path, item.document_id))[:limit]

    @staticmethod
    def _value_matches(actual: object, expected: str) -> bool:
        if isinstance(actual, str):
            return actual == expected
        if isinstance(actual, list):
            return expected in {str(value) for value in actual}
        return False

    @classmethod
    def _matches(cls, chunk: Chunk, filters: SearchFilters) -> bool:
        return all(
            cls._value_matches(
                chunk.source_path.rsplit(".", 1)[-1]
                if key == "source_type"
                else chunk.metadata.get(key),
                expected,
            )
            if key == "source_type" and "source_type" not in chunk.metadata
            else cls._value_matches(chunk.metadata.get(key), expected)
            for key, expected in filters.active().items()
        )

    @classmethod
    def _matches_document(cls, document: Document, filters: SearchFilters) -> bool:
        return all(
            cls._value_matches(
                document.source_type if key == "source_type" else document.metadata.get(key),
                expected,
            )
            for key, expected in filters.active().items()
        )
