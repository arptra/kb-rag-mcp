"""Project the source scanner output into a compact, deterministic service map."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphNode, GraphSnapshot
from gigacode_graph.scanner import RepositoryScanner
from service_map.models import (
    InterfaceKind,
    ServiceDependency,
    ServiceInterface,
    ServiceMapEvidence,
    ServiceMapIssue,
    ServiceMapSnapshot,
    ServiceRecord,
)

_INTERFACE_KINDS = {"HTTP", "KAFKA", "SCHEDULED", "GRPC", "CLI"}


@dataclass(frozen=True, slots=True)
class RepositoryInput:
    path: Path
    name: str
    source_url: str | None = None
    commit: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceMapBuildResult:
    graph: GraphSnapshot
    service_map: ServiceMapSnapshot


def _interface_kind(value: object) -> InterfaceKind:
    normalized = str(value or "UNKNOWN").upper()
    if normalized in _INTERFACE_KINDS:
        return cast(InterfaceKind, normalized)
    return "UNKNOWN"


class ServiceMapBuilder:
    """Scan repositories once and produce both graph and lightweight map snapshots."""

    def __init__(self, settings: GraphSettings) -> None:
        self._settings = settings

    def build(self, repositories: list[RepositoryInput]) -> ServiceMapBuildResult:
        if not repositories:
            return ServiceMapBuildResult(GraphSnapshot(), ServiceMapSnapshot())

        graph = RepositoryScanner(self._settings).scan(
            sorted((item.path.resolve() for item in repositories), key=str)
        )
        return self.from_graph(graph, repositories)

    def from_graph(
        self,
        graph: GraphSnapshot,
        repositories: list[RepositoryInput],
    ) -> ServiceMapBuildResult:
        """Build a map from an existing graph without rescanning repository files."""
        by_path = {str(item.path.resolve()): item for item in repositories}
        projected_graph = graph.model_copy(
            update={
                "nodes": [self._catalog_node(node, by_path) for node in graph.nodes],
            },
            deep=True,
        )
        return ServiceMapBuildResult(
            graph=projected_graph,
            service_map=self._project(projected_graph, by_path),
        )

    @staticmethod
    def _catalog_node(
        node: GraphNode,
        repositories: dict[str, RepositoryInput],
    ) -> GraphNode:
        if node.type not in {"Repository", "Service"}:
            return node
        repository = repositories.get(str(node.metadata.get("path")))
        if repository is None:
            return node
        return node.model_copy(
            update={
                "label": repository.name,
                "metadata": {**node.metadata, "catalog_name": repository.name},
            }
        )

    def _project(
        self,
        graph: GraphSnapshot,
        repositories: dict[str, RepositoryInput],
    ) -> ServiceMapSnapshot:
        nodes = {item.id: item for item in graph.nodes}
        entrypoints: dict[str, list[ServiceInterface]] = {}
        outbound: dict[str, list[ServiceInterface]] = {}
        evidence_ids: set[str] = set()

        outgoing_targets: dict[str, GraphNode] = {}
        for edge in graph.edges:
            target = nodes.get(edge.target)
            if target is not None and edge.source not in outgoing_targets:
                outgoing_targets[edge.source] = target

        for node in graph.nodes:
            if node.type == "EntryPoint" and node.service_id:
                interface = self._entrypoint(node)
                entrypoints.setdefault(node.service_id, []).append(interface)
                evidence_ids.update(interface.evidence_ids)
            elif node.type == "ExitPoint" and node.service_id:
                interface = self._exitpoint(node, outgoing_targets.get(node.id))
                outbound.setdefault(node.service_id, []).append(interface)
                evidence_ids.update(interface.evidence_ids)

        services: list[ServiceRecord] = []
        for node in graph.nodes:
            if node.type != "Service" or not node.service_id:
                continue
            path = str(node.metadata.get("path") or "")
            repository = repositories.get(path)
            aliases = {
                str(item)
                for item in node.metadata.get("aliases", [])
                if str(item).strip()
            }
            aliases.update({node.service_id, node.label})
            services.append(
                ServiceRecord(
                    id=node.service_id,
                    name=node.label,
                    aliases=sorted(aliases),
                    repository=repository.name if repository else str(
                        node.metadata.get("repository") or node.label
                    ),
                    repository_path=path,
                    source_url=(
                        repository.source_url
                        if repository
                        else self._optional_string(node.metadata.get("source"))
                    ),
                    commit=(
                        repository.commit
                        if repository and repository.commit
                        else self._optional_string(node.metadata.get("commit"))
                    ),
                    owner=self._optional_string(node.metadata.get("owner")),
                    entrypoints=sorted(
                        entrypoints.get(node.service_id, []), key=lambda item: item.id
                    ),
                    outbound_interfaces=sorted(
                        outbound.get(node.service_id, []), key=lambda item: item.id
                    ),
                )
            )

        dependencies = self._dependencies(graph, nodes)
        for dependency in dependencies:
            evidence_ids.update(dependency.evidence_ids)
        evidence = [
            ServiceMapEvidence.model_validate(item.model_dump())
            for item in graph.evidence
            if item.id in evidence_ids
        ]
        issues = [ServiceMapIssue.model_validate(item.model_dump()) for item in graph.issues]
        return ServiceMapSnapshot(
            generated_at=graph.generated_at,
            services=sorted(services, key=lambda item: (item.name.lower(), item.id)),
            dependencies=dependencies,
            evidence=sorted(evidence, key=lambda item: item.id),
            issues=issues,
        )

    @staticmethod
    def _entrypoint(node: GraphNode) -> ServiceInterface:
        kind = _interface_kind(node.metadata.get("trigger_type"))
        operation = str(node.metadata.get("operation") or node.label)
        return ServiceInterface(
            id=node.id,
            kind=kind,
            direction="inbound",
            operation=operation,
            description=f"{kind} {operation} accepted by {node.service_id}",
            evidence_ids=node.evidence_ids,
        )

    @staticmethod
    def _exitpoint(node: GraphNode, target: GraphNode | None) -> ServiceInterface:
        kind = _interface_kind(node.metadata.get("protocol"))
        operation = str(
            node.metadata.get("operation") or node.metadata.get("topic") or node.label
        )
        target_hint = ServiceMapBuilder._optional_string(node.metadata.get("target_hint"))
        if target_hint is None and target is not None:
            target_hint = target.label
        description = f"{kind} {operation} called by {node.service_id}"
        if target_hint:
            description = f"{description}; target {target_hint}"
        return ServiceInterface(
            id=node.id,
            kind=kind,
            direction="outbound",
            operation=operation,
            target_hint=target_hint,
            description=description,
            evidence_ids=node.evidence_ids,
        )

    @staticmethod
    def _dependencies(
        graph: GraphSnapshot,
        nodes: dict[str, GraphNode],
    ) -> list[ServiceDependency]:
        dependencies: list[ServiceDependency] = []
        for edge in graph.edges:
            source = nodes.get(edge.source)
            target = nodes.get(edge.target)
            if edge.type != "DEPENDS_ON" or source is None or source.type != "Service":
                continue
            if target is None or target.type not in {"Service", "ExternalSystem"}:
                continue
            target_service_id = target.service_id if target.type == "Service" else None
            target_hint = str(
                target.metadata.get("target_hint")
                or target_service_id
                or target.label
            )
            protocol = _interface_kind(edge.metadata.get("protocol"))
            operation = str(
                edge.metadata.get("operation")
                or edge.metadata.get("topic")
                or edge.label
                or target_hint
            )
            dependencies.append(
                ServiceDependency(
                    id=edge.id,
                    source_service_id=source.service_id or source.id.removeprefix("service:"),
                    target_service_id=target_service_id,
                    target_hint=target_hint,
                    protocol=protocol,
                    operation=operation,
                    confidence=edge.confidence,
                    resolved=target_service_id is not None,
                    evidence_ids=edge.evidence_ids,
                )
            )
        return sorted(dependencies, key=lambda item: item.id)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None
