"""Deterministic local feature-hashing embeddings with no model dependency."""

from __future__ import annotations

import hashlib
import re

import numpy as np
from numpy.typing import NDArray

_WORD = re.compile(r"\w+", re.UNICODE)
_FEATURE_CACHE_SIZE = 65_536
_MAX_CACHED_FEATURE_LENGTH = 64


class HashEmbeddingProvider:
    """Offline lexical embeddings based on words and character trigrams."""

    def __init__(self, dimension: int = 256) -> None:
        if dimension < 1:
            raise ValueError("Hash embedding dimension must be positive")
        self._dimension = dimension
        # Keep this cache local to the provider and bounded in both entry count
        # and feature length. Once full, hashing uncached features avoids costly
        # eviction churn when indexing mostly unique identifiers.
        self._feature_codes: dict[str, int] = {}

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
        vectors = np.empty((len(texts), self.dimension), dtype=np.float32)
        for index, text in enumerate(texts):
            vectors[index] = self._embed(text)
        return vectors

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
        feature_codes = self._feature_codes
        codes: list[int] = []
        for feature in features:
            code = feature_codes.get(feature)
            if code is None:
                digest = hashlib.sha256(feature.encode("utf-8")).digest()
                # The sign and one-based bucket share one integer, reducing
                # cache memory while retaining the original SHA-256 mapping.
                bucket = int.from_bytes(digest[:8], "big") % self.dimension + 1
                code = bucket if digest[8] & 1 else -bucket
                if (
                    len(feature_codes) < _FEATURE_CACHE_SIZE
                    and len(feature) <= _MAX_CACHED_FEATURE_LENGTH
                ):
                    feature_codes[feature] = code
            codes.append(code)
        encoded = np.asarray(codes, dtype=np.intp)
        # Unlike advanced-index += or a histogram, add.at preserves each
        # float32 addition in feature order, including repeated buckets.
        np.add.at(vector, np.abs(encoded) - 1, np.sign(encoded))
        norm = float(np.linalg.norm(vector))
        if norm > 0.0:
            vector /= norm
        return vector
