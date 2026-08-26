"""Read-only query layer over a versioned repository graph snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Iterable
from typing import Any

from gigacode_graph.models import (
    Confidence,
    EdgeType,
    Evidence,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    NodeType,
)
from gigacode_graph.store import GraphStore

_SERVICE_VIEW_TYPES = {"Service", "ExternalSystem"}
_CONFIDENCE_RANK = {"UNRESOLVED": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "DECLARED": 4}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _tokens(value: str) -> set[str]:
    return {item for item in re.split(r"[^a-zA-Z0-9_Ѐ-ӿ-]+", value.lower()) if item}


def _aggregate_service_edges(edges: list[GraphEdge]) -> list[GraphEdge]:
    """Collapse parallel API calls into one stable service-view link per protocol."""
    groups: dict[tuple[str, str, EdgeType, str], list[GraphEdge]] = {}
    for edge in edges:
        protocol = str(edge.metadata.get("protocol") or "UNKNOWN").upper()
        groups.setdefault((edge.source, edge.target, edge.type, protocol), []).append(edge)

    aggregated: list[GraphEdge] = []
    for (source, target, edge_type, protocol), group in sorted(groups.items()):
        ordered = sorted(group, key=lambda item: item.id)
        if len(ordered) == 1:
            aggregated.append(ordered[0])
            continue
        weakest = min(ordered, key=lambda item: _CONFIDENCE_RANK[item.confidence])
        operations = sorted(
            {
                str(
                    item.metadata.get("operation")
                    or item.metadata.get("topic")
                    or item.label
                )
                for item in ordered
                if item.metadata.get("operation") or item.metadata.get("topic") or item.label
            }
        )
        material = "\x1f".join((source, target, edge_type, protocol))
        edge_id = "service-link:" + hashlib.sha256(material.encode()).hexdigest()[:20]
        status = (
            "unresolved"
            if any(item.status == "unresolved" for item in ordered)
            else "confirmed"
            if all(item.status == "confirmed" for item in ordered)
            else "inferred"
        )
        origin = (
            "static+gigacode"
            if any(item.origin == "static+gigacode" for item in ordered)
            else "gigacode"
            if any(item.origin == "gigacode" for item in ordered)
            else "declared"
            if all(item.origin == "declared" for item in ordered)
            else "static"
        )
        verified_values = [item.verified_at for item in ordered if item.verified_at is not None]
        aggregated.append(
            weakest.model_copy(
                update={
                    "id": edge_id,
                    "label": f"{protocol} · {len(operations)} operations",
                    "confidence": weakest.confidence,
                    "status": status,
                    "origin": origin,
                    "verified_at": max(verified_values) if verified_values else None,
                    "metadata": {
                        "protocol": protocol,
                        "operation_count": len(operations),
                        "operations": operations,
                        "confidence_counts": {
                            confidence: sum(
                                item.confidence == confidence for item in ordered
                            )
                            for confidence in _CONFIDENCE_RANK
                        },
                        "edge_ids": [item.id for item in ordered],
                    },
                    "evidence_ids": list(
                        dict.fromkeys(
                            evidence_id
                            for item in ordered
                            for evidence_id in item.evidence_ids
                        )
                    ),
                }
            )
        )
    return aggregated


def _display_node_payloads(nodes: list[GraphNode]) -> list[dict[str, Any]]:
    duplicate_labels: dict[str, int] = {}
    for node in nodes:
        if node.type == "Service":
            key = node.label.strip().lower()
            duplicate_labels[key] = duplicate_labels.get(key, 0) + 1
    payloads: list[dict[str, Any]] = []
    for node in nodes:
        payload = node.model_dump(mode="json")
        if node.type == "Service" and duplicate_labels.get(node.label.strip().lower(), 0) > 1:
            qualifier = str(node.metadata.get("module_path") or node.service_id or node.id)
            payload["label"] = f"{node.label} · {qualifier}"
            payload["metadata"] = {
                **payload["metadata"],
                "original_label": node.label,
                "display_disambiguated": True,
            }
        payloads.append(payload)
    return payloads


class GraphService:
    """Query graph facts without exposing storage details to MCP, HTTP, or CLI."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store
        self._snapshot = store.load()
        self._store_revision = store.revision()
        self._rebuild_indexes()

    @property
    def snapshot(self) -> GraphSnapshot:
        self._maybe_reload()
        return self._snapshot

    def reload(self) -> dict[str, Any]:
        self._snapshot = self._store.load()
        self._store_revision = self._store.revision()
        self._rebuild_indexes()
        return self._overview

    def _maybe_reload(self) -> None:
        revision = self._store.revision()
        if revision != self._store_revision:
            self._snapshot = self._store.load()
            self._store_revision = revision
            self._rebuild_indexes()

    def _rebuild_indexes(self) -> None:
        self._nodes = {node.id: node for node in self._snapshot.nodes}
        self._edges_by_source: dict[str, list[GraphEdge]] = {}
        self._edges_by_target: dict[str, list[GraphEdge]] = {}
        self._evidence = {item.id: item for item in self._snapshot.evidence}
        for edge in self._snapshot.edges:
            self._edges_by_source.setdefault(edge.source, []).append(edge)
            self._edges_by_target.setdefault(edge.target, []).append(edge)
        self._overview = self._build_overview_payload()

    def overview(self) -> dict[str, Any]:
        self._maybe_reload()
        return self._overview

    def _build_overview_payload(self) -> dict[str, Any]:
        payload = self._snapshot.stats()
        payload["services"] = [
            {
                "id": node.id,
                "service_id": node.service_id,
                "label": node.label,
                "owner": node.metadata.get("owner"),
                "repository": node.metadata.get("repository"),
                "catalog_name": node.metadata.get("catalog_name"),
            }
            for node in self._snapshot.nodes
            if node.type == "Service"
        ]
        payload["issues"] = [item.model_dump(mode="json") for item in self._snapshot.issues[:100]]
        return payload

    def graph(
        self,
        *,
        view: str = "services",
        service: str | None = None,
        depth: int = 1,
        limit: int = 3_000,
        node_types: list[NodeType] | None = None,
        edge_types: list[EdgeType] | None = None,
        confidences: list[Confidence] | None = None,
        connected_only: bool = False,
        include_rejected: bool = False,
    ) -> dict[str, Any]:
        self._maybe_reload()
        if view not in {"services", "full"}:
            raise ValueError("view must be services or full")
        if not 0 <= depth <= 10:
            raise ValueError("depth must be between 0 and 10")
        if not 1 <= limit <= 20_000:
            raise ValueError("limit must be between 1 and 20000")

        selected_services: set[str] | None = None
        focus_id: str | None = None
        if service:
            focus = self._resolve_service(service)
            focus_id = focus.id
            selected_services = self._service_neighbourhood(focus.id, depth)

        if view == "services":
            nodes = [
                node
                for node in self._snapshot.nodes
                if node.type in _SERVICE_VIEW_TYPES
                and (
                    selected_services is None
                    or node.id in selected_services
                    or any(
                        edge.source in selected_services and edge.target == node.id
                        for edge in self._snapshot.edges
                        if edge.type == "DEPENDS_ON"
                    )
                )
            ]
            node_ids = {node.id for node in nodes}
            edges = [
                edge
                for edge in self._snapshot.edges
                if edge.type == "DEPENDS_ON" and edge.source in node_ids and edge.target in node_ids
            ]
        else:
            nodes = list(self._snapshot.nodes)
            edges = list(self._snapshot.edges)
            if selected_services is not None:
                service_names = {
                    self._nodes[item].service_id
                    for item in selected_services
                    if item in self._nodes and self._nodes[item].service_id
                }
                nodes = [
                    node
                    for node in nodes
                    if node.id in selected_services or node.service_id in service_names
                ]
                node_ids = {node.id for node in nodes}
                for edge in edges:
                    if edge.source in node_ids and edge.target in self._nodes:
                        node_ids.add(edge.target)
                    if edge.target in node_ids and edge.source in self._nodes:
                        node_ids.add(edge.source)
                nodes = [node for node in self._snapshot.nodes if node.id in node_ids]
                edges = [
                    edge for edge in edges if edge.source in node_ids and edge.target in node_ids
                ]

        if not include_rejected:
            edges = [edge for edge in edges if edge.status != "rejected"]
        if view == "services":
            edges = _aggregate_service_edges(edges)
        if edge_types:
            allowed_edge_types = set(edge_types)
            edges = [edge for edge in edges if edge.type in allowed_edge_types]
        if confidences:
            allowed_confidences = set(confidences)
            edges = [edge for edge in edges if edge.confidence in allowed_confidences]
        if node_types:
            allowed_node_types = set(node_types)
            nodes = [node for node in nodes if node.type in allowed_node_types]
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in edges if edge.source in node_ids and edge.target in node_ids]
        if connected_only:
            connected_ids = {
                endpoint
                for edge in edges
                for endpoint in (edge.source, edge.target)
            }
            nodes = [node for node in nodes if node.id in connected_ids]

        truncated = len(nodes) > limit
        nodes = nodes[:limit]
        node_ids = {node.id for node in nodes}
        edges = [edge for edge in edges if edge.source in node_ids and edge.target in node_ids]
        return {
            "schema_version": self._snapshot.schema_version,
            "snapshot_id": self._snapshot.snapshot_id,
            "analysis_mode": self._snapshot.analysis_mode,
            "verification": self._snapshot.verification,
            "generated_at": self._snapshot.generated_at.isoformat(),
            "view": view,
            "focus": focus_id,
            "truncated": truncated,
            "nodes": _display_node_payloads(nodes),
            "edges": [_jsonable(edge) for edge in edges],
        }

    def search(
        self,
        query: str,
        *,
        node_types: list[NodeType] | None = None,
        service: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        self._maybe_reload()
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        service_id = self._resolve_service(service).service_id if service else None
        query_tokens = _tokens(query)
        lowered = query.lower()
        hits: list[tuple[float, GraphNode]] = []
        for node in self._snapshot.nodes:
            if node_types and node.type not in node_types:
                continue
            if service_id and node.service_id not in {service_id, None}:
                continue
            metadata_text = json.dumps(node.metadata, ensure_ascii=False, default=str).lower()
            haystack = f"{node.id} {node.label} {metadata_text}".lower()
            overlap = len(query_tokens & _tokens(haystack))
            if lowered not in haystack and overlap == 0:
                continue
            score = overlap / max(1, len(query_tokens))
            if lowered in node.label.lower():
                score += 2.0
            elif lowered in node.id.lower():
                score += 1.0
            hits.append((score, node))
        hits.sort(key=lambda item: (-item[0], item[1].type, item[1].label))
        results = []
        for score, node in hits[:limit]:
            item = node.model_dump(mode="json")
            item["score"] = round(score, 4)
            item["evidence"] = self._evidence_payload(node.evidence_ids, limit=3)
            results.append(item)
        return {"query": query, "result_count": len(results), "results": results}

    def service_details(self, service: str) -> dict[str, Any]:
        self._maybe_reload()
        node = self._resolve_service(service)
        service_id = node.service_id
        dependencies = self.dependencies(service, direction="both", depth=1)
        operations = self.business_operations(service, limit=500)
        model = self.data_model(service=service, limit=1_000)
        related_types = {"EntryPoint", "ExitPoint", "Event", "ExternalSystem"}
        related = [
            item.model_dump(mode="json")
            for item in self._snapshot.nodes
            if item.service_id == service_id and item.type in related_types
        ]
        return {
            "service": node.model_dump(mode="json"),
            "dependencies": dependencies,
            "business": operations,
            "data_model": model,
            "related": related,
            "evidence": self._evidence_payload(node.evidence_ids),
        }

    def dependencies(
        self,
        service: str,
        *,
        direction: str = "outgoing",
        depth: int = 1,
    ) -> dict[str, Any]:
        self._maybe_reload()
        if direction not in {"outgoing", "incoming", "both"}:
            raise ValueError("direction must be outgoing, incoming, or both")
        if not 1 <= depth <= 10:
            raise ValueError("depth must be between 1 and 10")
        root = self._resolve_service(service)
        queue: deque[tuple[str, int, list[str]]] = deque([(root.id, 0, [root.id])])
        best_depth = {root.id: 0}
        edge_ids: set[str] = set()
        paths: list[dict[str, Any]] = []
        while queue:
            current, current_depth, path = queue.popleft()
            if current_depth >= depth:
                continue
            candidates: list[tuple[GraphEdge, str]] = []
            if direction in {"outgoing", "both"}:
                candidates.extend(
                    (edge, edge.target)
                    for edge in self._edges_by_source.get(current, [])
                    if edge.type == "DEPENDS_ON"
                    and self._nodes.get(edge.target) is not None
                    and self._nodes[edge.target].type in _SERVICE_VIEW_TYPES
                )
            if direction in {"incoming", "both"}:
                candidates.extend(
                    (edge, edge.source)
                    for edge in self._edges_by_target.get(current, [])
                    if edge.type == "DEPENDS_ON"
                    and self._nodes.get(edge.source) is not None
                    and self._nodes[edge.source].type in _SERVICE_VIEW_TYPES
                )
            for edge, neighbour in candidates:
                edge_ids.add(edge.id)
                next_path = [*path, neighbour]
                paths.append(
                    {
                        "depth": current_depth + 1,
                        "nodes": next_path,
                        "edge": edge.model_dump(mode="json"),
                    }
                )
                next_depth = current_depth + 1
                if next_depth < best_depth.get(neighbour, depth + 1):
                    best_depth[neighbour] = next_depth
                    queue.append((neighbour, next_depth, next_path))
        node_ids = set(best_depth)
        for edge_id in edge_ids:
            edge = next(item for item in self._snapshot.edges if item.id == edge_id)
            node_ids.update({edge.source, edge.target})
        return {
            "root": root.model_dump(mode="json"),
            "direction": direction,
            "depth": depth,
            "nodes": [
                self._nodes[node_id].model_dump(mode="json")
                for node_id in sorted(node_ids)
                if node_id in self._nodes
            ],
            "edges": [
                edge.model_dump(mode="json") for edge in self._snapshot.edges if edge.id in edge_ids
            ],
            "paths": paths,
        }

    def business_operations(self, service: str, *, limit: int = 100) -> dict[str, Any]:
        self._maybe_reload()
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        root = self._resolve_service(service)
        operations = [
            node
            for node in self._snapshot.nodes
            if node.type == "BusinessOperation" and node.service_id == root.service_id
        ][:limit]
        operation_ids = {item.id for item in operations}
        related_ids = set(operation_ids)
        edge_ids: set[str] = set()
        queue: deque[str] = deque(operation_ids)
        traversable = {
            "TRIGGERED_BY",
            "HANDLED_BY",
            "CALLS",
            "GUARDED_BY",
            "READS",
            "WRITES",
            "PUBLISHES",
            "CONSUMES",
            "EXITS_VIA",
            "DEPENDS_ON",
        }
        while queue:
            source = queue.popleft()
            for edge in self._edges_by_source.get(source, []):
                if edge.type not in traversable:
                    continue
                edge_ids.add(edge.id)
                target = self._nodes.get(edge.target)
                if target is None:
                    continue
                if target.id not in related_ids:
                    related_ids.add(target.id)
                    if target.type in {"EntryPoint", "ExitPoint", "CodeSymbol"}:
                        queue.append(target.id)
        for operation_id in operation_ids:
            for edge in self._edges_by_target.get(operation_id, []):
                if edge.type == "IMPLEMENTS":
                    edge_ids.add(edge.id)
                    related_ids.add(edge.source)
        edges = [edge for edge in self._snapshot.edges if edge.id in edge_ids]
        related = [
            node
            for node in self._snapshot.nodes
            if node.id in related_ids and node.id not in operation_ids
        ]
        evidence_ids = self._collect_evidence([*operations, *related], edges)
        return {
            "service": root.model_dump(mode="json"),
            "operation_count": len(operations),
            "operations": [item.model_dump(mode="json") for item in operations],
            "related_nodes": [item.model_dump(mode="json") for item in related],
            "edges": [item.model_dump(mode="json") for item in edges],
            "evidence": self._evidence_payload(evidence_ids, limit=100),
        }

    def data_model(
        self,
        *,
        service: str | None = None,
        table: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        self._maybe_reload()
        if not service and not table:
            raise ValueError("service or table is required")
        if not 1 <= limit <= 5_000:
            raise ValueError("limit must be between 1 and 5000")
        service_node = self._resolve_service(service) if service else None
        table_lower = table.lower() if table else None
        seed_nodes = [
            node
            for node in self._snapshot.nodes
            if node.type in {"DomainEntity", "Table", "Column"}
            and (service_node is None or node.service_id == service_node.service_id)
            and (
                table_lower is None
                or table_lower in node.id.lower()
                or table_lower in node.label.lower()
                or table_lower in json.dumps(node.metadata, ensure_ascii=False).lower()
            )
        ][:limit]
        node_ids = {node.id for node in seed_nodes}
        model_edge_types = {
            "DECLARES_ENTITY",
            "MANAGES_SCHEMA",
            "MAPS_TO",
            "HAS_COLUMN",
            "READS",
            "WRITES",
        }
        edges = [
            edge
            for edge in self._snapshot.edges
            if edge.type in model_edge_types
            and (edge.source in node_ids or edge.target in node_ids)
        ]
        related_ids = node_ids | {edge.source for edge in edges} | {edge.target for edge in edges}
        nodes = [node for node in self._snapshot.nodes if node.id in related_ids]
        evidence_ids = self._collect_evidence(nodes, edges)
        return {
            "service": service_node.model_dump(mode="json") if service_node else None,
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
            "evidence": self._evidence_payload(evidence_ids, limit=200),
        }

    def evidence(self, ids: list[str]) -> dict[str, Any]:
        self._maybe_reload()
        if not ids:
            raise ValueError("at least one evidence id is required")
        if len(ids) > 100:
            raise ValueError("at most 100 evidence ids can be requested")
        missing = [item for item in ids if item not in self._evidence]
        return {"items": self._evidence_payload(ids, limit=100), "missing": missing}

    def _resolve_service(self, value: str | None) -> GraphNode:
        if value is None or not value.strip():
            raise ValueError("service must not be empty")
        needle = value.strip().lower()
        direct = self._nodes.get(value) or self._nodes.get(f"service:{value}")
        if direct is not None and direct.type == "Service":
            return direct
        matches = []
        for node in self._snapshot.nodes:
            if node.type != "Service":
                continue
            aliases = [str(item).lower() for item in node.metadata.get("aliases", [])]
            identities = {
                node.id.lower(),
                node.label.lower(),
                (node.service_id or "").lower(),
                *aliases,
            }
            if needle in identities:
                matches.append(node)
        if not matches:
            raise KeyError(f"Unknown service: {value}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous service: {value}")
        return matches[0]

    def _service_neighbourhood(self, root: str, depth: int) -> set[str]:
        selected = {root}
        queue: deque[tuple[str, int]] = deque([(root, 0)])
        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            adjacent = [
                edge.target
                for edge in self._edges_by_source.get(current, [])
                if edge.type == "DEPENDS_ON" and edge.target.startswith("service:")
            ]
            adjacent.extend(
                edge.source
                for edge in self._edges_by_target.get(current, [])
                if edge.type == "DEPENDS_ON" and edge.source.startswith("service:")
            )
            for neighbour in adjacent:
                if neighbour not in selected:
                    selected.add(neighbour)
                    queue.append((neighbour, current_depth + 1))
        return selected

    def _collect_evidence(
        self, nodes: Iterable[GraphNode], edges: Iterable[GraphEdge]
    ) -> list[str]:
        evidence_ids = [evidence_id for item in nodes for evidence_id in item.evidence_ids]
        evidence_ids.extend(evidence_id for item in edges for evidence_id in item.evidence_ids)
        return list(dict.fromkeys(evidence_ids))

    def _evidence_payload(self, ids: Iterable[str], *, limit: int = 50) -> list[dict[str, Any]]:
        items: list[Evidence] = []
        for evidence_id in list(dict.fromkeys(ids))[:limit]:
            item = self._evidence.get(evidence_id)
            if item is not None:
                items.append(item)
        return [item.model_dump(mode="json") for item in items]
