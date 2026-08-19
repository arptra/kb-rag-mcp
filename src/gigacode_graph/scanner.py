"""Source-only Java/Spring repository scanner and cross-repository linker.

The first implementation deliberately avoids executing repository builds. It extracts high-value
architecture facts from source and configuration, records evidence for every conclusion, and marks
anything it cannot resolve instead of asking a language model to guess.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from xml.etree import ElementTree

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import (
    Confidence,
    EdgeType,
    Evidence,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    ScanIssue,
)

_IGNORED_DIRECTORIES = {
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    ".settings",
    "build",
    "dist",
    "generated",
    "node_modules",
    "out",
    "target",
}
_HTTP_ANNOTATIONS = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
    "RequestMapping": "ANY",
}
_READ_PREFIXES = ("find", "get", "read", "load", "exists", "count", "query", "search")
_WRITE_PREFIXES = ("save", "delete", "remove", "update", "insert", "persist", "flush")


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def _compact_snippet(value: str, limit: int = 280) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"


def _strip_quotes(value: str) -> str:
    return value.strip().strip("\"'").strip()


def _simple_type(value: str) -> str:
    cleaned = re.sub(r"<.*>", "", value).replace("[]", "").strip()
    return cleaned.rsplit(".", 1)[-1]


def _path_value(arguments: str) -> str:
    named = re.search(r"(?:path|value)\s*=\s*(?:\{\s*)?[\"']([^\"']+)", arguments)
    if named:
        return named.group(1)
    direct = re.search(r"[\"']([^\"']+)[\"']", arguments)
    return direct.group(1) if direct else ""


def _annotation_arguments(annotations: str, name: str) -> str | None:
    match = re.search(rf"@(?:[\w.]+\.)?{re.escape(name)}\s*(?:\((.*?)\))?", annotations, re.S)
    if match is None:
        return None
    return match.group(1) or ""


def _annotation_present(annotations: str, name: str) -> bool:
    return bool(re.search(rf"@(?:[\w.]+\.)?{re.escape(name)}\b", annotations))


def _join_paths(base: str, child: str) -> str:
    parts = [part.strip("/") for part in (base, child) if part and part != "/"]
    return f"/{'/'.join(parts)}" if parts else "/"


def _humanize(name: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ")
    return value[:1].upper() + value[1:]


def _balanced_block(text: str, opening: int) -> tuple[str, int]:
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(opening, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[opening + 1 : index], index
    return text[opening + 1 :], len(text)


@dataclass
class JavaField:
    name: str
    type_name: str
    annotations: str
    line: int


@dataclass
class JavaMethod:
    class_name: str
    name: str
    annotations: str
    body: str
    line: int
    body_offset: int
    file: Path

    @property
    def symbol(self) -> str:
        return f"{self.class_name}#{self.name}"


@dataclass
class JavaClass:
    name: str
    package: str
    kind: str
    annotations: str
    extends: str
    fields: dict[str, JavaField]
    methods: list[JavaMethod]
    file: Path
    text: str
    line: int


@dataclass
class ServiceScan:
    service_id: str
    repository_name: str
    repository_path: Path
    commit: str | None
    aliases: set[str]
    classes: dict[str, JavaClass] = field(default_factory=dict)
    entity_tables: dict[str, str] = field(default_factory=dict)
    repository_tables: dict[str, str] = field(default_factory=dict)
    feign_targets: dict[str, str] = field(default_factory=dict)
    operation_methods: dict[str, JavaMethod] = field(default_factory=dict)


@dataclass
class OutboundFact:
    source_service: str
    target_hint: str
    protocol: str
    operation: str
    symbol_id: str | None
    evidence_id: str
    confidence: Confidence


@dataclass
class TopicFact:
    service_id: str
    topic: str
    action: Literal["PUBLISHES", "CONSUMES"]
    operation_id: str | None
    symbol_id: str | None
    evidence_id: str


def _outbound_exit_id(fact: OutboundFact) -> str:
    return _stable_id(
        "exitpoint",
        fact.source_service,
        fact.protocol,
        fact.target_hint,
        fact.operation,
    )


def _topic_exit_id(fact: TopicFact) -> str:
    return _stable_id("exitpoint", fact.service_id, "KAFKA", fact.topic)


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self.evidence: dict[str, Evidence] = {}
        self.issues: list[ScanIssue] = []

    def add_node(self, node: GraphNode) -> GraphNode:
        current = self.nodes.get(node.id)
        if current is None:
            self.nodes[node.id] = node
            return node
        merged_evidence = list(dict.fromkeys([*current.evidence_ids, *node.evidence_ids]))
        merged_metadata = {**current.metadata, **node.metadata}
        updated = current.model_copy(
            update={"evidence_ids": merged_evidence, "metadata": merged_metadata}
        )
        self.nodes[node.id] = updated
        return updated

    def add_edge(
        self,
        *,
        source: str,
        target: str,
        edge_type: EdgeType,
        label: str = "",
        confidence: Confidence = "HIGH",
        metadata: dict[str, Any] | None = None,
        evidence_ids: list[str] | None = None,
    ) -> GraphEdge:
        edge_id = _stable_id("edge", source, target, edge_type, label)
        edge = GraphEdge(
            id=edge_id,
            source=source,
            target=target,
            type=edge_type,
            label=label,
            confidence=confidence,
            metadata=metadata or {},
            evidence_ids=evidence_ids or [],
        )
        current = self.edges.get(edge_id)
        if current is not None:
            edge = current.model_copy(
                update={
                    "evidence_ids": list(
                        dict.fromkeys([*current.evidence_ids, *edge.evidence_ids])
                    ),
                    "metadata": {**current.metadata, **edge.metadata},
                }
            )
        self.edges[edge_id] = edge
        return edge

    def add_evidence(
        self,
        *,
        scan: ServiceScan,
        file: Path,
        line: int,
        snippet: str,
        extractor: str,
        confidence: Confidence = "HIGH",
    ) -> str:
        relative = file.relative_to(scan.repository_path).as_posix()
        evidence_id = _stable_id(
            "evidence", scan.repository_name, scan.commit, relative, line, extractor, snippet
        )
        self.evidence[evidence_id] = Evidence(
            id=evidence_id,
            repository=scan.repository_name,
            commit=scan.commit,
            file=relative,
            line=max(1, line),
            snippet=_compact_snippet(snippet),
            extractor=extractor,
            confidence=confidence,
        )
        return evidence_id

    def snapshot(self) -> GraphSnapshot:
        return GraphSnapshot(
            nodes=sorted(self.nodes.values(), key=lambda item: (item.type, item.id)),
            edges=sorted(self.edges.values(), key=lambda item: (item.type, item.id)),
            evidence=sorted(self.evidence.values(), key=lambda item: item.id),
            issues=self.issues,
        )


class RepositoryScanner:
    """Build one evidence-backed snapshot from local repository checkouts."""

    def __init__(self, settings: GraphSettings | None = None) -> None:
        self.settings = settings or GraphSettings().resolved()
        self._builder = _GraphBuilder()
        self._outbound: list[OutboundFact] = []
        self._topics: list[TopicFact] = []
        self._scans: dict[str, ServiceScan] = {}

    def scan(self, repositories: list[Path]) -> GraphSnapshot:
        if not repositories:
            raise ValueError("At least one repository path is required")
        resolved = [path.resolve() for path in repositories]
        for repository in resolved:
            if not repository.is_dir():
                raise ValueError(f"Repository directory does not exist: {repository}")
            scan = self._discover_repository(repository)
            if scan.service_id in self._scans:
                raise ValueError(f"Duplicate service id discovered: {scan.service_id}")
            self._scans[scan.service_id] = scan
            self._scan_repository(scan)
        self._link_service_dependencies()
        self._link_topic_dependencies()
        return self._builder.snapshot()

    def _discover_repository(self, repository: Path) -> ServiceScan:
        manifest = self._load_manifest(repository)
        source_metadata = self._load_source_metadata(repository)
        explicit = manifest.get("service", {}) if isinstance(manifest, dict) else {}
        service_id = str(explicit.get("id", "")).strip() if isinstance(explicit, dict) else ""
        aliases: set[str] = set()
        owner: str | None = None
        display_name: str | None = None
        if isinstance(explicit, dict):
            owner = str(explicit.get("owner", "")).strip() or None
            display_name = str(explicit.get("displayName", "")).strip() or None
            raw_aliases = explicit.get("aliases", [])
            if isinstance(raw_aliases, list):
                aliases.update(str(item).strip() for item in raw_aliases if str(item).strip())
        if not service_id:
            discovered_id = self._spring_application_name(repository) or self._maven_artifact(
                repository
            )
            service_id = discovered_id or ""
        repository_name = str(source_metadata.get("repository_name") or repository.name)
        service_id = service_id or repository_name
        aliases.update({service_id, repository_name})
        commit = self._git_commit(repository)
        scan = ServiceScan(
            service_id=service_id,
            repository_name=repository_name,
            repository_path=repository,
            commit=commit,
            aliases=aliases,
        )
        repository_node = GraphNode(
            id=f"repository:{service_id}:{repository_name}",
            type="Repository",
            label=repository_name,
            metadata={
                "path": str(repository),
                "commit": commit,
                "source": source_metadata.get("source"),
                "requested_ref": source_metadata.get("ref"),
                "managed_checkout": bool(source_metadata.get("managed")),
            },
        )
        service_node = GraphNode(
            id=f"service:{service_id}",
            type="Service",
            label=display_name or service_id,
            service_id=service_id,
            metadata={
                "repository": repository_name,
                "path": str(repository),
                "commit": commit,
                "source": source_metadata.get("source"),
                "requested_ref": source_metadata.get("ref"),
                "owner": owner,
                "aliases": sorted(aliases),
            },
        )
        self._builder.add_node(repository_node)
        self._builder.add_node(service_node)
        self._builder.add_edge(
            source=repository_node.id,
            target=service_node.id,
            edge_type="CONTAINS",
            confidence="DECLARED" if explicit else "HIGH",
        )
        return scan

    def _scan_repository(self, scan: ServiceScan) -> None:
        java_files = list(self._files(scan.repository_path, {".java"}))
        for path in java_files:
            try:
                if path.stat().st_size > self.settings.max_java_file_bytes:
                    self._builder.issues.append(
                        ScanIssue(
                            repository=scan.repository_name,
                            file=path.relative_to(scan.repository_path).as_posix(),
                            message="Java file exceeded scanner size limit",
                        )
                    )
                    continue
                java_class = self._parse_java_class(path)
                if java_class is not None:
                    scan.classes[java_class.name] = java_class
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                self._builder.issues.append(
                    ScanIssue(
                        repository=scan.repository_name,
                        file=path.relative_to(scan.repository_path).as_posix(),
                        message=f"Cannot parse Java source: {exc}",
                    )
                )
        self._index_database_model(scan)
        self._index_java_architecture(scan)
        self._index_migrations(scan)
        self._trace_operations(scan)

    def _index_java_architecture(self, scan: ServiceScan) -> None:
        for java_class in scan.classes.values():
            class_annotations = java_class.annotations
            class_base = ""
            request_args = _annotation_arguments(class_annotations, "RequestMapping")
            if request_args is not None:
                class_base = _path_value(request_args)
            feign_args = _annotation_arguments(class_annotations, "FeignClient")
            if feign_args is not None:
                target = self._feign_target(feign_args)
                if target:
                    scan.feign_targets[java_class.name] = target
            is_repository = java_class.name in scan.repository_tables
            relevant_class = (
                any(
                    _annotation_present(class_annotations, annotation)
                    for annotation in (
                        "RestController",
                        "Controller",
                        "Service",
                        "Component",
                        "Entity",
                        "FeignClient",
                    )
                )
                or is_repository
            )
            for method in java_class.methods:
                mappings = self._http_mappings(method.annotations, class_base)
                kafka_args = _annotation_arguments(method.annotations, "KafkaListener")
                scheduled = _annotation_present(method.annotations, "Scheduled")
                method_relevant = (
                    relevant_class or bool(mappings) or kafka_args is not None or scheduled
                )
                symbol_id = f"symbol:{scan.service_id}:{method.symbol}"
                if method_relevant:
                    evidence_id = self._builder.add_evidence(
                        scan=scan,
                        file=method.file,
                        line=method.line,
                        snippet=f"{method.annotations} {method.symbol}",
                        extractor="java-symbol",
                    )
                    self._builder.add_node(
                        GraphNode(
                            id=symbol_id,
                            type="CodeSymbol",
                            label=method.symbol,
                            service_id=scan.service_id,
                            metadata={"class": java_class.name, "method": method.name},
                            evidence_ids=[evidence_id],
                        )
                    )
                for http_method, route in mappings:
                    if feign_args is not None:
                        evidence_id = self._builder.add_evidence(
                            scan=scan,
                            file=method.file,
                            line=method.line,
                            snippet=f"@FeignClient({feign_args}) {http_method} {route}",
                            extractor="spring-feign",
                        )
                        self._outbound.append(
                            OutboundFact(
                                source_service=scan.service_id,
                                target_hint=scan.feign_targets.get(java_class.name, ""),
                                protocol="HTTP",
                                operation=f"{http_method} {route}",
                                symbol_id=symbol_id,
                                evidence_id=evidence_id,
                                confidence="HIGH",
                            )
                        )
                    elif _annotation_present(
                        class_annotations, "RestController"
                    ) or _annotation_present(class_annotations, "Controller"):
                        self._add_operation(
                            scan=scan,
                            method=method,
                            trigger_type="HTTP",
                            trigger_label=f"{http_method} {route}",
                            extractor="spring-http-entrypoint",
                        )
                if kafka_args is not None:
                    topic = self._topic_value(kafka_args)
                    operation_id = self._add_operation(
                        scan=scan,
                        method=method,
                        trigger_type="KAFKA",
                        trigger_label=topic or "unresolved topic",
                        extractor="spring-kafka-listener",
                    )
                    evidence_id = self._builder.add_evidence(
                        scan=scan,
                        file=method.file,
                        line=method.line,
                        snippet=f"@KafkaListener({kafka_args})",
                        extractor="spring-kafka-listener",
                        confidence="HIGH" if topic else "UNRESOLVED",
                    )
                    if topic:
                        self._topics.append(
                            TopicFact(
                                service_id=scan.service_id,
                                topic=topic,
                                action="CONSUMES",
                                operation_id=operation_id,
                                symbol_id=symbol_id,
                                evidence_id=evidence_id,
                            )
                        )
                if scheduled:
                    self._add_operation(
                        scan=scan,
                        method=method,
                        trigger_type="SCHEDULED",
                        trigger_label="scheduled job",
                        extractor="spring-scheduled-entrypoint",
                    )
                self._collect_literal_outbound(scan, method, symbol_id)

    def _add_operation(
        self,
        *,
        scan: ServiceScan,
        method: JavaMethod,
        trigger_type: str,
        trigger_label: str,
        extractor: str,
    ) -> str:
        operation_id = f"operation:{scan.service_id}:{trigger_type}:{trigger_label}:{method.symbol}"
        entrypoint_id = (
            f"entrypoint:{scan.service_id}:{trigger_type}:{trigger_label}:{method.symbol}"
        )
        symbol_id = f"symbol:{scan.service_id}:{method.symbol}"
        evidence_id = self._builder.add_evidence(
            scan=scan,
            file=method.file,
            line=method.line,
            snippet=f"{method.annotations} {method.symbol}",
            extractor=extractor,
        )
        self._builder.add_node(
            GraphNode(
                id=symbol_id,
                type="CodeSymbol",
                label=method.symbol,
                service_id=scan.service_id,
                metadata={"class": method.class_name, "method": method.name},
                evidence_ids=[evidence_id],
            )
        )
        self._builder.add_node(
            GraphNode(
                id=operation_id,
                type="BusinessOperation",
                label=_humanize(method.name),
                service_id=scan.service_id,
                metadata={
                    "trigger_type": trigger_type,
                    "trigger": trigger_label,
                    "handler": method.symbol,
                    "description": (
                        f"{_humanize(method.name)} triggered by {trigger_type} {trigger_label}"
                    ),
                    "semantic_status": "deterministic-name; GigaCode enrichment pending",
                },
                evidence_ids=[evidence_id],
            )
        )
        self._builder.add_node(
            GraphNode(
                id=entrypoint_id,
                type="EntryPoint",
                label=f"{trigger_type} {trigger_label}",
                service_id=scan.service_id,
                metadata={"trigger_type": trigger_type, "operation": trigger_label},
                evidence_ids=[evidence_id],
            )
        )
        self._builder.add_edge(
            source=f"service:{scan.service_id}",
            target=operation_id,
            edge_type="IMPLEMENTS",
            evidence_ids=[evidence_id],
        )
        self._builder.add_edge(
            source=operation_id,
            target=entrypoint_id,
            edge_type="TRIGGERED_BY",
            label=trigger_label,
            evidence_ids=[evidence_id],
        )
        self._builder.add_edge(
            source=entrypoint_id,
            target=symbol_id,
            edge_type="HANDLED_BY",
            evidence_ids=[evidence_id],
        )
        scan.operation_methods[operation_id] = method
        return operation_id

    def _collect_literal_outbound(
        self, scan: ServiceScan, method: JavaMethod, symbol_id: str
    ) -> None:
        for match in re.finditer(
            r"(?:kafkaTemplate|KafkaTemplate|streamBridge|StreamBridge)\s*\.\s*"
            r"(?:send|sendDefault)\s*\(\s*[\"']([^\"']+)[\"']",
            method.body,
        ):
            topic = match.group(1)
            line = method.line + _line_number(method.body, match.start()) - 1
            evidence_id = self._builder.add_evidence(
                scan=scan,
                file=method.file,
                line=line,
                snippet=match.group(0),
                extractor="spring-kafka-producer",
            )
            self._topics.append(
                TopicFact(
                    service_id=scan.service_id,
                    topic=topic,
                    action="PUBLISHES",
                    operation_id=None,
                    symbol_id=symbol_id,
                    evidence_id=evidence_id,
                )
            )
        base_urls = re.findall(r"\.baseUrl\s*\(\s*[\"']([^\"']+)[\"']", method.body)
        request_matches = list(
            re.finditer(
                r"\.(get|post|put|patch|delete)\s*\(\s*\).*?\.uri\s*\(\s*"
                r"[\"']([^\"']+)[\"']",
                method.body,
                re.S,
            )
        )
        for match in request_matches:
            route = match.group(2)
            absolute = route if "://" in route else (base_urls[0] if base_urls else "")
            if not absolute:
                continue
            evidence_id = self._builder.add_evidence(
                scan=scan,
                file=method.file,
                line=method.line + _line_number(method.body, match.start()) - 1,
                snippet=match.group(0),
                extractor="spring-rest-client",
                confidence="MEDIUM",
            )
            self._outbound.append(
                OutboundFact(
                    source_service=scan.service_id,
                    target_hint=absolute,
                    protocol="HTTP",
                    operation=f"{match.group(1).upper()} {route}",
                    symbol_id=symbol_id,
                    evidence_id=evidence_id,
                    confidence="MEDIUM",
                )
            )

    def _trace_operations(self, scan: ServiceScan) -> None:
        method_lookup: dict[tuple[str, str], JavaMethod] = {}
        for java_class in scan.classes.values():
            for method in java_class.methods:
                method_lookup.setdefault((java_class.name, method.name), method)
        topic_by_symbol: dict[str, list[TopicFact]] = {}
        for fact in self._topics:
            if fact.service_id == scan.service_id and fact.symbol_id:
                topic_by_symbol.setdefault(fact.symbol_id, []).append(fact)
        outbound_by_symbol: dict[str, list[OutboundFact]] = {}
        for outbound_fact in self._outbound:
            if outbound_fact.source_service == scan.service_id and outbound_fact.symbol_id:
                outbound_by_symbol.setdefault(outbound_fact.symbol_id, []).append(outbound_fact)
        for operation_id, root_method in scan.operation_methods.items():
            queue: list[tuple[JavaMethod, int]] = [(root_method, 0)]
            visited: set[str] = set()
            while queue:
                method, depth = queue.pop(0)
                if method.symbol in visited or depth > self.settings.call_depth:
                    continue
                visited.add(method.symbol)
                symbol_id = f"symbol:{scan.service_id}:{method.symbol}"
                self._builder.add_node(
                    GraphNode(
                        id=symbol_id,
                        type="CodeSymbol",
                        label=method.symbol,
                        service_id=scan.service_id,
                        metadata={"class": method.class_name, "method": method.name},
                    )
                )
                self._extract_rules(scan, operation_id, method)
                current_class = scan.classes.get(method.class_name)
                if current_class is None:
                    continue
                call_pattern = r"\b([a-zA-Z_]\w*)\s*\.\s*([a-zA-Z_]\w*)\s*\("
                for call in re.finditer(call_pattern, method.body):
                    variable, called_name = call.group(1), call.group(2)
                    field_info = current_class.fields.get(variable)
                    if field_info is None:
                        continue
                    target_type = _simple_type(field_info.type_name)
                    target_symbol_id = f"symbol:{scan.service_id}:{target_type}#{called_name}"
                    target_method = method_lookup.get((target_type, called_name))
                    evidence_id = self._builder.add_evidence(
                        scan=scan,
                        file=method.file,
                        line=method.line + _line_number(method.body, call.start()) - 1,
                        snippet=call.group(0),
                        extractor="java-call",
                        confidence="MEDIUM",
                    )
                    self._builder.add_node(
                        GraphNode(
                            id=target_symbol_id,
                            type="CodeSymbol",
                            label=f"{target_type}#{called_name}",
                            service_id=scan.service_id,
                            metadata={"class": target_type, "method": called_name},
                            evidence_ids=[evidence_id],
                        )
                    )
                    self._builder.add_edge(
                        source=symbol_id,
                        target=target_symbol_id,
                        edge_type="CALLS",
                        confidence="MEDIUM",
                        evidence_ids=[evidence_id],
                    )
                    table = scan.repository_tables.get(target_type)
                    if table:
                        access = self._repository_access(called_name)
                        if access:
                            self._builder.add_edge(
                                source=operation_id,
                                target=f"table:{scan.service_id}:{table}",
                                edge_type=access,
                                label=called_name,
                                confidence="MEDIUM",
                                evidence_ids=[evidence_id],
                            )
                    if target_method is not None:
                        queue.append((target_method, depth + 1))
                    if target_type in scan.feign_targets:
                        for outbound in outbound_by_symbol.get(target_symbol_id, []):
                            self._builder.add_edge(
                                source=operation_id,
                                target=target_symbol_id,
                                edge_type="CALLS",
                                label=outbound.operation,
                                evidence_ids=[evidence_id, outbound.evidence_id],
                            )
                for outbound in outbound_by_symbol.get(symbol_id, []):
                    self._builder.add_edge(
                        source=operation_id,
                        target=_outbound_exit_id(outbound),
                        edge_type="EXITS_VIA",
                        label=outbound.operation,
                        confidence=outbound.confidence,
                        evidence_ids=[outbound.evidence_id],
                    )
                for topic in topic_by_symbol.get(symbol_id, []):
                    event_id = f"event:{topic.topic}"
                    self._builder.add_edge(
                        source=operation_id,
                        target=event_id,
                        edge_type=topic.action,
                        label=topic.topic,
                        evidence_ids=[topic.evidence_id],
                    )
                    if topic.action == "PUBLISHES":
                        self._builder.add_edge(
                            source=operation_id,
                            target=_topic_exit_id(topic),
                            edge_type="EXITS_VIA",
                            label=topic.topic,
                            evidence_ids=[topic.evidence_id],
                        )

    def _extract_rules(self, scan: ServiceScan, operation_id: str, method: JavaMethod) -> None:
        for match in re.finditer(r"\bif\s*\(([^\n{}]{1,400})\)", method.body):
            condition = _compact_snippet(match.group(1), 220)
            line = method.line + _line_number(method.body, match.start()) - 1
            evidence_id = self._builder.add_evidence(
                scan=scan,
                file=method.file,
                line=line,
                snippet=match.group(0),
                extractor="java-business-rule",
                confidence="MEDIUM",
            )
            following = method.body[match.end() : match.end() + 500]
            thrown = re.search(r"throw\s+new\s+([A-Za-z_]\w*)", following)
            effect = f"throws {thrown.group(1)}" if thrown else "controls execution branch"
            rule_id = _stable_id("rule", operation_id, method.symbol, condition)
            self._builder.add_node(
                GraphNode(
                    id=rule_id,
                    type="BusinessRule",
                    label=condition,
                    service_id=scan.service_id,
                    metadata={
                        "condition": condition,
                        "effect": effect,
                        "method": method.symbol,
                        "semantic_status": "raw condition; GigaCode enrichment pending",
                    },
                    evidence_ids=[evidence_id],
                )
            )
            self._builder.add_edge(
                source=operation_id,
                target=rule_id,
                edge_type="GUARDED_BY",
                label=condition,
                confidence="MEDIUM",
                evidence_ids=[evidence_id],
            )

    def _index_database_model(self, scan: ServiceScan) -> None:
        for java_class in scan.classes.values():
            if _annotation_present(java_class.annotations, "Entity"):
                table_args = _annotation_arguments(java_class.annotations, "Table") or ""
                table_name = self._named_string(table_args, "name") or java_class.name
                schema = self._named_string(table_args, "schema")
                qualified = f"{schema}.{table_name}" if schema else table_name
                scan.entity_tables[java_class.name] = qualified
                evidence_id = self._builder.add_evidence(
                    scan=scan,
                    file=java_class.file,
                    line=java_class.line,
                    snippet=f"{java_class.annotations} {java_class.kind} {java_class.name}",
                    extractor="jpa-entity",
                )
                entity_id = f"entity:{scan.service_id}:{java_class.name}"
                table_id = f"table:{scan.service_id}:{qualified}"
                self._builder.add_node(
                    GraphNode(
                        id=entity_id,
                        type="DomainEntity",
                        label=java_class.name,
                        service_id=scan.service_id,
                        metadata={"java_type": java_class.name},
                        evidence_ids=[evidence_id],
                    )
                )
                self._builder.add_node(
                    GraphNode(
                        id=table_id,
                        type="Table",
                        label=qualified,
                        service_id=scan.service_id,
                        metadata={"schema": schema, "table": table_name},
                        evidence_ids=[evidence_id],
                    )
                )
                self._builder.add_edge(
                    source=f"service:{scan.service_id}",
                    target=entity_id,
                    edge_type="DECLARES_ENTITY",
                    evidence_ids=[evidence_id],
                )
                self._builder.add_edge(
                    source=entity_id,
                    target=table_id,
                    edge_type="MAPS_TO",
                    evidence_ids=[evidence_id],
                )
                self._index_entity_columns(scan, java_class, table_id)
        for java_class in scan.classes.values():
            repository_match = re.search(
                r"(?:JpaRepository|CrudRepository|PagingAndSortingRepository)\s*<\s*"
                r"([\w.]+)",
                java_class.extends,
            )
            if repository_match:
                entity_type = _simple_type(repository_match.group(1))
                table = scan.entity_tables.get(entity_type)
                if table:
                    scan.repository_tables[java_class.name] = table

    def _index_entity_columns(
        self, scan: ServiceScan, java_class: JavaClass, table_id: str
    ) -> None:
        for field_info in java_class.fields.values():
            column_args = _annotation_arguments(field_info.annotations, "Column") or ""
            join_args = _annotation_arguments(field_info.annotations, "JoinColumn") or ""
            explicit = self._named_string(column_args, "name") or self._named_string(
                join_args, "name"
            )
            column_name = explicit or field_info.name
            evidence_id = self._builder.add_evidence(
                scan=scan,
                file=java_class.file,
                line=field_info.line,
                snippet=f"{field_info.annotations} {field_info.type_name} {field_info.name}",
                extractor="jpa-column",
                confidence="HIGH" if explicit else "MEDIUM",
            )
            column_id = f"column:{table_id.removeprefix('table:')}:{column_name}"
            self._builder.add_node(
                GraphNode(
                    id=column_id,
                    type="Column",
                    label=column_name,
                    service_id=scan.service_id,
                    metadata={"java_field": field_info.name, "java_type": field_info.type_name},
                    evidence_ids=[evidence_id],
                )
            )
            self._builder.add_edge(
                source=table_id,
                target=column_id,
                edge_type="HAS_COLUMN",
                evidence_ids=[evidence_id],
            )

    def _index_migrations(self, scan: ServiceScan) -> None:
        for path in self._files(scan.repository_path, {".sql", ".yaml", ".yml", ".xml"}):
            relative = path.relative_to(scan.repository_path).as_posix().lower()
            if not any(marker in relative for marker in ("migration", "liquibase", "changelog")):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self._builder.issues.append(
                    ScanIssue(
                        repository=scan.repository_name,
                        file=relative,
                        message=f"Cannot read migration: {exc}",
                    )
                )
                continue
            matches: list[re.Match[str]] = list(
                re.finditer(r"\bcreate\s+table\s+(?:if\s+not\s+exists\s+)?([\w.\"`]+)", text, re.I)
            )
            matches.extend(re.finditer(r"\btableName\s*:\s*[\"']?([\w.]+)", text))
            matches.extend(re.finditer(r"\btableName\s*=\s*[\"']([\w.]+)", text))
            for match in matches:
                table = match.group(1).strip('"`')
                evidence_id = self._builder.add_evidence(
                    scan=scan,
                    file=path,
                    line=_line_number(text, match.start()),
                    snippet=match.group(0),
                    extractor="database-migration",
                )
                table_id = f"table:{scan.service_id}:{table}"
                self._builder.add_node(
                    GraphNode(
                        id=table_id,
                        type="Table",
                        label=table,
                        service_id=scan.service_id,
                        metadata={"declared_in_migration": True},
                        evidence_ids=[evidence_id],
                    )
                )
                self._builder.add_edge(
                    source=f"service:{scan.service_id}",
                    target=table_id,
                    edge_type="MANAGES_SCHEMA",
                    evidence_ids=[evidence_id],
                )

    def _link_service_dependencies(self) -> None:
        alias_map: dict[str, str] = {}
        for service_id, scan in self._scans.items():
            for alias in scan.aliases:
                alias_map[self._normalize_target(alias)] = service_id
        for fact in self._outbound:
            normalized = self._normalize_target(fact.target_hint)
            target_service = alias_map.get(normalized)
            if target_service:
                target_id = f"service:{target_service}"
                confidence = fact.confidence
            else:
                label = fact.target_hint or "unresolved target"
                target_id = f"external:{normalized or _stable_id('unknown', label)}"
                confidence = "UNRESOLVED" if "${" in label or not label else "LOW"
                self._builder.add_node(
                    GraphNode(
                        id=target_id,
                        type="ExternalSystem",
                        label=label,
                        metadata={"target_hint": fact.target_hint, "resolved": False},
                        evidence_ids=[fact.evidence_id],
                    )
                )
            exitpoint_id = _outbound_exit_id(fact)
            self._builder.add_node(
                GraphNode(
                    id=exitpoint_id,
                    type="ExitPoint",
                    label=f"{fact.protocol} {fact.operation}",
                    service_id=fact.source_service,
                    metadata={
                        "protocol": fact.protocol,
                        "operation": fact.operation,
                        "target_hint": fact.target_hint,
                    },
                    evidence_ids=[fact.evidence_id],
                )
            )
            self._builder.add_edge(
                source=f"service:{fact.source_service}",
                target=exitpoint_id,
                edge_type="EXITS_VIA",
                label=fact.operation,
                confidence=fact.confidence,
                evidence_ids=[fact.evidence_id],
            )
            self._builder.add_edge(
                source=exitpoint_id,
                target=target_id,
                edge_type="DEPENDS_ON",
                label=f"{fact.protocol} {fact.operation}",
                confidence=confidence,
                metadata={"protocol": fact.protocol, "operation": fact.operation},
                evidence_ids=[fact.evidence_id],
            )
            self._builder.add_edge(
                source=f"service:{fact.source_service}",
                target=target_id,
                edge_type="DEPENDS_ON",
                label=f"{fact.protocol} {fact.operation}",
                confidence=confidence,
                metadata={"protocol": fact.protocol, "operation": fact.operation},
                evidence_ids=[fact.evidence_id],
            )

    def _link_topic_dependencies(self) -> None:
        producers: dict[str, list[TopicFact]] = {}
        consumers: dict[str, list[TopicFact]] = {}
        for fact in self._topics:
            bucket = producers if fact.action == "PUBLISHES" else consumers
            bucket.setdefault(fact.topic, []).append(fact)
            event_id = f"event:{fact.topic}"
            self._builder.add_node(
                GraphNode(
                    id=event_id,
                    type="Event",
                    label=fact.topic,
                    metadata={"topic": fact.topic},
                    evidence_ids=[fact.evidence_id],
                )
            )
            self._builder.add_edge(
                source=f"service:{fact.service_id}",
                target=event_id,
                edge_type=fact.action,
                label=fact.topic,
                evidence_ids=[fact.evidence_id],
            )
            if fact.action == "PUBLISHES":
                exitpoint_id = _topic_exit_id(fact)
                self._builder.add_node(
                    GraphNode(
                        id=exitpoint_id,
                        type="ExitPoint",
                        label=f"KAFKA {fact.topic}",
                        service_id=fact.service_id,
                        metadata={"protocol": "KAFKA", "topic": fact.topic},
                        evidence_ids=[fact.evidence_id],
                    )
                )
                self._builder.add_edge(
                    source=f"service:{fact.service_id}",
                    target=exitpoint_id,
                    edge_type="EXITS_VIA",
                    label=fact.topic,
                    evidence_ids=[fact.evidence_id],
                )
                self._builder.add_edge(
                    source=exitpoint_id,
                    target=event_id,
                    edge_type="PUBLISHES",
                    label=fact.topic,
                    evidence_ids=[fact.evidence_id],
                )
        for topic, source_facts in producers.items():
            for source in source_facts:
                for target in consumers.get(topic, []):
                    if source.service_id == target.service_id:
                        continue
                    self._builder.add_edge(
                        source=f"service:{source.service_id}",
                        target=f"service:{target.service_id}",
                        edge_type="DEPENDS_ON",
                        label=f"KAFKA {topic}",
                        metadata={"protocol": "KAFKA", "topic": topic},
                        evidence_ids=[source.evidence_id, target.evidence_id],
                    )

    def _parse_java_class(self, path: Path) -> JavaClass | None:
        text = path.read_text(encoding="utf-8")
        package_match = re.search(r"\bpackage\s+([\w.]+)\s*;", text)
        class_match = re.search(
            r"(?P<header>(?:(?:^|\n)[ \t]*@[\w.]+(?:\s*\([^;]*?\))?[ \t]*\n)*)"
            r"[ \t]*(?:public\s+|protected\s+|private\s+|abstract\s+|final\s+)*"
            r"(?P<kind>class|interface|record)\s+(?P<name>[A-Za-z_]\w*)"
            r"(?P<tail>[^\{]*)\{",
            text,
            re.M | re.S,
        )
        if class_match is None:
            return None
        opening = class_match.end() - 1
        body, _closing = _balanced_block(text, opening)
        body_start = opening + 1
        methods = self._parse_methods(
            class_name=class_match.group("name"),
            body=body,
            body_start=body_start,
            full_text=text,
            path=path,
        )
        fields = self._parse_fields(body, body_start, text)
        return JavaClass(
            name=class_match.group("name"),
            package=package_match.group(1) if package_match else "",
            kind=class_match.group("kind"),
            annotations=class_match.group("header") or "",
            extends=class_match.group("tail") or "",
            fields=fields,
            methods=methods,
            file=path,
            text=text,
            line=_line_number(text, class_match.start()),
        )

    def _parse_methods(
        self,
        *,
        class_name: str,
        body: str,
        body_start: int,
        full_text: str,
        path: Path,
    ) -> list[JavaMethod]:
        pattern = re.compile(
            r"(?P<annotations>(?:(?:^|\n)[ \t]*@[\w.]+(?:\s*\([^;]*?\))?[ \t]*\n)*)"
            r"[ \t]*(?P<visibility>public|protected|private)?[ \t]*"
            r"(?:static\s+|final\s+|abstract\s+|synchronized\s+|default\s+|native\s+)*"
            r"(?P<return>[\w.$<>\[\],? ]+)\s+(?P<name>[A-Za-z_]\w*)\s*"
            r"\((?P<params>[^;{}]*)\)\s*(?:throws\s+[^\{;]+)?(?P<term>\{|;)",
            re.M,
        )
        methods: list[JavaMethod] = []
        for match in pattern.finditer(body):
            annotations = match.group("annotations") or ""
            if not match.group("visibility") and not annotations.strip():
                continue
            name = match.group("name")
            if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
                continue
            absolute = body_start + match.start()
            method_body = ""
            body_offset = body_start + match.end()
            if match.group("term") == "{":
                opening = body_start + match.end() - 1
                method_body, _end = _balanced_block(full_text, opening)
                body_offset = opening + 1
            methods.append(
                JavaMethod(
                    class_name=class_name,
                    name=name,
                    annotations=annotations,
                    body=method_body,
                    line=_line_number(full_text, absolute),
                    body_offset=body_offset,
                    file=path,
                )
            )
        return methods

    def _parse_fields(self, body: str, body_start: int, full_text: str) -> dict[str, JavaField]:
        pattern = re.compile(
            r"(?P<annotations>(?:(?:^|\n)[ \t]*@[\w.]+(?:\s*\([^;]*?\))?[ \t]*\n)*)"
            r"[ \t]*(?:private|protected|public)\s+"
            r"(?:static\s+|final\s+|transient\s+|volatile\s+)*"
            r"(?P<type>[\w.$<>\[\],? ]+)\s+(?P<name>[A-Za-z_]\w*)\s*"
            r"(?:=[^;]+)?;",
            re.M,
        )
        fields: dict[str, JavaField] = {}
        for match in pattern.finditer(body):
            fields[match.group("name")] = JavaField(
                name=match.group("name"),
                type_name=" ".join(match.group("type").split()),
                annotations=match.group("annotations") or "",
                line=_line_number(full_text, body_start + match.start()),
            )
        return fields

    def _http_mappings(self, annotations: str, base_path: str) -> list[tuple[str, str]]:
        mappings: list[tuple[str, str]] = []
        for annotation, default_method in _HTTP_ANNOTATIONS.items():
            arguments = _annotation_arguments(annotations, annotation)
            if arguments is None:
                continue
            method = default_method
            if annotation == "RequestMapping":
                method_match = re.search(r"RequestMethod\.([A-Z]+)", arguments)
                if method_match:
                    method = method_match.group(1)
            mappings.append((method, _join_paths(base_path, _path_value(arguments))))
        return mappings

    @staticmethod
    def _named_string(arguments: str, name: str) -> str | None:
        match = re.search(rf"\b{re.escape(name)}\s*=\s*[\"']([^\"']+)[\"']", arguments)
        return match.group(1) if match else None

    def _feign_target(self, arguments: str) -> str:
        return (
            self._named_string(arguments, "name")
            or self._named_string(arguments, "value")
            or self._named_string(arguments, "url")
            or _path_value(arguments)
        )

    def _topic_value(self, arguments: str) -> str:
        named = self._named_string(arguments, "topics")
        return named or _path_value(arguments)

    @staticmethod
    def _repository_access(
        method_name: str,
    ) -> Literal["READS", "WRITES"] | None:
        lowered = method_name.lower()
        if lowered.startswith(_WRITE_PREFIXES):
            return "WRITES"
        if lowered.startswith(_READ_PREFIXES):
            return "READS"
        return None

    @staticmethod
    def _load_manifest(repository: Path) -> dict[str, Any]:
        path = repository / "gigacode-graph.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Manifest must be a JSON object: {path}")
        return payload

    @staticmethod
    def _load_source_metadata(repository: Path) -> dict[str, Any]:
        path = repository / ".gigacode-graph-source.json"
        if not path.is_file():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}

    def _spring_application_name(self, repository: Path) -> str | None:
        candidates = [
            path
            for path in self._files(repository, {".properties", ".yaml", ".yml"})
            if path.name.startswith("application") or path.name.startswith("bootstrap")
        ]
        for path in sorted(candidates):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            properties = re.search(r"(?m)^\s*spring\.application\.name\s*[:=]\s*([^\s#]+)", text)
            if properties:
                return _strip_quotes(properties.group(1))
            yaml = re.search(
                r"(?ms)^spring\s*:\s*\n(?:(?:[ \t]+.*)?\n)*?"
                r"[ \t]+application\s*:\s*\n(?:(?:[ \t]+.*)?\n)*?"
                r"[ \t]+name\s*:\s*([^\s#]+)",
                text,
            )
            if yaml:
                return _strip_quotes(yaml.group(1))
        return None

    @staticmethod
    def _maven_artifact(repository: Path) -> str | None:
        path = repository / "pom.xml"
        if not path.is_file():
            return None
        try:
            root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
        except (ElementTree.ParseError, OSError, UnicodeDecodeError):
            return None
        for child in root:
            if child.tag.rsplit("}", 1)[-1] == "artifactId" and child.text:
                return child.text.strip()
        return None

    @staticmethod
    def _git_commit(repository: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    @staticmethod
    def _files(root: Path, suffixes: set[str]) -> Iterator[Path]:
        for current, directories, files in os.walk(root, followlinks=False):
            directory = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _IGNORED_DIRECTORIES and not (directory / name).is_symlink()
            ]
            for name in files:
                path = directory / name
                if not path.is_symlink() and path.suffix.lower() in suffixes:
                    yield path

    @staticmethod
    def _normalize_target(value: str) -> str:
        raw = value.strip().lower()
        if not raw:
            return ""
        if raw.startswith("${"):
            return raw
        if "://" in raw:
            parsed = urlparse(raw)
            raw = parsed.hostname or parsed.path
        raw = raw.split("/", 1)[0].split(":", 1)[0]
        for suffix in (".default.svc.cluster.local", ".svc.cluster.local", ".svc"):
            raw = raw.removesuffix(suffix)
        return re.sub(r"[^a-z0-9_-]", "", raw)
