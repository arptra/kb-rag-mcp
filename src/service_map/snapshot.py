"""Finalize graph and service-map artifacts as one revisioned snapshot."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from service_map.builder import ServiceMapBuildResult

AnalysisMode = Literal["static", "static+gigacode", "partial"]


def finalize_snapshot(
    result: ServiceMapBuildResult,
    *,
    mode: AnalysisMode,
    verification: dict[str, Any] | None = None,
) -> ServiceMapBuildResult:
    """Give both persisted projections one deterministic identity and normalized edge states."""
    generated_at = datetime.now(UTC)
    normalized_edges = []
    for edge in result.graph.edges:
        status = edge.status
        if edge.metadata.get("verification_status") == "rejected":
            status = "rejected"
        elif edge.confidence in {"DECLARED", "HIGH"}:
            status = "confirmed"
        elif edge.confidence == "UNRESOLVED":
            status = "unresolved"
        elif status not in {"confirmed", "rejected"}:
            status = "inferred"
        origin = edge.origin
        if edge.confidence == "DECLARED" and origin == "static":
            origin = "declared"
        normalized_edges.append(edge.model_copy(update={"status": status, "origin": origin}))

    summary = dict(verification or {})
    graph = result.graph.model_copy(
        update={
            "schema_version": 2,
            "generated_at": generated_at,
            "analysis_mode": mode,
            "verification": summary,
            "edges": normalized_edges,
        },
        deep=True,
    )
    normalized_by_id = {edge.id: edge for edge in normalized_edges}
    normalized_dependencies = []
    for dependency in result.service_map.dependencies:
        normalized_edge = normalized_by_id.get(dependency.id)
        normalized_dependencies.append(
            dependency.model_copy(
                update={
                    "status": (
                        normalized_edge.status
                        if normalized_edge is not None
                        else dependency.status
                    ),
                    "origin": (
                        normalized_edge.origin
                        if normalized_edge is not None
                        else dependency.origin
                    ),
                }
            )
        )
    service_map = result.service_map.model_copy(
        update={
            "schema_version": 2,
            "generated_at": generated_at,
            "analysis_mode": mode,
            "verification": summary,
            "dependencies": normalized_dependencies,
        },
        deep=True,
    )
    canonical = {
        "algorithm": graph.algorithm,
        "nodes": [item.model_dump(mode="json") for item in graph.nodes],
        "edges": [item.model_dump(mode="json") for item in graph.edges],
        "evidence": [item.model_dump(mode="json") for item in graph.evidence],
        "services": [item.model_dump(mode="json") for item in service_map.services],
        "dependencies": [
            item.model_dump(mode="json") for item in service_map.dependencies
        ],
        "mode": mode,
        "verification": summary,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    snapshot_id = f"sha256:{digest}"
    graph = graph.model_copy(update={"snapshot_id": snapshot_id})
    service_map = service_map.model_copy(update={"snapshot_id": snapshot_id})
    return ServiceMapBuildResult(
        graph=graph,
        service_map=service_map,
        partial=result.partial,
    )
