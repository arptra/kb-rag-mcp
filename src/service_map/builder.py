"""Project the source scanner output into a compact, deterministic service map."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphNode, GraphSnapshot, ScanIssue
from gigacode_graph.scanner import (
    RepositoryScanner,
    ScanTarget,
    merge_and_relink_snapshots,
)
from gigacode_graph.store import JsonGraphStore
from service_map.layout import (
    BuildSystem,
    LayoutIssue,
    ModuleLayout,
    ModuleRole,
    ModuleState,
    RepositoryLayout,
    RepositoryLayoutAnalyzer,
)
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
_ANALYZER_VERSION = "service-map-v3-java-kotlin-inventory"


@dataclass(frozen=True, slots=True)
class RepositoryInput:
    path: Path
    name: str
    source_url: str | None = None
    commit: str | None = None
    excluded_module_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceMapBuildResult:
    graph: GraphSnapshot
    service_map: ServiceMapSnapshot
    partial: bool = False


def _interface_kind(value: object) -> InterfaceKind:
    normalized = str(value or "UNKNOWN").upper()
    if normalized in _INTERFACE_KINDS:
        return cast(InterfaceKind, normalized)
    return "UNKNOWN"


class ServiceMapBuilder:
    """Scan repositories once and produce both graph and lightweight map snapshots."""

    def __init__(self, settings: GraphSettings) -> None:
        self._settings = settings

    def build(
        self,
        repositories: list[RepositoryInput],
        *,
        progress: Callable[[str], None] | None = None,
        checkpoint: Callable[[ServiceMapBuildResult], None] | None = None,
        force_service_ids: set[str] | None = None,
        force_all: bool = False,
    ) -> ServiceMapBuildResult:
        if not repositories:
            return ServiceMapBuildResult(GraphSnapshot(), ServiceMapSnapshot())

        build_started_at = time.monotonic()
        ordered = sorted(repositories, key=lambda value: str(value.path.resolve()))
        layouts: list[tuple[RepositoryInput, RepositoryLayout]] = []
        for position, item in enumerate(ordered, start=1):
            layout_started_at = time.monotonic()
            if progress is not None:
                progress(
                    f"Layout [{position}/{len(ordered)}]: {item.name}; path={item.path.resolve()}"
                )
            layout_cache_path = self._layout_cache_path(item)
            layout = self._load_layout(layout_cache_path) if layout_cache_path else None
            if layout is None:
                layout = RepositoryLayoutAnalyzer().discover(item.path)
                if layout_cache_path is not None:
                    self._save_layout(layout_cache_path, layout)
            elif progress is not None:
                assert layout_cache_path is not None
                progress(f"Layout cache hit: {item.name}; path={layout_cache_path.name}")
            layouts.append((item, layout))
            if progress is not None:
                progress(
                    f"Layout ready: {item.name}; modules={len(layout.modules)}; "
                    f"openspec={len(layout.openspec_roots)}; issues={len(layout.issues)}; "
                    f"elapsed={time.monotonic() - layout_started_at:.3f}s"
                )
        targets, issues = self._scan_targets(layouts)
        if progress is not None:
            active = sum(target.module_state == "active" for target in targets)
            empty = sum(target.module_state == "empty" for target in targets)
            unsupported = sum(target.module_state == "unsupported" for target in targets)
            progress(
                f"Scan plan: modules={len(targets)}; active={active}; empty={empty}; "
                f"unsupported={unsupported}; layout_issues={len(issues)}"
            )
        if targets and checkpoint is not None:
            layout_graph = RepositoryScanner(self._settings).discover_targets(
                targets,
                progress=progress,
            )
            if issues:
                layout_graph = layout_graph.model_copy(
                    update={"issues": [*layout_graph.issues, *issues]}
                )
            layout_result = self.from_graph(layout_graph, repositories)
            checkpoint(layout_result)

        def publish_scan_checkpoint(snapshot: GraphSnapshot) -> None:
            if checkpoint is None:
                return
            if issues:
                snapshot = snapshot.model_copy(update={"issues": [*snapshot.issues, *issues]})
            checkpoint(self.from_graph(snapshot, repositories))

        module_snapshots: list[GraphSnapshot] = []
        last_checkpoint_at = time.monotonic()
        forced = force_service_ids or set()
        for position, target in enumerate(targets, start=1):
            cache_path = self._cache_path(target)
            use_cache = not force_all and target.service_id not in forced
            snapshot = self._load_cached(cache_path) if use_cache else None
            if snapshot is not None:
                if progress is not None:
                    progress(
                        f"Module cache hit [{position}/{len(targets)}]: "
                        f"{target.service_id}; path={cache_path.name}"
                    )
            else:
                scan_started_at = time.monotonic()
                if progress is not None:
                    reason = "forced" if not use_cache else "miss"
                    progress(
                        f"Module cache {reason} [{position}/{len(targets)}]: {target.service_id}"
                    )
                snapshot = RepositoryScanner(self._settings).scan_targets(
                    [target],
                    progress=progress,
                )
                JsonGraphStore(cache_path).save(snapshot)
                if progress is not None:
                    progress(
                        f"Module cached: {target.service_id}; "
                        f"elapsed={time.monotonic() - scan_started_at:.3f}s"
                    )
            module_snapshots.append(snapshot)
            if checkpoint is not None and time.monotonic() - last_checkpoint_at >= 5:
                publish_scan_checkpoint(merge_and_relink_snapshots(module_snapshots))
                last_checkpoint_at = time.monotonic()
        graph = merge_and_relink_snapshots(module_snapshots) if targets else GraphSnapshot()
        if issues:
            graph = graph.model_copy(update={"issues": [*graph.issues, *issues]})
        result = self.from_graph(graph, repositories)
        if progress is not None:
            progress(
                f"Snapshot ready: services={len(result.service_map.services)}; "
                f"nodes={len(result.graph.nodes)}; edges={len(result.graph.edges)}; "
                f"evidence={len(result.graph.evidence)}; issues={len(result.graph.issues)}; "
                f"elapsed={time.monotonic() - build_started_at:.3f}s"
            )
        return result

    def _cache_path(self, target: ScanTarget) -> Path:
        digest = hashlib.sha256()
        digest.update(_ANALYZER_VERSION.encode())
        digest.update(str(self._settings.call_depth).encode())
        for value in (
            target.repository_name,
            target.service_id,
            target.display_name,
            target.owner,
            target.module_path,
            target.module_state,
            target.build_system,
            target.commit,
            *target.aliases,
            *target.component_paths,
        ):
            digest.update(str(value or "").encode())
            digest.update(b"\x00")
        repository = target.repository_path or target.path
        paths = sorted({*(target.source_files or ()), *(target.resource_files or ())})
        for path in paths:
            try:
                relative = path.relative_to(repository).as_posix()
                stat = path.stat()
            except (OSError, ValueError):
                continue
            digest.update(relative.encode())
            digest.update(f"\x00{stat.st_size}\x00{stat.st_mtime_ns}\x00".encode())
        return self._settings.module_cache_path / f"{digest.hexdigest()}.json"

    def _layout_cache_path(self, repository: RepositoryInput) -> Path | None:
        if not repository.commit:
            return None
        digest = hashlib.sha256(
            f"{_ANALYZER_VERSION}\x00{repository.path.resolve()}\x00{repository.commit}".encode()
        ).hexdigest()
        return self._settings.module_cache_path / "layouts" / f"{digest}.json"

    @staticmethod
    def _save_layout(path: Path, layout: RepositoryLayout) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "root": str(layout.root),
            "openspec_roots": [str(item) for item in layout.openspec_roots],
            "issues": [{"file": item.file, "message": item.message} for item in layout.issues],
            "modules": [
                {
                    "root": str(item.root),
                    "relative_path": item.relative_path,
                    "service_id": item.service_id,
                    "display_name": item.display_name,
                    "owner": item.owner,
                    "aliases": list(item.aliases),
                    "build_system": item.build_system,
                    "state": item.state,
                    "role": item.role,
                    "declared": item.declared,
                    "source_roots": [str(value) for value in item.source_roots],
                    "resource_roots": [str(value) for value in item.resource_roots],
                    "openspec_roots": [str(value) for value in item.openspec_roots],
                    "source_files": [str(value) for value in item.source_files],
                    "resource_files": [str(value) for value in item.resource_files],
                    "component_paths": list(item.component_paths),
                }
                for item in layout.modules
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _load_layout(path: Path | None) -> RepositoryLayout | None:
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                return None
            return RepositoryLayout(
                root=Path(payload["root"]),
                openspec_roots=tuple(Path(value) for value in payload["openspec_roots"]),
                issues=tuple(LayoutIssue(**value) for value in payload["issues"]),
                modules=tuple(
                    ModuleLayout(
                        root=Path(value["root"]),
                        relative_path=value["relative_path"],
                        service_id=value["service_id"],
                        display_name=value["display_name"],
                        owner=value["owner"],
                        aliases=tuple(value["aliases"]),
                        build_system=cast(BuildSystem, value["build_system"]),
                        state=cast(ModuleState, value["state"]),
                        role=cast(ModuleRole, value["role"]),
                        declared=bool(value["declared"]),
                        source_roots=tuple(Path(item) for item in value["source_roots"]),
                        resource_roots=tuple(Path(item) for item in value["resource_roots"]),
                        openspec_roots=tuple(Path(item) for item in value["openspec_roots"]),
                        source_files=tuple(Path(item) for item in value["source_files"]),
                        resource_files=tuple(Path(item) for item in value["resource_files"]),
                        component_paths=tuple(value["component_paths"]),
                    )
                    for value in payload["modules"]
                ),
            )
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _load_cached(path: Path) -> GraphSnapshot | None:
        if not path.is_file():
            return None
        try:
            return JsonGraphStore(path).load()
        except (OSError, ValueError):
            return None

    @staticmethod
    def _scan_targets(
        layouts: list[tuple[RepositoryInput, RepositoryLayout]],
    ) -> tuple[list[ScanTarget], list[ScanIssue]]:
        modules: list[tuple[RepositoryInput, ModuleLayout]] = [
            (repository, module)
            for repository, layout in layouts
            for module in layout.modules
            if module.relative_path not in repository.excluded_module_paths
        ]
        counts: dict[str, int] = {}
        for _repository, module in modules:
            counts[module.service_id] = counts.get(module.service_id, 0) + 1

        targets: list[ScanTarget] = []
        issues: list[ScanIssue] = []
        for repository, layout in layouts:
            for issue in layout.issues:
                issues.append(
                    ScanIssue(
                        repository=repository.name,
                        file=issue.file,
                        message=issue.message,
                    )
                )
        for repository, module in modules:
            service_id = module.service_id
            aliases = set(module.aliases)
            if counts[service_id] > 1:
                digest = hashlib.sha256(
                    f"{repository.path.resolve()}\x1f{module.relative_path}".encode()
                ).hexdigest()[:8]
                service_id = f"{service_id}--{digest}"
                aliases.add(module.service_id)
                issues.append(
                    ScanIssue(
                        repository=repository.name,
                        file=None,
                        message=(
                            f"Duplicate discovered service id '{module.service_id}' was "
                            f"disambiguated as '{service_id}' for module {module.relative_path}"
                        ),
                    )
                )
            targets.append(
                ScanTarget(
                    path=module.root,
                    repository_path=repository.path.resolve(),
                    repository_name=repository.name,
                    service_id=service_id,
                    display_name=module.display_name,
                    owner=module.owner,
                    aliases=tuple(sorted(aliases)),
                    module_path=module.relative_path,
                    module_state=module.state,
                    build_system=module.build_system,
                    source_roots=module.source_roots,
                    resource_roots=module.resource_roots,
                    source_files=module.source_files,
                    resource_files=module.resource_files,
                    component_paths=module.component_paths,
                    source_url=repository.source_url,
                    commit=repository.commit,
                )
            )
        return targets, issues

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
        lookup_path = (
            node.metadata.get("repository_path") or node.metadata.get("path")
            if node.type == "Service"
            else node.metadata.get("path")
        )
        repository = repositories.get(str(lookup_path))
        if repository is None:
            return node
        module_path = str(node.metadata.get("module_path") or ".")
        return node.model_copy(
            update={
                "label": (
                    repository.name
                    if node.type == "Repository" or module_path == "."
                    else node.label
                ),
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
            aliases = {str(item) for item in node.metadata.get("aliases", []) if str(item).strip()}
            aliases.update({node.service_id, node.label})
            services.append(
                ServiceRecord(
                    id=node.service_id,
                    name=node.label,
                    aliases=sorted(aliases),
                    repository=repository.name
                    if repository
                    else str(node.metadata.get("repository") or node.label),
                    repository_path=path,
                    repository_root=self._optional_string(node.metadata.get("repository_path")),
                    module_path=str(node.metadata.get("module_path") or "."),
                    component_paths=[
                        str(item)
                        for item in node.metadata.get("component_paths", [])
                        if str(item).strip()
                    ],
                    module_state=cast(
                        ModuleState, str(node.metadata.get("module_state") or "active")
                    ),
                    build_system=cast(
                        BuildSystem, str(node.metadata.get("build_system") or "unknown")
                    ),
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
        operation = str(node.metadata.get("operation") or node.metadata.get("topic") or node.label)
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
                target.metadata.get("target_hint") or target_service_id or target.label
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
