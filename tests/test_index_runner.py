from __future__ import annotations

import threading
from pathlib import Path

import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.index_runner import IndexBuildCancelled, IndexBuildProcessRunner
from corporate_kb.service import KnowledgeService


def test_index_process_publishes_complete_cache_only_after_success(
    settings_factory,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "one.md").write_text("# One\n\nFirst document.", encoding="utf-8")
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    initial = service.build_index(force=True)
    assert initial.document_count == 1

    (settings.knowledge_dir / "two.md").write_text("# Two\n\nSecond document.", encoding="utf-8")
    IndexBuildProcessRunner(settings, timeout_seconds=30).build()
    refreshed = service.reload_cached_index()

    assert refreshed.document_count == 2
    assert {item.source_path for item in service.list_documents(limit=10)} == {
        "one.md",
        "two.md",
    }


def test_index_process_honours_cancellation_before_start(
    settings_factory,
    tmp_path: Path,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    settings.cache_dir.mkdir(parents=True)
    marker = settings.cache_dir / "keep.txt"
    marker.write_text("serving cache", encoding="utf-8")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(IndexBuildCancelled):
        IndexBuildProcessRunner(settings, timeout_seconds=30).build(cancel=cancel)

    assert marker.read_text(encoding="utf-8") == "serving cache"
    assert list(tmp_path.rglob(".index-build-*")) == []
