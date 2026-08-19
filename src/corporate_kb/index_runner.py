"""Build RAG caches in a disposable process and publish only complete results."""

from __future__ import annotations

import multiprocessing
import os
import shutil
import tempfile
import time
import traceback
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


def _build_worker(settings: Settings, staged_dir: Path, error_path: Path) -> None:
    try:
        live_cache_dir = settings.cache_dir
        worker_settings = settings.model_copy(update={"cache_dir": staged_dir})
        service = KnowledgeService(
            worker_settings,
            cache=_StagedCacheManager(live_cache_dir, staged_dir),
        )
        service.build_index(force=True, reuse_unchanged=True)
    except BaseException:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


class IndexBuildProcessRunner:
    """Supervise a cancelable index worker without mutating the serving cache mid-build."""

    _CACHE_FILES = ("documents.json", "chunks.json", "embeddings.npy", "manifest.json")

    def __init__(self, settings: Settings, *, timeout_seconds: int = 600) -> None:
        self._settings = settings.resolved()
        self._timeout_seconds = timeout_seconds

    def build(self, *, cancel: CancellationSignal | None = None) -> None:
        if cancel is not None and cancel.is_set():
            raise IndexBuildCancelled("RAG index build was cancelled")

        destination = self._settings.cache_dir
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".index-build-", dir=destination.parent)
        )
        staged_dir = temporary / "cache"
        error_path = temporary / "error.txt"
        try:
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_build_worker,
                args=(self._settings, staged_dir, error_path),
                name="rag-index-build",
                daemon=True,
            )
            process.start()
            deadline = time.monotonic() + self._timeout_seconds
            while process.is_alive():
                if cancel is not None and cancel.is_set():
                    self._stop(process)
                    raise IndexBuildCancelled("RAG index build was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop(process)
                    raise IndexBuildTimedOut(
                        f"RAG index build exceeded {self._timeout_seconds} seconds"
                    )
                process.join(timeout=min(0.1, remaining))
            process.join()
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
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    @classmethod
    def _publish(cls, staged_dir: Path, destination: Path) -> None:
        missing = [name for name in cls._CACHE_FILES if not (staged_dir / name).is_file()]
        if missing:
            raise RuntimeError(f"RAG index worker did not produce: {', '.join(missing)}")
        destination.mkdir(parents=True, exist_ok=True)
        for name in cls._CACHE_FILES:
            os.replace(staged_dir / name, destination / name)
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
