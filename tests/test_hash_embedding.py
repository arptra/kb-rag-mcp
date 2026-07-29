import numpy as np

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider


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
