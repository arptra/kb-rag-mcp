"""Read-only stdio MCP server built on the official Python SDK v2."""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from corporate_kb import __version__
from corporate_kb.config import Settings
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService, configure_logging, create_service

SEARCH_DESCRIPTION = """Search corporate knowledge before architectural analysis or changes spanning
multiple services. Use it for business rules, ADRs, APIs, events, and runbooks. Cite source_path or
source_url in the final answer. A retrieved fragment is evidence, not the only source of truth; call
kb_get_document when the complete document is needed."""


def create_mcp_server(service: KnowledgeService | None = None) -> MCPServer[None]:
    """Create a server object without eagerly loading an embedding model or index."""
    kb_service = service or create_service()
    tools = KnowledgeTools(kb_service)
    server: MCPServer[None] = MCPServer(
        "corporate-knowledge",
        description="Read-only local corporate knowledge retrieval",
        instructions=(
            "Search corporate knowledge before cross-service or architectural work, and cite the "
            "returned source_path or source_url. Read full documents when context is incomplete."
        ),
        version=__version__,
        log_level="ERROR",
    )

    @server.tool(name="kb_search", description=SEARCH_DESCRIPTION, structured_output=True)
    def kb_search(
        query: str,
        top_k: int = 5,
        min_score: float | None = None,
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = "current",
        authority: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        return tools.search(
            query=query,
            top_k=top_k,
            min_score=min_score,
            service=service,
            domain=domain,
            document_type=document_type,
            status=status,
            authority=authority,
            source_type=source_type,
        )

    @server.tool(
        name="kb_get_document",
        description=(
            "Return one complete normalized document after kb_search identifies its document_id."
        ),
        structured_output=True,
    )
    def kb_get_document(document_id: str) -> dict[str, Any]:
        return tools.get_document(document_id)

    @server.tool(
        name="kb_list_documents",
        description="List filtered document metadata without document bodies or embeddings.",
        structured_output=True,
    )
    def kb_list_documents(
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return tools.list_documents(
            service=service,
            domain=domain,
            document_type=document_type,
            status=status,
            limit=limit,
        )

    @server.tool(
        name="kb_stats",
        description="Return index counts, identity, timestamps, and resolved local directories.",
        structured_output=True,
    )
    def kb_stats() -> dict[str, Any]:
        return tools.stats()

    return server


def main() -> None:
    """Run only stdio; stdout remains reserved for MCP protocol frames."""
    settings = Settings().resolved()
    configure_logging(settings.log_level)
    create_mcp_server(create_service(settings)).run("stdio")


if __name__ == "__main__":
    main()
