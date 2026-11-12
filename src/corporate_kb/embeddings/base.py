"""Embedding provider interface."""

from typing import Protocol

import numpy as np
from numpy.typing import NDArray


def matrix_is_finite(matrix: NDArray[np.float32]) -> bool:
    """Validate mmap-backed matrices without allocating a full-size boolean copy."""
    return all(
        np.isfinite(matrix[offset : offset + 4096]).all() for offset in range(0, len(matrix), 4096)
    )


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def cache_identity(self) -> str: ...

    def embed_documents(self, texts: list[str]) -> NDArray[np.float32]: ...

    def embed_query(self, query: str) -> NDArray[np.float32]: ...
