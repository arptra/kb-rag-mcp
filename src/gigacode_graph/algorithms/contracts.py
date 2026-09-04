"""Stable contracts for pluggable graph construction algorithms."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphSnapshot

if TYPE_CHECKING:
    from gigacode_graph.scanner import ScanTarget
    from service_map.builder import RepositoryInput, ServiceMapBuildResult


ProgressCallback = Callable[[str], None]
CheckpointCallback = Callable[[GraphSnapshot], None]


class CancellationSignal(Protocol):
    """Minimal cancellation contract shared by CLI and server workers."""

    def is_set(self) -> bool: ...


class GraphBuildCancelled(RuntimeError):
    """Raised by an algorithm when its caller requested cancellation."""


@dataclass(frozen=True, slots=True)
class GraphAlgorithmDescriptor:
    """Identity that participates in snapshots, replay and module cache keys."""

    id: str
    version: str
    description: str
    cache_namespace: str
    capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "description": self.description,
            "cache_namespace": self.cache_namespace,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class GraphBuildRequest:
    """One deterministic unit of work passed to every builder implementation."""

    targets: tuple[ScanTarget, ...]
    discovery_only: bool = False
    run_id: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class GraphBuildContext:
    """Runtime services supplied by the caller, never hidden as global state."""

    settings: GraphSettings
    progress: ProgressCallback | None = None
    checkpoint: CheckpointCallback | None = None
    cancel: CancellationSignal | None = None

    def emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def check_cancelled(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise GraphBuildCancelled("Graph construction was cancelled")


@dataclass(frozen=True, slots=True)
class GraphBuildResult:
    """Algorithm output plus machine-readable diagnostics for debug tooling."""

    graph: GraphSnapshot
    descriptor: GraphAlgorithmDescriptor
    metrics: Mapping[str, int | float | str | bool] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


class GraphBuildAlgorithm(Protocol):
    """Structural interface implemented by graph builders."""

    @property
    def descriptor(self) -> GraphAlgorithmDescriptor: ...

    def build(
        self,
        request: GraphBuildRequest,
        context: GraphBuildContext,
    ) -> GraphBuildResult: ...


class GraphVerificationAlgorithm(Protocol):
    """Contract for optional graph verifiers/enrichers such as GigaCode."""

    @property
    def id(self) -> str: ...

    def verify(
        self,
        result: ServiceMapBuildResult,
        repositories: list[RepositoryInput],
        *,
        verify_all: bool = False,
        cancel: CancellationSignal | None = None,
        progress: ProgressCallback | None = None,
    ) -> tuple[ServiceMapBuildResult, dict[str, Any]]: ...
