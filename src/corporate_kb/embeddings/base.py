"""Embedding provider interface."""

from typing import Protocol

import numpy as np
from numpy.typing import NDArray


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
