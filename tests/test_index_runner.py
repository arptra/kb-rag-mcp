from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.index_runner import (
    IndexBuildCancelled,
    IndexBuildProcessRunner,
    IndexBuildTimedOut,
)
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


@pytest.mark.parametrize("emits_progress", [True, False])
def test_default_watchdog_measures_inactivity_not_total_time(
    settings_factory, monkeypatch, emits_progress
):
    settings = settings_factory()
    clock = {"time": 0.0}

    class Process:
        exitcode = None
        steps = 0
        stopped = False

        def __init__(self, **kwargs):
            self.path = kwargs["args"][2].with_name("progress.jsonl")

        def start(self):
            pass

        def is_alive(self):
            return not self.stopped and self.steps < 6

        def join(self, timeout=None):
            if timeout is None or self.stopped:
                return
            self.steps += 1
            clock["time"] += 0.4
            if emits_progress:
                with self.path.open("a") as handle:
                    handle.write(json.dumps({"stage": "embedding", "completed": self.steps}) + "\n")
            if self.steps == 6:
                self.exitcode = 0

        def terminate(self):
            self.stopped = True

        kill = terminate

    class Context:
        def Process(self, **kwargs):
            return Process(**kwargs)

    monkeypatch.setattr(
        "corporate_kb.index_runner.multiprocessing.get_context", lambda _: Context()
    )
    monkeypatch.setattr("corporate_kb.index_runner.time.monotonic", lambda: clock["time"])
    monkeypatch.setattr(IndexBuildProcessRunner, "_publish", lambda *_: None)
    runner = IndexBuildProcessRunner(settings, no_progress_timeout_seconds=1)
    events = []
    if emits_progress:
        runner.build(progress=events.append)
        assert clock["time"] > 1
        assert events[-1]["stage"] == "published"
        assert any(event.get("completed") == 6 for event in events)
    else:
        with pytest.raises(IndexBuildTimedOut, match="no progress"):
            runner.build()


def test_failed_process_publication_does_not_activate_new_history(settings_factory, monkeypatch):
    from test_history import write_page

    settings = settings_factory()
    write_page(settings.knowledge_dir, 1, "Old payment window.")
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    write_page(settings.knowledge_dir, 2, "New payment window.")

    def fail(*_):
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(IndexBuildProcessRunner, "_publish", fail)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        IndexBuildProcessRunner(settings, timeout_seconds=30).build()
    assert service.history.read("confluence:team:123").source_version == "1"
    service.reload_cached_index()
    assert service.list_documents()[0].source_version == "1"
