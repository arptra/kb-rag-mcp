"""Lazy sentence-transformers provider for Qwen3 Embedding."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, cast

import numpy as np
from numpy.typing import NDArray

logger = logging.getLogger(__name__)


class SentenceTransformerEmbeddingProvider:
    """Normalized document and instructed-query embeddings with lazy model loading."""

    def __init__(
        self,
        *,
        model_name: str,
        device: Literal["auto", "cpu", "mps", "cuda"],
        batch_size: int,
        max_seq_length: int,
        dimension: int,
        query_instruction: str,
        local_files_only: bool = True,
    ) -> None:
        self._model_name = model_name
        self._device_setting = device
        self._batch_size = batch_size
        self._max_seq_length = max_seq_length
        self._dimension = dimension
        self._query_instruction = query_instruction
        self._local_files_only = local_files_only
        self._model: Any | None = None

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def provider_name(self) -> str:
        return "sentence_transformers"

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def cache_identity(self) -> str:
        return json.dumps(
            {
                "provider": self.provider_name,
                "model": self.model_name,
                "dimension": self.dimension,
                "max_seq_length": self._max_seq_length,
                "query_instruction": self._query_instruction,
                "local_files_only": self._local_files_only,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def embed_documents(self, texts: list[str]) -> NDArray[np.float32]:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        model = self._get_model()
        encoded = model.encode(
            texts,
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return self._validate_matrix(encoded, len(texts))

    def embed_query(self, query: str) -> NDArray[np.float32]:
        model = self._get_model()
        prompts = getattr(model, "prompts", {})
        if isinstance(prompts, dict) and "query" in prompts:
            encoded = model.encode(
                query,
                prompt_name="query",
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        else:
            instructed = f"Instruct: {self._query_instruction}\nQuery: {query}"
            encoded = model.encode(
                instructed,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        vector = np.asarray(encoded, dtype=np.float32).reshape(-1)
        if vector.shape != (self.dimension,):
            raise ValueError(
                f"Embedding model returned query dimension {vector.shape[0]}, "
                f"expected {self.dimension}"
            )
        if not np.isfinite(vector).all():
            raise ValueError("Embedding model returned NaN or infinite query values")
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise ValueError("Embedding model returned a zero query vector")
        return cast(NDArray[np.float32], vector / norm)

    def _get_model(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            device = self._resolve_device()
            logger.info("Loading embedding model %s on %s", self.model_name, device)
            try:
                self._model = SentenceTransformer(
                    self.model_name,
                    device=device,
                    truncate_dim=self.dimension,
                    local_files_only=self._local_files_only,
                )
            except Exception as exc:
                if not self._local_files_only:
                    raise
                raise RuntimeError(
                    "Embedding model is not available locally and network access is disabled. "
                    "Set KB_EMBEDDING_MODEL to a local model directory or use "
                    "KB_EMBEDDING_PROVIDER=hash. No external download was attempted."
                ) from exc
            self._model.max_seq_length = self._max_seq_length
        return self._model

    def _resolve_device(self) -> str:
        if self._device_setting != "auto":
            return self._device_setting
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _validate_matrix(self, encoded: Any, expected_rows: int) -> NDArray[np.float32]:
        matrix = np.asarray(encoded, dtype=np.float32)
        if matrix.shape != (expected_rows, self.dimension):
            raise ValueError(
                f"Embedding model returned shape {matrix.shape}, "
                f"expected {(expected_rows, self.dimension)}"
            )
        if not np.isfinite(matrix).all():
            raise ValueError("Embedding model returned NaN or infinite document values")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        if np.any(norms == 0.0):
            raise ValueError("Embedding model returned a zero document vector")
        return cast(NDArray[np.float32], matrix / norms)
