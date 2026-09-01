"""Use GigaCode to verify bounded static dependency candidates without owning the graph."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from corporate_kb.gigacode_runner import (
    CancellationSignal,
    GigaCodeCancelled,
    GigaCodeRunner,
)
from gigacode_graph.config import GraphSettings
from gigacode_graph.contracts import contracts_compatible
from gigacode_graph.models import Evidence, GraphEdge, GraphNode, ScanIssue
from service_map import RepositoryInput, ServiceMapBuilder, ServiceMapBuildResult
from service_map.models import ServiceDependency, ServiceMapEvidence, ServiceMapSnapshot

_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edge_updates": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": ["confirm", "reject", "retarget", "unresolved"],
                    },
                    "target_service_id": {"type": ["string", "null"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["HIGH", "MEDIUM", "LOW", "UNRESOLVED"],
                    },
                    "reason": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer", "minimum": 1},
                                "symbol": {"type": ["string", "null"]},
                            },
                            "required": ["file", "line"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "candidate_id",
                    "decision",
                    "target_service_id",
                    "confidence",
                    "reason",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        },
        "discovered_edges": {
            "type": "array",
            "maxItems": 100,
            "items": {
                "type": "object",
                "properties": {
                    "source_service_id": {"type": "string"},
                    "target_service_id": {"type": "string"},
                    "target_entrypoint_id": {"type": "string"},
                    "protocol": {"type": "string", "enum": ["HTTP", "KAFKA"]},
                    "operation": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM"]},
                    "reason": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 20,
                        "items": {
                            "type": "object",
                            "properties": {
                                "file": {"type": "string"},
                                "line": {"type": "integer", "minimum": 1},
                                "symbol": {"type": ["string", "null"]},
                            },
                            "required": ["file", "line"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "source_service_id",
                    "target_service_id",
                    "target_entrypoint_id",
                    "protocol",
                    "operation",
                    "confidence",
                    "reason",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        },
        "analyzed_files": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2000,
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 200,
        },
    },
    "required": ["edge_updates", "discovered_edges", "analyzed_files", "warnings"],
    "additionalProperties": False,
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerificationEvidence(_StrictModel):
    file: str
    line: int = Field(ge=1)
    symbol: str | None = None


class EdgeUpdate(_StrictModel):
    candidate_id: str
    decision: Literal["confirm", "reject", "retarget", "unresolved"]
    target_service_id: str | None = None
    confidence: Literal["HIGH", "MEDIUM", "LOW", "UNRESOLVED"]
    reason: str
    evidence: list[VerificationEvidence] = Field(default_factory=list)


class DiscoveredEdge(_StrictModel):
    source_service_id: str
    target_service_id: str
    target_entrypoint_id: str
    protocol: Literal["HTTP", "KAFKA"]
    operation: str
    confidence: Literal["HIGH", "MEDIUM"]
    reason: str
    evidence: list[VerificationEvidence] = Field(min_length=1)


class VerificationPayload(_StrictModel):
    edge_updates: list[EdgeUpdate] = Field(default_factory=list)
    discovered_edges: list[DiscoveredEdge] = Field(default_factory=list)
    analyzed_files: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class GraphGigaCodeVerifier:
    """Resolve static dependency hints using small repository-scoped model requests."""

    def __init__(
        self,
        runner: GigaCodeRunner,
        graph_settings: GraphSettings,
        artifact_root: Path,
    ) -> None:
        self._runner = runner
        self._graph_settings = graph_settings
        self._artifact_root = artifact_root

    def verify(
        self,
        result: ServiceMapBuildResult,
        repositories: list[RepositoryInput],
        *,
        verify_all: bool = False,
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str, str], None] | None = None,
        authentication_complete: Callable[[str], None] | None = None,
    ) -> tuple[ServiceMapBuildResult, dict[str, Any]]:
        candidates = [
            item
            for item in result.service_map.dependencies
            if verify_all or not item.resolved or item.confidence in {"LOW", "UNRESOLVED"}
        ]
        summary: dict[str, Any] = {
            "requested": len(candidates),
            "processed": 0,
            "failed": 0,
            "confirmed": 0,
            "retargeted": 0,
            "rejected": 0,
            "unresolved": 0,
            "discovered": 0,
            "ignored": 0,
            "warnings": [],
            "runs": [],
        }
        if not candidates and not verify_all:
            if progress is not None:
                progress("GigaCode verification: no dependency candidates require review")
            return result, summary

        services = {item.id: item for item in result.service_map.services}
        evidence = {item.id: item for item in result.service_map.evidence}
        repository_by_root = {str(item.path.resolve()): item for item in repositories}
        grouped: dict[str, tuple[RepositoryInput, list[ServiceDependency]]] = {}
        if verify_all:
            for configured_repository in repositories:
                grouped[str(configured_repository.path.resolve())] = (
                    configured_repository,
                    [],
                )
        for candidate in candidates:
            source = services.get(candidate.source_service_id)
            repository: RepositoryInput | None = None
            if source is not None and source.repository_root:
                repository = repository_by_root.get(str(Path(source.repository_root).resolve()))
            if repository is None and source is not None:
                repository = next(
                    (item for item in repositories if item.name == source.repository),
                    None,
                )
            if repository is None:
                summary["ignored"] += 1
                summary["warnings"].append(f"No checkout found for dependency {candidate.id}")
                continue
            key = str(repository.path.resolve())
            grouped.setdefault(key, (repository, []))[1].append(candidate)

        graph = result.graph.model_copy(deep=True)
        raw_runs: list[dict[str, Any]] = []
        for repository, repository_candidates in grouped.values():
            batches = [
                repository_candidates[offset : offset + 25]
                for offset in range(0, len(repository_candidates), 25)
            ]
            if not batches and verify_all:
                batches = [[]]
            for batch_number, batch in enumerate(batches, start=1):
                if cancel is not None and cancel.is_set():
                    raise GigaCodeCancelled("GigaCode graph verification was cancelled")
                label = f"graph:{repository.name}:{batch_number}"
                if progress is not None:
                    progress(
                        "GigaCode verification: "
                        f"repository={repository.name}; candidates={len(batch)}; "
                        f"processed={summary['processed']}/{summary['requested']}"
                    )
                auth_required: Callable[[str], None] | None = None
                auth_completed: Callable[[], None] | None = None
                if authentication_url is not None:

                    def auth_required(url: str, current: str = label) -> None:
                        authentication_url(current, url)

                if authentication_complete is not None:

                    def auth_completed(current: str = label) -> None:
                        authentication_complete(current)

                try:
                    response = self._runner.run_json(
                        checkout=repository.path,
                        prompt=self._prompt(
                            repository=repository,
                            candidates=batch,
                            services=result.service_map,
                            evidence=evidence,
                            discover_missing=verify_all and batch_number == 1,
                        ),
                        schema=_RESULT_SCHEMA,
                        cancel=cancel,
                        progress=progress,
                        authentication_url=auth_required,
                        authentication_complete=auth_completed,
                    )
                    payload = VerificationPayload.model_validate(response.payload)
                except GigaCodeCancelled:
                    raise
                except Exception as exc:
                    error = self._failure_message(exc)
                    summary["processed"] += len(batch)
                    summary["failed"] += 1
                    summary["unresolved"] += len(batch)
                    summary["warnings"].append(f"GigaCode verification skipped {label}: {error}")
                    failed_run = {
                        "repository": repository.name,
                        "checkout": str(repository.path.resolve()),
                        "candidate_ids": [item.id for item in batch],
                        "status": "failed",
                        "error": error,
                    }
                    raw_runs.append(failed_run)
                    summary["runs"].append(
                        {
                            "repository": repository.name,
                            "candidate_count": len(batch),
                            "status": "failed",
                            "error": error,
                        }
                    )
                    if progress is not None:
                        progress(
                            "GigaCode verification fallback: "
                            f"repository={repository.name}; candidates={len(batch)}; "
                            f"static_graph_preserved=true; error={error}"
                        )
                    continue
                raw_runs.append(
                    {
                        "repository": repository.name,
                        "checkout": str(repository.path.resolve()),
                        "candidate_ids": [item.id for item in batch],
                        "session_id": response.session_id,
                        "model": response.model,
                        "duration_ms": response.duration_ms,
                        "usage": response.usage,
                        "result": payload.model_dump(mode="json"),
                    }
                )
                graph = self._apply_updates(
                    graph,
                    result.service_map,
                    repository,
                    batch,
                    payload,
                    summary,
                )
                summary["processed"] += len(batch)
                summary["warnings"].extend(payload.warnings)
                summary["runs"].append(
                    {
                        "repository": repository.name,
                        "session_id": response.session_id,
                        "model": response.model,
                        "candidate_count": len(batch),
                        "status": "completed",
                    }
                )

        if summary["failed"] and not any(
            run.get("status") == "completed" for run in summary["runs"]
        ):
            summary["fallback"] = "static-graph"

        projected = ServiceMapBuilder(self._graph_settings).from_graph(graph, repositories)
        projected = ServiceMapBuildResult(
            graph=projected.graph,
            service_map=projected.service_map,
            partial=result.partial,
        )
        artifact = self._save_artifact(raw_runs, summary)
        summary["artifact"] = str(artifact)
        if progress is not None:
            progress(
                "GigaCode verification ready: "
                f"confirmed={summary['confirmed']}; retargeted={summary['retargeted']}; "
                f"discovered={summary['discovered']}; rejected={summary['rejected']}; "
                f"unresolved={summary['unresolved']}; failed={summary['failed']}; "
                f"artifact={artifact}"
            )
        return projected, summary

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        detail = " ".join(str(exc).split()) or "no error details"
        return f"{type(exc).__name__}: {detail}"[:4000]

    def _apply_updates(
        self,
        graph: Any,
        service_map: ServiceMapSnapshot,
        repository: RepositoryInput,
        batch: list[ServiceDependency],
        payload: VerificationPayload,
        summary: dict[str, Any],
    ) -> Any:
        allowed = {item.id: item for item in batch}
        service_nodes = {
            item.service_id: item
            for item in graph.nodes
            if item.type == "Service" and item.service_id
        }
        nodes = {item.id: item for item in graph.nodes}
        edges = {item.id: item for item in graph.edges}
        graph_evidence = {item.id: item for item in graph.evidence}
        now = datetime.now(UTC)
        for update in payload.edge_updates:
            candidate = allowed.get(update.candidate_id)
            edge = edges.get(update.candidate_id)
            if candidate is None or edge is None:
                summary["ignored"] += 1
                summary["warnings"].append(
                    f"GigaCode returned unknown candidate {update.candidate_id}"
                )
                continue
            evidence_ids = list(edge.evidence_ids)
            for proposed in update.evidence:
                validated = self._validated_evidence(repository, proposed, update.confidence)
                if validated is None:
                    summary["warnings"].append(
                        f"Rejected invalid evidence {proposed.file}:{proposed.line} "
                        f"for {candidate.id}"
                    )
                    continue
                graph_evidence[validated.id] = validated
                evidence_ids.append(validated.id)
            metadata = {
                **edge.metadata,
                "verification_reason": update.reason[:2000],
                "verification_status": update.decision,
            }
            replacement: GraphEdge
            if update.decision == "reject":
                replacement = edge.model_copy(
                    update={
                        "confidence": "LOW",
                        "status": "rejected",
                        "origin": "static+gigacode",
                        "verified_at": now,
                        "metadata": metadata,
                        "evidence_ids": list(dict.fromkeys(evidence_ids)),
                    }
                )
                summary["rejected"] += 1
            elif update.decision == "unresolved":
                replacement = edge.model_copy(
                    update={
                        "confidence": "UNRESOLVED",
                        "status": "unresolved",
                        "origin": "static+gigacode",
                        "verified_at": now,
                        "metadata": metadata,
                        "evidence_ids": list(dict.fromkeys(evidence_ids)),
                    }
                )
                summary["unresolved"] += 1
            else:
                target = service_nodes.get(update.target_service_id)
                if target is None:
                    summary["ignored"] += 1
                    summary["warnings"].append(
                        f"Unknown target service {update.target_service_id!r} for {candidate.id}"
                    )
                    continue
                replacement = edge.model_copy(
                    update={
                        "target": target.id,
                        "confidence": update.confidence,
                        "status": ("confirmed" if update.confidence in {"HIGH"} else "inferred"),
                        "origin": "static+gigacode",
                        "verified_at": now,
                        "metadata": {
                            **metadata,
                            "resolved_target_service_id": update.target_service_id,
                        },
                        "evidence_ids": list(dict.fromkeys(evidence_ids)),
                    }
                )
                summary["retargeted" if update.decision == "retarget" else "confirmed"] += 1
            edges[edge.id] = replacement

        owned_services = {
            item.id
            for item in service_map.services
            if item.repository == repository.name
            or (
                item.repository_root is not None
                and Path(item.repository_root).resolve() == repository.path.resolve()
            )
        }
        entrypoints = {
            entry.id: (service, entry)
            for service in service_map.services
            for entry in service.entrypoints
        }
        for discovery in payload.discovered_edges:
            source = service_nodes.get(discovery.source_service_id)
            target = service_nodes.get(discovery.target_service_id)
            target_contract = entrypoints.get(discovery.target_entrypoint_id)
            if (
                source is None
                or discovery.source_service_id not in owned_services
                or target is None
                or discovery.source_service_id == discovery.target_service_id
                or target_contract is None
                or target_contract[0].id != discovery.target_service_id
            ):
                summary["ignored"] += 1
                summary["warnings"].append(
                    "Rejected GigaCode discovery with an unknown source, target, or entrypoint: "
                    f"{discovery.source_service_id} -> {discovery.target_service_id}"
                )
                continue
            entrypoint = target_contract[1]
            if not contracts_compatible(
                discovery.protocol,
                discovery.operation,
                entrypoint.kind,
                entrypoint.operation,
            ):
                summary["ignored"] += 1
                summary["warnings"].append(
                    "Rejected incompatible GigaCode discovery: "
                    f"{discovery.operation} -> {entrypoint.operation}"
                )
                continue
            source_evidence: list[str] = []
            for proposed in discovery.evidence:
                validated = self._validated_evidence(repository, proposed, discovery.confidence)
                if validated is not None:
                    graph_evidence[validated.id] = validated
                    source_evidence.append(validated.id)
            target_evidence = [
                evidence_id
                for evidence_id in entrypoint.evidence_ids
                if evidence_id in graph_evidence
            ]
            if not source_evidence or not target_evidence:
                summary["ignored"] += 1
                summary["warnings"].append(
                    "Rejected GigaCode discovery without two-sided evidence: "
                    f"{discovery.source_service_id} -> {discovery.target_service_id}"
                )
                continue
            duplicate = next(
                (
                    edge
                    for edge in edges.values()
                    if edge.type == "DEPENDS_ON"
                    and edge.source == source.id
                    and edge.target == target.id
                    and contracts_compatible(
                        str(edge.metadata.get("protocol") or "UNKNOWN"),
                        str(edge.metadata.get("operation") or edge.label),
                        discovery.protocol,
                        discovery.operation,
                    )
                ),
                None,
            )
            if duplicate is not None:
                summary["ignored"] += 1
                continue
            material = "\x1f".join(
                (
                    discovery.source_service_id,
                    discovery.target_service_id,
                    discovery.protocol,
                    discovery.operation,
                    discovery.target_entrypoint_id,
                )
            )
            digest = hashlib.sha256(material.encode()).hexdigest()[:20]
            exitpoint_id = f"exitpoint:gigacode:{digest}"
            evidence_ids = list(dict.fromkeys([*source_evidence, *target_evidence]))
            nodes[exitpoint_id] = GraphNode(
                id=exitpoint_id,
                type="ExitPoint",
                label=f"{discovery.protocol} {discovery.operation}",
                service_id=discovery.source_service_id,
                metadata={
                    "protocol": discovery.protocol,
                    "operation": discovery.operation,
                    "target_hint": discovery.target_service_id,
                    "matcher": "gigacode-discovery",
                },
                evidence_ids=evidence_ids,
            )
            edge_metadata = {
                "protocol": discovery.protocol,
                "operation": discovery.operation,
                "matcher": "gigacode-discovery",
                "target_entrypoint_id": discovery.target_entrypoint_id,
                "verification_reason": discovery.reason[:2000],
            }
            status = "confirmed" if discovery.confidence == "HIGH" else "inferred"
            for suffix, edge_source, edge_target, edge_type in (
                ("exit", source.id, exitpoint_id, "EXITS_VIA"),
                ("target", exitpoint_id, target.id, "DEPENDS_ON"),
                ("service", source.id, target.id, "DEPENDS_ON"),
            ):
                edge_id = f"edge:gigacode:{suffix}:{digest}"
                edges[edge_id] = GraphEdge(
                    id=edge_id,
                    source=edge_source,
                    target=edge_target,
                    type=edge_type,
                    label=f"{discovery.protocol} {discovery.operation}",
                    confidence=discovery.confidence,
                    status=status,
                    origin="gigacode",
                    verified_at=now,
                    metadata=edge_metadata,
                    evidence_ids=evidence_ids,
                )
            summary["discovered"] += 1

        issues = list(graph.issues)
        for warning in payload.warnings:
            issues.append(
                ScanIssue(repository=repository.name, message=f"GigaCode: {warning[:2000]}")
            )
        return graph.model_copy(
            update={
                "nodes": list(nodes.values()),
                "edges": list(edges.values()),
                "evidence": list(graph_evidence.values()),
                "issues": issues,
            }
        )

    @staticmethod
    def _validated_evidence(
        repository: RepositoryInput,
        proposed: VerificationEvidence,
        confidence: Literal["HIGH", "MEDIUM", "LOW", "UNRESOLVED"],
    ) -> Evidence | None:
        root = repository.path.resolve()
        path = (root / proposed.file).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if proposed.line > len(lines):
            return None
        snippet = lines[proposed.line - 1].strip()[:1000]
        identity = f"{repository.name}\0{relative.as_posix()}\0{proposed.line}\0{snippet}"
        evidence_id = "gigacode:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        return Evidence(
            id=evidence_id,
            repository=repository.name,
            commit=repository.commit,
            file=relative.as_posix(),
            line=proposed.line,
            snippet=(f"{proposed.symbol}: {snippet}" if proposed.symbol and snippet else snippet),
            extractor="gigacode-verifier",
            confidence=confidence,
        )

    @staticmethod
    def _prompt(
        *,
        repository: RepositoryInput,
        candidates: list[ServiceDependency],
        services: ServiceMapSnapshot,
        evidence: dict[str, ServiceMapEvidence],
        discover_missing: bool,
    ) -> str:
        catalog = [
            {
                "id": item.id,
                "name": item.name,
                "aliases": item.aliases,
                "module_path": item.module_path,
                "entrypoints": [
                    {
                        "id": entry.id,
                        "kind": entry.kind,
                        "operation": entry.operation,
                    }
                    for entry in item.entrypoints[:30]
                ],
            }
            for item in services.services
        ]
        packets = []
        for item in candidates:
            packets.append(
                {
                    "candidate_id": item.id,
                    "source_service_id": item.source_service_id,
                    "current_target_service_id": item.target_service_id,
                    "target_hint": item.target_hint,
                    "protocol": item.protocol,
                    "operation": item.operation,
                    "confidence": item.confidence,
                    "evidence": [
                        evidence[evidence_id].model_dump(mode="json")
                        for evidence_id in item.evidence_ids
                        if evidence_id in evidence
                    ][:10],
                }
            )
        return (
            "You verify a source-derived service dependency graph. Work read-only inside the "
            f"repository checkout {repository.name!r}. Inspect only files needed to confirm the "
            "candidate edges. Never infer runtime order, a target service, API, or event without "
            "source evidence. Use decision=unresolved when proof is insufficient. Evidence paths "
            "must be relative to the checkout and lines must exist. Return one JSON object "
            "matching "
            "the supplied output contract. Do not wrap it in Markdown.\n\nSERVICE CATALOG:\n"
            + json.dumps(catalog, ensure_ascii=False)
            + "\n\nDEPENDENCY CANDIDATES:\n"
            + json.dumps(packets, ensure_ascii=False)
            + "\n\nDISCOVERY MODE:\n"
            + (
                "Inspect this repository for outbound HTTP or Kafka calls missed by the supplied "
                "candidates. Add discovered_edges only when the source call has file:line "
                "evidence and it matches one exact target_entrypoint_id from SERVICE CATALOG by "
                "protocol and operation. Do not report an existing candidate again."
                if discover_missing
                else "Do not discover new edges in this batch; return discovered_edges as []."
            )
        )

    def _save_artifact(
        self,
        runs: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> Path:
        directory = self._artifact_root / "gigacode-verification"
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"
        destination = directory / filename
        payload = json.dumps(
            {"schema_version": 1, "summary": summary, "runs": runs},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination
