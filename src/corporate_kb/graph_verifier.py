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
from gigacode_graph.models import Evidence, GraphEdge, ScanIssue
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
    "required": ["edge_updates", "analyzed_files", "warnings"],
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


class VerificationPayload(_StrictModel):
    edge_updates: list[EdgeUpdate] = Field(default_factory=list)
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
            "confirmed": 0,
            "retargeted": 0,
            "rejected": 0,
            "unresolved": 0,
            "ignored": 0,
            "warnings": [],
            "runs": [],
        }
        if not candidates:
            if progress is not None:
                progress("GigaCode verification: no dependency candidates require review")
            return result, summary

        services = {item.id: item for item in result.service_map.services}
        evidence = {item.id: item for item in result.service_map.evidence}
        repository_by_root = {str(item.path.resolve()): item for item in repositories}
        grouped: dict[str, tuple[RepositoryInput, list[ServiceDependency]]] = {}
        for candidate in candidates:
            source = services.get(candidate.source_service_id)
            repository = None
            if source is not None and source.repository_root:
                repository = repository_by_root.get(str(Path(source.repository_root).resolve()))
            if repository is None and source is not None:
                repository = next(
                    (item for item in repositories if item.name == source.repository),
                    None,
                )
            if repository is None:
                summary["ignored"] += 1
                summary["warnings"].append(
                    f"No checkout found for dependency {candidate.id}"
                )
                continue
            key = str(repository.path.resolve())
            grouped.setdefault(key, (repository, []))[1].append(candidate)

        graph = result.graph.model_copy(deep=True)
        raw_runs: list[dict[str, Any]] = []
        for repository, repository_candidates in grouped.values():
            for offset in range(0, len(repository_candidates), 25):
                batch = repository_candidates[offset : offset + 25]
                if cancel is not None and cancel.is_set():
                    raise GigaCodeCancelled("GigaCode graph verification was cancelled")
                label = f"graph:{repository.name}:{offset // 25 + 1}"
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
                response = self._runner.run_json(
                    checkout=repository.path,
                    prompt=self._prompt(
                        repository=repository,
                        candidates=batch,
                        services=result.service_map,
                        evidence=evidence,
                    ),
                    schema=_RESULT_SCHEMA,
                    cancel=cancel,
                    progress=progress,
                    authentication_url=auth_required,
                    authentication_complete=auth_completed,
                )
                payload = VerificationPayload.model_validate(response.payload)
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
                    }
                )

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
                f"rejected={summary['rejected']}; unresolved={summary['unresolved']}; "
                f"artifact={artifact}"
            )
        return projected, summary

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
                        "status": (
                            "confirmed"
                            if update.confidence in {"HIGH"}
                            else "inferred"
                        ),
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

        issues = list(graph.issues)
        for warning in payload.warnings:
            issues.append(
                ScanIssue(repository=repository.name, message=f"GigaCode: {warning[:2000]}")
            )
        return graph.model_copy(
            update={
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
            snippet=(
                f"{proposed.symbol}: {snippet}" if proposed.symbol and snippet else snippet
            ),
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
    ) -> str:
        catalog = [
            {
                "id": item.id,
                "name": item.name,
                "aliases": item.aliases,
                "module_path": item.module_path,
                "entrypoints": [
                    {"kind": entry.kind, "operation": entry.operation}
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
        )

    def _save_artifact(
        self,
        runs: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> Path:
        directory = self._artifact_root / "gigacode-verification"
        directory.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8]}.json"
        )
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
