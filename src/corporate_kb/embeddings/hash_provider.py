"""Deterministic feature-hashing embeddings for tests and smoke checks."""

from __future__ import annotations

import hashlib
import re

import numpy as np
from numpy.typing import NDArray

_WORD = re.compile(r"\w+", re.UNICODE)


class HashEmbeddingProvider:
    """Offline embeddings; useful for pipeline verification, not semantic production search."""

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 1:
            raise ValueError("Hash embedding dimension must be positive")
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def provider_name(self) -> str:
        return "hash"

    @property
    def model_name(self) -> str:
        return "sha256-word-char-3gram-v1"

    @property
    def cache_identity(self) -> str:
        return f"hash:{self.model_name}:dimension={self.dimension}"

    def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        return np.vstack([self._embed(text) for text in texts]).astype(np.float32, copy=False)

    def embed_query(self, query: str) -> NDArray[np.float32]:
        return self._embed(query)

    def _embed(self, text: str) -> NDArray[np.float32]:
        vector = np.zeros(self.dimension, dtype=np.float32)
        words = _WORD.findall(text.lower())
        features = [f"w:{word}" for word in words]
        for word in words:
            padded = f"^{word}$"
            features.extend(
                f"c3:{padded[index : index + 3]}" for index in range(max(0, len(padded) - 2))
            )
        for feature in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector
