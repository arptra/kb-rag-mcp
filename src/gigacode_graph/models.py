"""Versioned graph metamodel shared by scanner, API, UI, CLI, and MCP."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NodeType = Literal[
    "Repository",
    "Service",
    "BusinessOperation",
    "BusinessRule",
    "EntryPoint",
    "ExitPoint",
    "CodeSymbol",
    "DomainEntity",
    "Table",
    "Column",
    "Event",
    "ExternalSystem",
]
EdgeType = Literal[
    "CONTAINS",
    "IMPLEMENTS",
    "TRIGGERED_BY",
    "EXITS_VIA",
    "HANDLED_BY",
    "CALLS",
    "DEPENDS_ON",
    "GUARDED_BY",
    "DECLARES_ENTITY",
    "MANAGES_SCHEMA",
    "MAPS_TO",
    "HAS_COLUMN",
    "READS",
    "WRITES",
    "PUBLISHES",
    "CONSUMES",
]
Confidence = Literal["DECLARED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"]


class GraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(GraphModel):
    id: str
    repository: str
    commit: str | None = None
    file: str
    line: int = Field(ge=1)
    snippet: str
    extractor: str
    confidence: Confidence = "HIGH"


class GraphNode(GraphModel):
    id: str
    type: NodeType
    label: str
    service_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class GraphEdge(GraphModel):
    id: str
    source: str
    target: str
    type: EdgeType
    label: str = ""
    confidence: Confidence = "HIGH"
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class ScanIssue(GraphModel):
    repository: str
    file: str | None = None
    message: str
    severity: Literal["warning", "error"] = "warning"


class IngestionRecord(GraphModel):
    source_type: Literal["git", "local"]
    source: str
    requested_ref: str | None = None
    checkout_path: str
    commit: str | None = None
    action: Literal["cloned", "updated", "reused", "local"]


class IngestionManifest(GraphModel):
    schema_version: int = 1
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    graph_generated_at: datetime
    graph_path: str
    repositories: list[IngestionRecord] = Field(default_factory=list)


class GraphSnapshot(GraphModel):
    schema_version: int = 1
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    issues: list[ScanIssue] = Field(default_factory=list)

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for node in self.nodes:
            by_type[node.type] = by_type.get(node.type, 0) + 1
        edge_types: dict[str, int] = {}
        for edge in self.edges:
            edge_types[edge.type] = edge_types.get(edge.type, 0) + 1
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at.isoformat(),
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "evidence_count": len(self.evidence),
            "issue_count": len(self.issues),
            "nodes_by_type": by_type,
            "edges_by_type": edge_types,
        }
