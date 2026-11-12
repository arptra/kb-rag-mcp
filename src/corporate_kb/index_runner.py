"""Build RAG caches in a disposable process and publish only complete results."""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from corporate_kb.cache.manager import CachedIndex, CacheManager
from corporate_kb.config import Settings
from corporate_kb.service import KnowledgeService


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class WorkerProcess(Protocol):
    @property
    def exitcode(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class IndexBuildCancelled(RuntimeError):
    """Raised when an active RAG index build is cancelled."""


class IndexBuildTimedOut(RuntimeError):
    """Raised when a RAG index build exceeds its hard deadline."""


class _StagedCacheManager(CacheManager):
    """Read reusable embeddings from the live cache and write only to staging."""

    defer_history_activation = True

    def __init__(self, source_dir: Path, staged_dir: Path) -> None:
        super().__init__(staged_dir)
        self._source = CacheManager(source_dir)

    def load(
        self,
        *,
        knowledge_hash: str | None,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
        return self._source.load(
            knowledge_hash=knowledge_hash,
            embedding_cache_identity=embedding_cache_identity,
            chunking=chunking,
        )

    def load_compatible(
        self,
        *,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
        return self._source.load_compatible(
            embedding_cache_identity=embedding_cache_identity,
            chunking=chunking,
        )

    def load_snapshot(self) -> CachedIndex | None:
        return self._source.load_snapshot()


def _build_worker(settings: Settings, staged_dir: Path, error_path: Path) -> None:
    try:
        live_cache_dir = settings.cache_dir
        worker_settings = settings.model_copy(update={"cache_dir": staged_dir})
        service = KnowledgeService(
            worker_settings,
            cache=_StagedCacheManager(live_cache_dir, staged_dir),
        )

        def report(event: dict[str, object]) -> None:
            with error_path.with_name("progress.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                handle.flush()

        service.build_index(force=True, reuse_unchanged=True, progress=report)
    except BaseException:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


class IndexBuildProcessRunner:
    """Supervise a cancelable index worker without mutating the serving cache mid-build."""

    _CACHE_FILES = ("documents.json", "chunks.json", "embeddings.npy", "manifest.json")

    def __init__(
        self,
        settings: Settings,
        *,
        timeout_seconds: int | None = None,
        no_progress_timeout_seconds: int | None = None,
        overall_timeout_seconds: int | None = None,
    ) -> None:
        self._settings = settings.resolved()
        # Explicit timeout preserves the original caller contract; default is inactivity.
        self._timeout_seconds = (
            timeout_seconds
            or overall_timeout_seconds
            or getattr(settings, "index_build_overall_timeout_seconds", None)
        )
        self._no_progress_timeout = int(
            no_progress_timeout_seconds
            or getattr(settings, "index_build_no_progress_timeout_seconds", None)
            or getattr(settings, "index_build_timeout_seconds", 600)
            or 600
        )

    def build(
        self,
        *,
        cancel: CancellationSignal | None = None,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        if cancel is not None and cancel.is_set():
            raise IndexBuildCancelled("RAG index build was cancelled")

        destination = self._settings.cache_dir
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".index-build-", dir=destination.parent))
        staged_dir = temporary / "cache"
        error_path = temporary / "error.txt"
        progress_path = temporary / "progress.jsonl"
        try:
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_build_worker,
                args=(self._settings, staged_dir, error_path),
                name="rag-index-build",
                daemon=True,
            )
            process.start()
            started = time.monotonic()
            last_progress = started
            cursor = 0
            while process.is_alive():
                if cancel is not None and cancel.is_set():
                    self._stop(process)
                    raise IndexBuildCancelled("RAG index build was cancelled")
                cursor, events = self._progress_events(progress_path, cursor)
                for event in events:
                    last_progress = time.monotonic()
                    if progress is not None:
                        progress(event)
                now = time.monotonic()
                if self._timeout_seconds is not None and now - started >= self._timeout_seconds:
                    self._stop(process)
                    raise IndexBuildTimedOut(
                        f"RAG index build exceeded {self._timeout_seconds} seconds"
                    )
                if now - last_progress >= self._no_progress_timeout:
                    self._stop(process)
                    raise IndexBuildTimedOut(
                        "RAG index build made no progress for "
                        f"{self._no_progress_timeout} seconds; "
                        "completed embedding batches were saved"
                    )
                process.join(timeout=0.1)
            process.join()
            cursor, events = self._progress_events(progress_path, cursor)
            if progress is not None:
                for event in events:
                    progress(event)
            if process.exitcode != 0:
                detail = (
                    error_path.read_text(encoding="utf-8")[-4000:]
                    if error_path.is_file()
                    else f"worker exited with code {process.exitcode}"
                )
                raise RuntimeError(f"RAG index build failed: {detail}")
            if cancel is not None and cancel.is_set():
                raise IndexBuildCancelled("RAG index build was cancelled")
            self._publish(staged_dir, destination)
            if progress is not None:
                progress({"stage": "published", "message": "Index generation published"})
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _progress_events(path: Path, cursor: int) -> tuple[int, list[dict[str, object]]]:
        if not path.is_file():
            return cursor, []
        events = []
        with path.open(encoding="utf-8") as handle:
            handle.seek(cursor)
            while line := handle.readline():
                if not line.endswith("\n"):
                    break  # The worker has not finished writing this event yet.
                cursor = handle.tell()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        return cursor, events

    @classmethod
    def _publish(cls, staged_dir: Path, destination: Path) -> None:
        source = CacheManager(staged_dir).snapshot_directory()
        missing = [name for name in cls._CACHE_FILES if not (source / name).is_file()]
        if missing:
            raise RuntimeError(f"RAG index worker did not produce: {', '.join(missing)}")
        generation = uuid.uuid4().hex
        generation_root = destination / "generations"
        generation_root.mkdir(parents=True, exist_ok=True)
        os.replace(source, generation_root / generation)
        CacheManager(destination).publish_generation(generation)
        try:
            descriptor = os.open(destination, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _stop(process: WorkerProcess) -> None:
        if not process.is_alive():
            process.join()
            return
        process.terminate()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
