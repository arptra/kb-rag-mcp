"""Warm rebuilds do not rewrite vectors; progress stays bounded and accurate."""

import json

import numpy as np

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.index_runner import _ProgressReporter
from corporate_kb.service import KnowledgeService


class CountingHashProvider(HashEmbeddingProvider):
    def __init__(self, dimension):
        super().__init__(dimension)
        self.texts = []

    def embed_documents(self, texts):
        self.texts.extend(texts)
        return super().embed_documents(texts)


def test_legacy_warm_rebuild_only_writes_vectors_for_new_text(settings_factory, monkeypatch):
    settings = settings_factory(embedding_checkpoint_batch_size=2)
    settings.knowledge_dir.mkdir()
    for number in range(5):
        (settings.knowledge_dir / f"{number}.md").write_text(
            f"# Document {number}\n\nKnown text {number}."
        )
    provider = CountingHashProvider(settings.embedding_dimension)
    service = KnowledgeService(settings, provider=provider)
    service.build_index()
    old = service.cache.load_snapshot()
    assert old is not None
    service.history.import_snapshot(
        old.documents, old.chunks, old.embeddings, old.manifest.embedding_cache_identity
    )
    writes = []
    save = service.history.save_embeddings

    def save_new(identity, chunks, matrix, **kwargs):
        writes.extend(chunk.embedding_text for chunk in chunks)
        return save(identity, chunks, matrix, **kwargs)

    monkeypatch.setattr(service.history, "save_embeddings", save_new)
    provider.texts.clear()
    events = []
    service.build_index(force=True, reuse_unchanged=True, progress=events.append)
    assert provider.texts == writes == []
    final = [event for event in events if event["stage"] == "embedding"][-1]
    assert final["reused"] == final["completed"] == final["total"] == len(old.chunks)
    assert final["computed"] == 0
    assert final["provider"] == "hash"
    snapshot = service.cache.load_snapshot()
    np.testing.assert_array_equal(snapshot.embeddings, old.embeddings)

    (settings.knowledge_dir / "new.md").write_text("# New policy\n\nNew unique text.")
    events.clear()
    service.build_index(force=True, reuse_unchanged=True, progress=events.append)
    assert len(writes) == len(provider.texts) == 1
    assert "New unique text" in writes[0]
    final = [event for event in events if event["stage"] == "embedding"][-1]
    assert final["computed"] == 1
    assert final["reused"] == len(old.chunks)
    assert final["computed"] + final["reused"] == final["completed"] == final["total"]


def test_duplicate_text_in_batch_is_computed_and_saved_once(settings_factory, monkeypatch):
    settings = settings_factory()
    settings.knowledge_dir.mkdir()
    (settings.knowledge_dir / "page.md").write_text("# Page\n\nShared text.")
    provider = CountingHashProvider(settings.embedding_dimension)
    service = KnowledgeService(settings, provider=provider)
    chunk = service.chunker.chunk(service._load_documents()[0])[0]
    batch = [chunk, chunk.model_copy(update={"chunk_id": "duplicate"})]
    writes = []
    save = service.history.save_embeddings

    def record_save(identity, chunks, matrix, **kwargs):
        writes.extend(chunks)
        return save(identity, chunks, matrix, **kwargs)

    monkeypatch.setattr(service.history, "save_embeddings", record_save)
    matrix, reused, computed = service._embed_checkpoint_batch(batch)
    assert (reused, computed) == (0, 2)
    assert len(provider.texts) == len(writes) == 1
    np.testing.assert_array_equal(matrix[0], matrix[1])
    again, reused, computed = service._embed_checkpoint_batch(batch)
    assert (reused, computed) == (2, 0)
    assert len(provider.texts) == len(writes) == 1
    np.testing.assert_array_equal(again, matrix)


def test_worker_progress_limits_repeated_io_but_keeps_boundaries(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("corporate_kb.index_runner.time.monotonic", lambda: clock[0])
    path = tmp_path / "progress.jsonl"
    reporter = _ProgressReporter(path)
    reporter({"stage": "loading", "completed": 0})
    for completed in range(1, 1000):
        reporter({"stage": "loading", "completed": completed})
    reporter({"stage": "embedding", "completed": 0, "total": 1000})
    for completed in range(1, 100):
        reporter({"stage": "embedding", "completed": completed, "total": 1000})
    clock[0] = 0.6
    reporter({"stage": "embedding", "completed": 500, "total": 1000})
    reporter({"stage": "embedding", "completed": 1000, "total": 1000, "reused": 999, "computed": 1})
    reporter({"stage": "saving", "message": "Publishing validated cache"})
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["stage"] for event in events] == [
        "loading",
        "embedding",
        "embedding",
        "embedding",
        "saving",
    ]
    assert events[2]["completed"] == 500
    assert events[3]["completed"] == events[3]["total"] == 1000
    assert events[3]["reused"] == 999 and events[3]["computed"] == 1
