"""Contract validation, expectation checks and human-readable graph explanations."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from gigacode_graph.lab.models import EdgeTarget, GraphLabCase
from gigacode_graph.models import GraphEdge, GraphNode, GraphSnapshot


def validate_graph(
    graph: GraphSnapshot,
    case: GraphLabCase | None = None,
    *,
    repository_roots: dict[str, Path] | None = None,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    node_ids = [item.id for item in graph.nodes]
    edge_ids = [item.id for item in graph.edges]
    evidence_ids = [item.id for item in graph.evidence]
    for kind, values in (("node", node_ids), ("edge", edge_ids), ("evidence", evidence_ids)):
        duplicates = sorted(key for key, count in Counter(values).items() if count > 1)
        for duplicate in duplicates:
            failures.append(_finding("duplicate-id", f"Duplicate {kind} id: {duplicate}"))

    node_set = set(node_ids)
    evidence_set = set(evidence_ids)
    for edge in graph.edges:
        if edge.source not in node_set or edge.target not in node_set:
            failures.append(
                _finding(
                    "dangling-edge",
                    f"{edge.id}: {edge.source} -> {edge.target}",
                    edge_id=edge.id,
                )
            )
        _check_evidence_refs(edge.id, edge.evidence_ids, evidence_set, failures)
    for node in graph.nodes:
        _check_evidence_refs(node.id, node.evidence_ids, evidence_set, failures)

    if repository_roots:
        for evidence in graph.evidence:
            root = repository_roots.get(evidence.repository)
            if root is None:
                warnings.append(
                    _finding(
                        "unknown-evidence-repository",
                        f"No checkout registered for evidence {evidence.id}",
                    )
                )
                continue
            path = (root / evidence.file).resolve()
            try:
                path.relative_to(root.resolve())
            except ValueError:
                failures.append(
                    _finding("evidence-path-escape", f"{evidence.id}: {evidence.file}")
                )
                continue
            if not path.is_file():
                failures.append(
                    _finding("missing-evidence-file", f"{evidence.id}: {evidence.file}")
                )
                continue
            line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
            if evidence.line > line_count:
                failures.append(
                    _finding(
                        "invalid-evidence-line",
                        f"{evidence.id}: {evidence.file}:{evidence.line} > {line_count}",
                    )
                )

    if case is not None:
        _validate_expectations(graph, case, failures)
    return {
        "schema_version": 1,
        "status": "passed" if not failures else "failed",
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": failures,
        "warnings": warnings,
        "stats": graph.stats(),
    }


def explain_edge(graph: GraphSnapshot, edge_id: str) -> dict[str, Any]:
    edge = next((item for item in graph.edges if item.id == edge_id), None)
    if edge is None:
        raise KeyError(f"Unknown graph edge: {edge_id}")
    nodes = {item.id: item for item in graph.nodes}
    evidence = {item.id: item for item in graph.evidence}
    return {
        "edge": edge.model_dump(mode="json"),
        "source": _node_summary(nodes.get(edge.source)),
        "target": _node_summary(nodes.get(edge.target)),
        "evidence": [
            evidence[value].model_dump(mode="json")
            for value in edge.evidence_ids
            if value in evidence
        ],
        "decision": {
            "origin": edge.origin,
            "confidence": edge.confidence,
            "status": edge.status,
            "matcher": edge.metadata.get("matcher"),
            "verification_reason": edge.metadata.get("verification_reason"),
        },
    }


def explain_missing(
    graph: GraphSnapshot,
    *,
    source: str,
    target: str | None = None,
    protocol: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    services = [item for item in graph.nodes if item.type == "Service"]
    source_node = _resolve_service(services, source)
    target_node = _resolve_service(services, target) if target else None
    exits = [
        item
        for item in graph.nodes
        if item.type == "ExitPoint" and item.service_id == source_node.service_id
    ]
    entries = [
        item
        for item in graph.nodes
        if item.type == "EntryPoint"
        and (target_node is None or item.service_id == target_node.service_id)
    ]
    matching_exits = [item for item in exits if _node_contract_matches(item, protocol, operation)]
    matching_entries = [
        item for item in entries if _node_contract_matches(item, protocol, operation)
    ]
    related_edges = [
        item
        for item in graph.edges
        if item.type == "DEPENDS_ON"
        and (
            item.source == source_node.id
            or any(item.source == exitpoint.id for exitpoint in matching_exits)
        )
    ]
    reasons: list[str] = []
    if not exits:
        reasons.append("Static analysis found no outbound interface for the source service.")
    elif not matching_exits:
        reasons.append(
            "Outbound interfaces exist, but none match the requested protocol/operation."
        )
    if target_node is not None and not matching_entries:
        reasons.append("The target has no compatible discovered entrypoint.")
    if matching_exits and matching_entries and not related_edges:
        reasons.append("Both endpoints exist, but alias/contract relinking produced no dependency.")
    if not reasons and related_edges:
        reasons.append(
            "A related dependency exists; inspect candidates for its exact target/status."
        )
    return {
        "source": _node_summary(source_node),
        "target": _node_summary(target_node),
        "query": {"protocol": protocol, "operation": operation},
        "matching_exitpoints": [_node_summary(item) for item in matching_exits[:100]],
        "matching_entrypoints": [_node_summary(item) for item in matching_entries[:100]],
        "candidate_edges": [item.model_dump(mode="json") for item in related_edges[:100]],
        "likely_reasons": reasons,
        "next_checks": [
            "Inspect ExitPoint evidence and target_hint.",
            "Check whether the target entrypoint operation normalizes to the same contract.",
            "Run the case with mode=gigacode and verify_all=true if static evidence is weak.",
        ],
    }


def compare_graphs(before: GraphSnapshot, after: GraphSnapshot) -> dict[str, Any]:
    before_nodes = {item.id for item in before.nodes}
    after_nodes = {item.id for item in after.nodes}
    before_edges = {item.id for item in before.edges}
    after_edges = {item.id for item in after.edges}
    before_evidence = {item.id for item in before.evidence}
    after_evidence = {item.id for item in after.evidence}
    return {
        "schema_version": 1,
        "before": before.stats(),
        "after": after.stats(),
        "nodes": _set_delta(before_nodes, after_nodes),
        "edges": _set_delta(before_edges, after_edges),
        "evidence": _set_delta(before_evidence, after_evidence),
    }


def _validate_expectations(
    graph: GraphSnapshot,
    case: GraphLabCase,
    failures: list[dict[str, Any]],
) -> None:
    counts = Counter(item.type for item in graph.nodes)
    dependency_count = sum(item.type == "DEPENDS_ON" for item in graph.edges)
    expected = case.expectations.counts
    _minimum(failures, "services", counts["Service"], expected.min_services)
    _minimum(failures, "entrypoints", counts["EntryPoint"], expected.min_entrypoints)
    _minimum(failures, "exitpoints", counts["ExitPoint"], expected.min_exitpoints)
    _minimum(failures, "dependencies", dependency_count, expected.min_dependencies)
    if expected.max_issues is not None and len(graph.issues) > expected.max_issues:
        failures.append(
            _finding(
                "count-above-maximum",
                f"issues: actual={len(graph.issues)}, maximum={expected.max_issues}",
            )
        )
    services = [item for item in graph.nodes if item.type == "Service"]
    for service_target in case.expectations.services:
        try:
            service = _resolve_service(services, service_target.service)
        except KeyError:
            failures.append(_finding("missing-service", service_target.service))
            continue
        entries = sum(
            item.type == "EntryPoint" and item.service_id == service.service_id
            for item in graph.nodes
        )
        exits = sum(
            item.type == "ExitPoint" and item.service_id == service.service_id
            for item in graph.nodes
        )
        _minimum(
            failures,
            f"{service_target.service}.entrypoints",
            entries,
            service_target.min_entrypoints,
        )
        _minimum(
            failures,
            f"{service_target.service}.exitpoints",
            exits,
            service_target.min_exitpoints,
        )
    for edge_target in case.expectations.required_edges:
        actual = len(_matching_edges(graph, edge_target))
        if actual < edge_target.minimum:
            failures.append(
                _finding(
                    "missing-required-edge",
                    f"{edge_target.source} -> {edge_target.target or '*'}: "
                    f"actual={actual}, minimum={edge_target.minimum}",
                    expectation=edge_target.model_dump(mode="json"),
                )
            )
    for edge_target in case.expectations.forbidden_edges:
        actual = len(_matching_edges(graph, edge_target))
        if actual:
            failures.append(
                _finding(
                    "forbidden-edge",
                    f"{edge_target.source} -> {edge_target.target or '*'}: actual={actual}",
                    expectation=edge_target.model_dump(mode="json"),
                )
            )


def _matching_edges(graph: GraphSnapshot, target: EdgeTarget) -> list[GraphEdge]:
    nodes = {item.id: item for item in graph.nodes}
    services = [item for item in graph.nodes if item.type == "Service"]
    try:
        source = _resolve_service(services, target.source)
        expected_target = _resolve_service(services, target.target) if target.target else None
    except KeyError:
        return []
    matches: list[GraphEdge] = []
    for edge in graph.edges:
        if edge.type != "DEPENDS_ON":
            continue
        source_node = nodes.get(edge.source)
        target_node = nodes.get(edge.target)
        edge_source_service = source_node.service_id if source_node else None
        edge_target_service = target_node.service_id if target_node else None
        if edge_source_service != source.service_id:
            continue
        if expected_target is not None and edge_target_service != expected_target.service_id:
            continue
        if (
            target.protocol
            and str(edge.metadata.get("protocol", "")).upper() != target.protocol.upper()
        ):
            continue
        operation = str(edge.metadata.get("operation") or edge.label)
        if (
            target.operation_contains
            and target.operation_contains.casefold() not in operation.casefold()
        ):
            continue
        matches.append(edge)
    return matches


def _resolve_service(services: list[GraphNode], value: str | None) -> GraphNode:
    if not value:
        raise KeyError("Service identifier is empty")
    normalized = value.casefold()
    matches = []
    for service in services:
        aliases = {
            service.id,
            service.service_id or "",
            service.label,
            *(str(item) for item in service.metadata.get("aliases", [])),
        }
        if any(alias.casefold() == normalized for alias in aliases):
            matches.append(service)
    if len(matches) != 1:
        raise KeyError(f"Service {value!r} resolved to {len(matches)} nodes")
    return matches[0]


def _node_contract_matches(node: GraphNode, protocol: str | None, operation: str | None) -> bool:
    node_protocol = str(node.metadata.get("protocol") or node.metadata.get("trigger_type") or "")
    node_operation = str(node.metadata.get("operation") or node.label)
    return (not protocol or node_protocol.upper() == protocol.upper()) and (
        not operation or operation.casefold() in node_operation.casefold()
    )


def _check_evidence_refs(
    owner: str,
    references: list[str],
    evidence_ids: set[str],
    failures: list[dict[str, Any]],
) -> None:
    for reference in references:
        if reference not in evidence_ids:
            failures.append(
                _finding("dangling-evidence", f"{owner} references missing {reference}")
            )


def _minimum(
    failures: list[dict[str, Any]],
    label: str,
    actual: int,
    minimum: int,
) -> None:
    if actual < minimum:
        failures.append(
            _finding(
                "count-below-minimum",
                f"{label}: actual={actual}, minimum={minimum}",
            )
        )


def _finding(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "details": details}


def _node_summary(node: GraphNode | None) -> dict[str, Any] | None:
    if node is None:
        return None
    return {
        "id": node.id,
        "type": node.type,
        "label": node.label,
        "service_id": node.service_id,
        "metadata": node.metadata,
        "evidence_ids": node.evidence_ids,
    }


def _set_delta(before: set[str], after: set[str]) -> dict[str, Any]:
    added = sorted(after - before)
    removed = sorted(before - after)
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "unchanged_count": len(before & after),
        "added": added[:1000],
        "removed": removed[:1000],
        "truncated": len(added) > 1000 or len(removed) > 1000,
    }
