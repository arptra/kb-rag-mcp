"""Read-only MCP server that gives GigaCode structured repository graph context."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from gigacode_graph import __version__
from gigacode_graph.config import GraphSettings
from gigacode_graph.models import NodeType
from gigacode_graph.service import GraphService
from gigacode_graph.store import JsonGraphStore
from gigacode_graph.tools import GraphTools

logger = logging.getLogger(__name__)


def create_mcp_server(
    service: GraphService,
    *,
    auth: AuthProvider | None = None,
) -> FastMCP:
    """Create the server without transport side effects, suitable for tests and embedding."""
    tools = GraphTools(service)
    server = FastMCP(
        "gigacode-repository-graph",
        version=__version__,
        auth=auth,
        instructions=(
            "Use this read-only graph before planning cross-service changes. Start with "
            "code_graph_search or code_graph_service, follow dependency paths, and cite the "
            "returned repository/file/line evidence. Confidence is part of every inferred edge; "
            "UNRESOLVED and LOW facts must be verified in source before editing."
        ),
    )

    annotations = {"readOnlyHint": True, "openWorldHint": False}

    @server.tool(
        name="code_graph_overview",
        description=(
            "List indexed services, graph counts, extraction issues, and snapshot identity. "
            "Use this first when the relevant service is unknown."
        ),
        annotations=annotations,
    )
    def overview() -> dict[str, Any]:
        return tools.overview()

    @server.tool(
        name="code_graph_search",
        description=(
            "Search services, operations, rules, entry points, entities, tables, columns, events, "
            "and code symbols. Results include source evidence when available."
        ),
        annotations=annotations,
    )
    def search(
        query: str,
        node_types: list[NodeType] | None = None,
        service: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return tools.search(query, node_types=node_types, service=service, limit=limit)

    @server.tool(
        name="code_graph_service",
        description=(
            "Return one service dossier: incoming/outgoing dependencies, business operations, "
            "entry points, events, database model, access facts, and evidence."
        ),
        annotations=annotations,
    )
    def service_details(service: str) -> dict[str, Any]:
        return tools.service(service)

    @server.tool(
        name="code_graph_dependencies",
        description=(
            "Traverse evidence-backed service dependencies. Direction is outgoing, incoming, or "
            "both; depth is 1..10. Inspect confidence before using a path in an "
            "implementation plan."
        ),
        annotations=annotations,
    )
    def dependencies(
        service: str,
        direction: str = "outgoing",
        depth: int = 1,
    ) -> dict[str, Any]:
        return tools.dependencies(service, direction=direction, depth=depth)

    @server.tool(
        name="code_graph_business_operations",
        description=(
            "Return extracted business operations for a service with triggers, raw conditional "
            "rules, handlers, related calls/events, and exact source evidence."
        ),
        annotations=annotations,
    )
    def business_operations(service: str, limit: int = 100) -> dict[str, Any]:
        return tools.business_operations(service, limit=limit)

    @server.tool(
        name="code_graph_data_model",
        description=(
            "Return JPA entities, database tables/columns, migrations, and operation READS/WRITES "
            "edges. Supply service or table."
        ),
        annotations=annotations,
    )
    def data_model(
        service: str | None = None,
        table: str | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        return tools.data_model(service=service, table=table, limit=limit)

    @server.tool(
        name="code_graph_evidence",
        description=(
            "Resolve graph evidence IDs to repository, commit, file, line, snippet, extractor, "
            "and confidence for source verification."
        ),
        annotations=annotations,
    )
    def evidence(evidence_ids: list[str]) -> dict[str, Any]:
        return tools.evidence(evidence_ids)

    return server


def main() -> None:
    """Run stdio MCP for GigaCode; stdout remains reserved for protocol frames."""
    settings = GraphSettings().resolved()
    logging.basicConfig(level=settings.log_level)
    try:
        service = GraphService(JsonGraphStore(settings.store_path))
        logger.info("Loaded repository graph: %s", service.overview())
        create_mcp_server(service).run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
