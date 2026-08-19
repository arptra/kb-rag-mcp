"""Run CPU-heavy repository analysis outside the HTTP server process."""

from __future__ import annotations

import multiprocessing
import tempfile
import time
import traceback
from pathlib import Path
from typing import Protocol

from gigacode_graph.config import GraphSettings
from gigacode_graph.store import JsonGraphStore
from service_map.builder import RepositoryInput, ServiceMapBuilder, ServiceMapBuildResult
from service_map.store import JsonServiceMapStore


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class WorkerProcess(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class ServiceMapBuildCancelled(RuntimeError):
    """Raised when the user cancels repository analysis."""


class ServiceMapBuildTimedOut(RuntimeError):
    """Raised when repository analysis exceeds its hard deadline."""


def _build_worker(
    settings: GraphSettings,
    repositories: list[RepositoryInput],
    graph_path: Path,
    service_map_path: Path,
    error_path: Path,
) -> None:
    try:
        result = ServiceMapBuilder(settings).build(repositories)
        JsonGraphStore(graph_path).save(result.graph)
        JsonServiceMapStore(service_map_path).save(result.service_map)
    except BaseException:
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


class ServiceMapProcessRunner:
    """Supervise a disposable analysis process with cancellation and a hard timeout."""

    def __init__(self, settings: GraphSettings, *, timeout_seconds: int = 60) -> None:
        self._settings = settings
        self._timeout_seconds = timeout_seconds

    def build(
        self,
        repositories: list[RepositoryInput],
        *,
        cancel: CancellationSignal | None = None,
    ) -> ServiceMapBuildResult:
        if cancel is not None and cancel.is_set():
            raise ServiceMapBuildCancelled("Repository analysis was cancelled")
        if not repositories:
            return ServiceMapBuilder(self._settings).build([])

        with tempfile.TemporaryDirectory(prefix="service-map-") as directory:
            temporary = Path(directory)
            graph_path = temporary / "system_graph.json"
            service_map_path = temporary / "service_map.json"
            error_path = temporary / "error.txt"
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_build_worker,
                args=(
                    self._settings,
                    repositories,
                    graph_path,
                    service_map_path,
                    error_path,
                ),
                name="service-map-analysis",
                daemon=True,
            )
            process.start()
            deadline = time.monotonic() + self._timeout_seconds
            while process.is_alive():
                if cancel is not None and cancel.is_set():
                    self._stop(process)
                    raise ServiceMapBuildCancelled("Repository analysis was cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop(process)
                    raise ServiceMapBuildTimedOut(
                        f"Repository analysis exceeded {self._timeout_seconds} seconds"
                    )
                process.join(timeout=min(0.1, remaining))
            process.join()
            if process.exitcode != 0:
                detail = (
                    error_path.read_text(encoding="utf-8")[-4000:]
                    if error_path.is_file()
                    else f"worker exited with code {process.exitcode}"
                )
                raise RuntimeError(f"Repository analysis failed: {detail}")
            return ServiceMapBuildResult(
                graph=JsonGraphStore(graph_path).load(),
                service_map=JsonServiceMapStore(service_map_path).load(),
            )

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
