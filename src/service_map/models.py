"""Stable file contract for the lightweight map of services and their interfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gigacode_graph.models import Confidence

InterfaceKind = Literal["HTTP", "KAFKA", "SCHEDULED", "GRPC", "CLI", "UNKNOWN"]
InterfaceDirection = Literal["inbound", "outbound"]


class ServiceMapModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceMapEvidence(ServiceMapModel):
    id: str
    repository: str
    commit: str | None = None
    file: str
    line: int = Field(ge=1)
    snippet: str
    extractor: str
    confidence: Confidence = "HIGH"


class ServiceInterface(ServiceMapModel):
    id: str
    kind: InterfaceKind
    direction: InterfaceDirection
    operation: str
    target_hint: str | None = None
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class ServiceDependency(ServiceMapModel):
    id: str
    source_service_id: str
    target_service_id: str | None = None
    target_hint: str
    protocol: InterfaceKind
    operation: str
    confidence: Confidence
    resolved: bool
    status: Literal["confirmed", "inferred", "unresolved", "rejected"] = "inferred"
    origin: Literal["declared", "static", "gigacode", "static+gigacode"] = "static"
    evidence_ids: list[str] = Field(default_factory=list)


class ServiceRecord(ServiceMapModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    repository: str
    repository_path: str
    repository_root: str | None = None
    module_path: str = "."
    component_paths: list[str] = Field(default_factory=list)
    module_state: Literal["active", "empty", "unsupported"] = "active"
    build_system: Literal["maven", "gradle", "unknown"] = "unknown"
    source_url: str | None = None
    commit: str | None = None
    owner: str | None = None
    entrypoints: list[ServiceInterface] = Field(default_factory=list)
    outbound_interfaces: list[ServiceInterface] = Field(default_factory=list)


class ServiceMapIssue(ServiceMapModel):
    repository: str
    file: str | None = None
    message: str
    severity: Literal["warning", "error"] = "warning"


class ServiceMapSnapshot(ServiceMapModel):
    schema_version: int = 2
    snapshot_id: str | None = None
    analysis_mode: Literal["static", "static+gigacode", "partial"] = "static"
    verification: dict[str, Any] = Field(default_factory=dict)
    algorithm: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    services: list[ServiceRecord] = Field(default_factory=list)
    dependencies: list[ServiceDependency] = Field(default_factory=list)
    evidence: list[ServiceMapEvidence] = Field(default_factory=list)
    issues: list[ServiceMapIssue] = Field(default_factory=list)

    def overview(self) -> dict[str, object]:
        entrypoint_count = sum(len(item.entrypoints) for item in self.services)
        outbound_count = sum(len(item.outbound_interfaces) for item in self.services)
        unresolved_count = sum(not item.resolved for item in self.dependencies)
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "analysis_mode": self.analysis_mode,
            "verification": self.verification,
            "algorithm": self.algorithm,
            "generated_at": self.generated_at.isoformat(),
            "service_count": len(self.services),
            "entrypoint_count": entrypoint_count,
            "outbound_interface_count": outbound_count,
            "dependency_count": len(self.dependencies),
            "unresolved_dependency_count": unresolved_count,
            "evidence_count": len(self.evidence),
            "issue_count": len(self.issues),
            "services": [
                {
                    "id": item.id,
                    "name": item.name,
                    "repository": item.repository,
                    "repository_root": item.repository_root,
                    "module_path": item.module_path,
                    "component_paths": item.component_paths,
                    "module_state": item.module_state,
                    "build_system": item.build_system,
                    "owner": item.owner,
                    "entrypoint_count": len(item.entrypoints),
                    "outbound_interface_count": len(item.outbound_interfaces),
                }
                for item in self.services
            ],
        }
