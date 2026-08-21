"""Run CPU-heavy repository analysis outside the HTTP server process."""

from __future__ import annotations

import multiprocessing
import signal
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import ScanIssue
from gigacode_graph.store import JsonGraphStore
from service_map.builder import RepositoryInput, ServiceMapBuilder, ServiceMapBuildResult
from service_map.models import ServiceMapIssue
from service_map.store import JsonServiceMapStore


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class WorkerProcess(Protocol):
    @property
    def exitcode(self) -> int | None: ...

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
    progress_path: Path,
    checkpoint_path: Path,
    force_service_ids: set[str],
    force_all: bool,
) -> None:
    def report(message: str) -> None:
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")

    def save_checkpoint(result: ServiceMapBuildResult) -> None:
        JsonGraphStore(graph_path).save(result.graph)
        JsonServiceMapStore(service_map_path).save(result.service_map)
        checkpoint_path.write_text(str(time.time_ns()), encoding="utf-8")
        report(
            f"Checkpoint ready: services={len(result.service_map.services)}; "
            f"nodes={len(result.graph.nodes)}; edges={len(result.graph.edges)}"
        )

    try:
        report(f"Worker started; repositories={len(repositories)}")
        result = ServiceMapBuilder(settings).build(
            repositories,
            progress=report,
            checkpoint=save_checkpoint,
            force_service_ids=force_service_ids,
            force_all=force_all,
        )
        report("Writing system_graph.json")
        JsonGraphStore(graph_path).save(result.graph)
        report("Writing service_map.json")
        JsonServiceMapStore(service_map_path).save(result.service_map)
        report("Worker completed successfully")
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
        progress: Callable[[str], None] | None = None,
        checkpoint: Callable[[ServiceMapBuildResult], None] | None = None,
        force_service_ids: set[str] | None = None,
        force_all: bool = False,
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
            progress_path = temporary / "progress.log"
            checkpoint_path = temporary / "checkpoint.ready"
            context = multiprocessing.get_context("spawn")
            process = context.Process(
                target=_build_worker,
                args=(
                    self._settings,
                    repositories,
                    graph_path,
                    service_map_path,
                    error_path,
                    progress_path,
                    checkpoint_path,
                    force_service_ids or set(),
                    force_all,
                ),
                name="service-map-analysis",
                daemon=True,
            )
            process.start()
            if progress is not None:
                progress(
                    f"Analysis supervisor started worker "
                    f"pid={getattr(process, 'pid', 'unknown')}; "
                    f"timeout={self._timeout_seconds}s"
                )
            progress_offset = 0
            last_progress: str | None = None
            published_checkpoint: int | None = None
            supervisor_started_at = time.monotonic()
            last_heartbeat_at = supervisor_started_at
            deadline = supervisor_started_at + self._timeout_seconds
            while process.is_alive():
                progress_offset, latest = self._drain_progress(
                    progress_path,
                    progress_offset,
                    progress,
                )
                last_progress = latest or last_progress
                published_checkpoint = self._publish_checkpoint_if_changed(
                    graph_path,
                    service_map_path,
                    checkpoint_path,
                    published_checkpoint,
                    checkpoint,
                )
                if cancel is not None and cancel.is_set():
                    self._stop(process)
                    self._drain_progress(progress_path, progress_offset, progress)
                    raise ServiceMapBuildCancelled("Repository analysis was cancelled")
                now = time.monotonic()
                if progress is not None and now - last_heartbeat_at >= 5:
                    progress(
                        f"Analysis heartbeat: worker_pid={getattr(process, 'pid', 'unknown')}; "
                        f"elapsed={now - supervisor_started_at:.1f}s; "
                        f"last_operation={last_progress or 'worker startup'}"
                    )
                    last_heartbeat_at = now
                remaining = deadline - now
                if remaining <= 0:
                    self._stop(process)
                    _offset, latest = self._drain_progress(
                        progress_path,
                        progress_offset,
                        progress,
                    )
                    last_progress = latest or last_progress
                    suffix = f"; last operation: {last_progress}" if last_progress else ""
                    partial = self._partial_result(
                        graph_path,
                        service_map_path,
                        f"Analysis reached the {self._timeout_seconds}s time limit{suffix}",
                    )
                    if partial is not None:
                        if progress is not None:
                            progress("Time limit reached; publishing the latest partial checkpoint")
                        if checkpoint is not None:
                            checkpoint(partial)
                        return partial
                    raise ServiceMapBuildTimedOut(
                        f"Repository analysis exceeded {self._timeout_seconds} seconds{suffix}"
                    )
                process.join(timeout=min(0.1, remaining))
            process.join()
            _offset, latest = self._drain_progress(
                progress_path,
                progress_offset,
                progress,
            )
            last_progress = latest or last_progress
            published_checkpoint = self._publish_checkpoint_if_changed(
                graph_path,
                service_map_path,
                checkpoint_path,
                published_checkpoint,
                checkpoint,
            )
            if process.exitcode != 0:
                detail = (
                    error_path.read_text(encoding="utf-8")[-4000:]
                    if error_path.is_file()
                    else self._exit_detail(process.exitcode)
                )
                if last_progress:
                    detail = f"{detail}\nLast completed operation: {last_progress}"
                partial = self._partial_result(graph_path, service_map_path, detail)
                if partial is not None:
                    if progress is not None:
                        progress("Worker failed; publishing the latest partial checkpoint")
                    if checkpoint is not None:
                        checkpoint(partial)
                    return partial
                raise RuntimeError(f"Repository analysis failed: {detail}")
            return ServiceMapBuildResult(
                graph=JsonGraphStore(graph_path).load(),
                service_map=JsonServiceMapStore(service_map_path).load(),
            )

    @staticmethod
    def _publish_checkpoint_if_changed(
        graph_path: Path,
        service_map_path: Path,
        checkpoint_path: Path,
        published: int | None,
        callback: Callable[[ServiceMapBuildResult], None] | None,
    ) -> int | None:
        if (
            callback is None
            or not graph_path.is_file()
            or not service_map_path.is_file()
            or not checkpoint_path.is_file()
        ):
            return published
        revision = checkpoint_path.stat().st_mtime_ns
        if revision == published:
            return published
        callback(
            ServiceMapBuildResult(
                graph=JsonGraphStore(graph_path).load(),
                service_map=JsonServiceMapStore(service_map_path).load(),
                partial=True,
            )
        )
        return revision

    @staticmethod
    def _partial_result(
        graph_path: Path,
        service_map_path: Path,
        reason: str,
    ) -> ServiceMapBuildResult | None:
        if not graph_path.is_file() or not service_map_path.is_file():
            return None
        message = f"Partial analysis checkpoint: {reason}"
        graph = JsonGraphStore(graph_path).load()
        service_map = JsonServiceMapStore(service_map_path).load()
        return ServiceMapBuildResult(
            graph=graph.model_copy(
                update={
                    "issues": [
                        *graph.issues,
                        ScanIssue(repository="system", message=message),
                    ]
                }
            ),
            service_map=service_map.model_copy(
                update={
                    "issues": [
                        *service_map.issues,
                        ServiceMapIssue(repository="system", message=message),
                    ]
                }
            ),
            partial=True,
        )

    @staticmethod
    def _drain_progress(
        path: Path,
        offset: int,
        callback: Callable[[str], None] | None,
    ) -> tuple[int, str | None]:
        if not path.is_file():
            return offset, None
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
            next_offset = handle.tell()
        latest: str | None = None
        for raw_line in chunk.splitlines():
            message = raw_line.decode("utf-8", errors="replace").strip()
            if not message:
                continue
            latest = message
            if callback is not None:
                callback(message)
        return next_offset, latest

    @staticmethod
    def _exit_detail(exitcode: int | None) -> str:
        if exitcode is not None and exitcode < 0:
            number = -exitcode
            try:
                name = signal.Signals(number).name
            except ValueError:
                name = "UNKNOWN_SIGNAL"
            return f"worker was terminated by {name} (signal {number})"
        return f"worker exited with code {exitcode}"

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
