"""Stable JSON tool facade shared by MCP and the HTTP API."""

from __future__ import annotations

from typing import Any

from gigacode_graph.models import NodeType
from gigacode_graph.service import GraphService


class GraphTools:
    def __init__(self, service: GraphService) -> None:
        self._service = service

    def overview(self) -> dict[str, Any]:
        return self._service.overview()

    def search(
        self,
        query: str,
        *,
        node_types: list[NodeType] | None = None,
        service: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._service.search(
            query,
            node_types=node_types,
            service=service,
            limit=limit,
        )

    def service(self, service: str) -> dict[str, Any]:
        return self._service.service_details(service)

    def dependencies(
        self,
        service: str,
        *,
        direction: str = "outgoing",
        depth: int = 1,
    ) -> dict[str, Any]:
        return self._service.dependencies(service, direction=direction, depth=depth)

    def business_operations(self, service: str, *, limit: int = 100) -> dict[str, Any]:
        return self._service.business_operations(service, limit=limit)

    def data_model(
        self,
        *,
        service: str | None = None,
        table: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._service.data_model(service=service, table=table, limit=limit)

    def evidence(self, evidence_ids: list[str]) -> dict[str, Any]:
        return self._service.evidence(evidence_ids)
