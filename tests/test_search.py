from __future__ import annotations

from pathlib import Path

import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.models import SearchFilters
from corporate_kb.service import KnowledgeIndexMissingError, KnowledgeService


class CountingHashProvider(HashEmbeddingProvider):
    def __init__(self, dimension: int) -> None:
        super().__init__(dimension)
        self.document_calls = 0

    def embed_documents(self, texts: list[str]):
        self.document_calls += 1
        return super().embed_documents(texts)


class FailingLoader:
    """Makes an unintended source-tree scan fail the test immediately."""

    def load_directory(self, _root: Path):
        raise AssertionError("serving a prepared index must not scan knowledge files")


def write_knowledge(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "limits.md").write_text(
        """---
document_type: service
service: limits-service
domain: payments
status: current
---
# Limits Service

The limits-service owns daily limits and publishes limit-updated.
""",
        encoding="utf-8",
    )
    (root / "customer.txt").write_text("Customer service owns customer profile.", encoding="utf-8")


def test_service_build_search_cache_force_get_and_list(settings_factory) -> None:
    settings = settings_factory()
    write_knowledge(settings.knowledge_dir)
    first_provider = CountingHashProvider(settings.embedding_dimension)
    first = KnowledgeService(settings, provider=first_provider)

    built = first.build_index()
    results = first.search(
        "daily limits limits-service",
        top_k=2,
        filters=SearchFilters(service="limits-service", status="current"),
    )
    documents = first.list_documents(filters=SearchFilters(status="current"), limit=10)
    loaded_document = first.get_document(results[0].document_id)

    assert built.loaded_from_cache is False
    assert built.document_count == 2
    assert first_provider.document_calls == 1
    assert results[0].source_path == "limits.md"
    assert loaded_document.title == "Limits Service"
    assert len(documents) == 2

    cached_provider = CountingHashProvider(settings.embedding_dimension)
    cached = KnowledgeService(settings, provider=cached_provider)
    cached_stats = cached.load_or_build_index()
    assert cached_stats.loaded_from_cache is True
    assert cached_provider.document_calls == 0

    rebuilt = cached.build_index(force=True)
    assert rebuilt.loaded_from_cache is False
    assert cached_provider.document_calls == 1


def test_service_invalidates_cache_after_document_change(settings_factory) -> None:
    settings = settings_factory(auto_index=True)
    write_knowledge(settings.knowledge_dir)
    first = KnowledgeService(settings, provider=CountingHashProvider(settings.embedding_dimension))
    original = first.build_index()
    (settings.knowledge_dir / "limits.md").write_text("# Changed\n\nnew content", encoding="utf-8")

    provider = CountingHashProvider(settings.embedding_dimension)
    second = KnowledgeService(settings, provider=provider)
    changed = second.load_or_build_index()

    assert changed.knowledge_hash != original.knowledge_hash
    assert changed.loaded_from_cache is False
    assert provider.document_calls == 1


def test_service_requires_explicit_index_when_auto_index_is_disabled(settings_factory) -> None:
    settings = settings_factory(auto_index=False)
    write_knowledge(settings.knowledge_dir)
    service = KnowledgeService(
        settings,
        provider=CountingHashProvider(settings.embedding_dimension),
    )

    with pytest.raises(KnowledgeIndexMissingError, match=r"\./scripts/dev\.sh index"):
        service.load_or_build_index()


def test_search_uses_prepared_cache_without_scanning_source_documents(settings_factory) -> None:
    settings = settings_factory(auto_index=False)
    write_knowledge(settings.knowledge_dir)
    builder = KnowledgeService(
        settings,
        provider=CountingHashProvider(settings.embedding_dimension),
    )
    builder.build_index()

    service = KnowledgeService(
        settings,
        provider=CountingHashProvider(settings.embedding_dimension),
        loader=FailingLoader(),  # type: ignore[arg-type]
    )

    results = service.search("daily limits", top_k=1)

    assert results[0].source_path == "limits.md"
    assert service.stats().loaded_from_cache is True
