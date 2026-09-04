"""Production source-derived algorithm exposed through the common contract."""

from __future__ import annotations

import time

from gigacode_graph.algorithms.base import BaseGraphBuildAlgorithm
from gigacode_graph.algorithms.contracts import (
    GraphAlgorithmDescriptor,
    GraphBuildContext,
    GraphBuildRequest,
    GraphBuildResult,
)
from gigacode_graph.scanner import RepositoryScanner


class StaticV2Algorithm(BaseGraphBuildAlgorithm):
    """Tree-sitter/config/OpenAPI scanner with bounded service-call tracing."""

    _DESCRIPTOR = GraphAlgorithmDescriptor(
        id="static-v2",
        version="2.0.0",
        description=(
            "Java/Kotlin source, configuration, OpenAPI and bounded call-chain analysis"
        ),
        cache_namespace="static-v2-service-map-v5",
        capabilities=(
            "layout-discovery",
            "java",
            "kotlin",
            "http",
            "kafka",
            "service-call-tracing",
            "evidence",
        ),
    )

    @property
    def descriptor(self) -> GraphAlgorithmDescriptor:
        return self._DESCRIPTOR

    def build(
        self,
        request: GraphBuildRequest,
        context: GraphBuildContext,
    ) -> GraphBuildResult:
        if not request.targets:
            raise ValueError("At least one scan target is required")
        context.check_cancelled()
        started_at = time.monotonic()
        context.emit(
            f"Algorithm {self.descriptor.id}@{self.descriptor.version} start; "
            f"targets={len(request.targets)}; discovery_only={request.discovery_only}"
        )
        scanner = RepositoryScanner(context.settings)
        if request.discovery_only:
            graph = scanner.discover_targets(list(request.targets), progress=context.progress)
        else:
            graph = scanner.scan_targets(
                list(request.targets),
                progress=context.progress,
                checkpoint=context.checkpoint,
            )
        context.check_cancelled()
        elapsed = time.monotonic() - started_at
        algorithm = self.descriptor.as_dict()
        algorithm["run_id"] = request.run_id
        graph = graph.model_copy(update={"algorithm": algorithm})
        context.emit(
            f"Algorithm {self.descriptor.id}@{self.descriptor.version} ready; "
            f"nodes={len(graph.nodes)}; edges={len(graph.edges)}; elapsed={elapsed:.3f}s"
        )
        return GraphBuildResult(
            graph=graph,
            descriptor=self.descriptor,
            metrics={
                "target_count": len(request.targets),
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
                "evidence_count": len(graph.evidence),
                "issue_count": len(graph.issues),
                "elapsed_seconds": round(elapsed, 6),
            },
        )
