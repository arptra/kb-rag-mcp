from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from corporate_kb.embeddings.sentence_transformer import SentenceTransformerEmbeddingProvider


def provider() -> SentenceTransformerEmbeddingProvider:
    return SentenceTransformerEmbeddingProvider(
        model_name="/local/models/qwen-embedding",
        device="cpu",
        batch_size=2,
        max_seq_length=128,
        dimension=4,
        query_instruction="Retrieve local knowledge.",
        local_files_only=True,
    )


def test_provider_forces_sentence_transformers_local_files_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeModel:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            self.prompts: dict[str, str] = {}
            self.max_seq_length = 0
            captured["model_name"] = model_name
            captured.update(kwargs)

        def encode(self, text: str, **kwargs: Any) -> np.ndarray:
            del text, kwargs
            return np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=FakeModel),
    )

    vector = provider().embed_query("local query")

    assert captured["model_name"] == "/local/models/qwen-embedding"
    assert captured["local_files_only"] is True
    np.testing.assert_array_equal(vector, np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32))


def test_missing_local_model_has_practical_offline_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MissingModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise OSError("not found locally")

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=MissingModel),
    )

    with pytest.raises(RuntimeError, match="No external download was attempted"):
        provider().embed_query("local query")
