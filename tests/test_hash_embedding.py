import hashlib
import random
import re

import numpy as np
import pytest
from numpy.typing import NDArray

from corporate_kb.embeddings import hash_provider
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider


def _original_embedding(text: str, dimension: int) -> NDArray[np.float32]:
    """Reference mapping used by persisted sha256-word-char-3gram-v1 caches."""
    vector = np.zeros(dimension, dtype=np.float32)
    words = re.findall(r"\w+", text.lower(), re.UNICODE)
    features = [f"w:{word}" for word in words]
    for word in words:
        padded = f"^{word}$"
        features.extend(
            f"c3:{padded[index : index + 3]}" for index in range(max(0, len(padded) - 2))
        )
    for feature in features:
        digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % dimension
        vector[index] += 1.0 if digest[8] & 1 else -1.0
    norm = float(np.linalg.norm(vector))
    if norm > 0.0:
        vector /= norm
    return vector


def test_hash_embeddings_are_deterministic_normalized_and_finite() -> None:
    provider = HashEmbeddingProvider(dimension=128)
    first = provider.embed_documents(["Daily LIMIT лимит"])
    second = provider.embed_documents(["Daily LIMIT лимит"])
    query = provider.embed_query("Daily LIMIT лимит")

    np.testing.assert_array_equal(first, second)
    np.testing.assert_allclose(first[0], query)
    np.testing.assert_allclose(np.linalg.norm(first, axis=1), [1.0], atol=1e-6)
    assert first.shape == (1, 128)
    assert query.shape == (128,)
    assert np.isfinite(first).all()


def test_hash_provider_handles_empty_inputs_without_nan() -> None:
    provider = HashEmbeddingProvider(dimension=32)

    assert provider.embed_documents([]).shape == (0, 32)
    assert np.array_equal(provider.embed_query(""), np.zeros(32, dtype=np.float32))


@pytest.mark.parametrize("dimension", [1, 2, 7, 128, 256, 4096])
def test_hash_embeddings_remain_bitwise_compatible_with_existing_caches(dimension: int) -> None:
    randomizer = random.Random(42)
    words = ["service", "Пользователь", "индекс", "token_17", "İ", "ß", "café", "漢字"]
    texts = [
        "",
        " \n\t --- !!! 👋 → / ",
        "A я 1 _ Z",
        "café cafe\u0301 Straße İSTANBUL 東京 Привет-Мир \uff11\uff12\uff13",
        "request_id=17 /api/v1?query=СЕРВИС; version:2.0.0",
        " ".join(randomizer.choices(words, k=700)),
        "collision " * 2000,
    ]
    provider = HashEmbeddingProvider(dimension=dimension)
    expected = np.vstack([_original_embedding(text, dimension) for text in texts])

    actual = provider.embed_documents(texts)
    repeated = provider.embed_documents(texts)

    assert actual.dtype == np.float32
    assert actual.tobytes() == expected.tobytes()
    assert repeated.tobytes() == expected.tobytes()
    for index, text in enumerate(texts):
        assert provider.embed_query(text).tobytes() == expected[index].tobytes()
    assert provider.cache_identity == f"hash:sha256-word-char-3gram-v1:dimension={dimension}"


def test_hash_feature_cache_reuses_hashes_without_sharing_between_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_sha256 = hashlib.sha256
    calls: list[bytes] = []

    def counted_sha256(feature: bytes) -> object:
        calls.append(feature)
        return original_sha256(feature)

    monkeypatch.setattr(hash_provider.hashlib, "sha256", counted_sha256)
    first = HashEmbeddingProvider(dimension=128)
    second = HashEmbeddingProvider(dimension=256)
    text = "cache кэш service сервис " * 100

    first.embed_query(text)
    initial_calls = len(calls)
    assert initial_calls > 0
    assert len(set(calls)) == initial_calls
    first.embed_documents([text, text])
    assert len(calls) == initial_calls
    second.embed_query(text)
    assert len(calls) == 2 * initial_calls


def test_full_feature_cache_keeps_vectors_compatible_for_unique_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hash_provider, "_FEATURE_CACHE_SIZE", 32)
    provider = HashEmbeddingProvider(dimension=7)
    texts = [" ".join(f"identifier_{batch}_{index}" for index in range(100)) for batch in range(3)]
    for text in texts:
        assert provider.embed_query(text).tobytes() == _original_embedding(text, 7).tobytes()
        assert len(provider._feature_codes) == 32


def test_feature_cache_does_not_retain_unbounded_word_lengths() -> None:
    provider = HashEmbeddingProvider(dimension=32)
    text = "я" * 10_000

    assert provider.embed_query(text).tobytes() == _original_embedding(text, 32).tobytes()
    assert provider._feature_codes
    assert all(len(feature) <= 64 for feature in provider._feature_codes)
    assert f"w:{text}" not in provider._feature_codes
