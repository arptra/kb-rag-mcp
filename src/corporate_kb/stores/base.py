"""Replaceable knowledge store contract."""

from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from corporate_kb.models import Chunk, Document, SearchFilters, SearchResult


class KnowledgeStore(Protocol):
    def replace_index(
        self,
        documents: list[Document],
        chunks: list[Chunk],
        embeddings: NDArray[np.float32],
    ) -> None: ...

    def search(
        self,
        query_vector: NDArray[np.float32],
        *,
        top_k: int,
        min_score: float | None,
        filters: SearchFilters,
    ) -> list[SearchResult]: ...

    def get_document(self, document_id: str) -> Document | None: ...

    def list_documents(self, filters: SearchFilters, *, limit: int) -> list[Document]: ...

    @property
    def document_count(self) -> int: ...

    @property
    def chunk_count(self) -> int: ...

    @property
    def embedding_dimension(self) -> int: ...
