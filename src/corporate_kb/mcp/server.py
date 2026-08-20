"""Read-only corporate knowledge server built with standalone FastMCP."""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from corporate_kb import __version__
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.feature_context import FeatureContextPlanner
from corporate_kb.mcp.managed_tools import ManagedToolRegistry
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import (
    KnowledgeService,
    configure_logging,
    create_service,
    create_ssot_service,
)
from corporate_kb.usage import UsageTracker

SEARCH_DESCRIPTION = """Search corporate knowledge before architectural analysis or changes spanning
multiple services. Use it for business rules, ADRs, APIs, events, and runbooks. Cite source_path or
source_url in the final answer. Results are compact, diverse excerpts under a token budget. A
retrieved fragment is evidence, not the only source of truth; call kb_get_chunk when a specific
chunk needs more context, or kb_get_document for document-level context."""
SSOT_DESCRIPTION = """Answer a current business or implementation question from service SSOTs.
Use this as one self-contained call for a feature spanning services: the server discovers involved
services, performs additional filtered searches internally, and returns one compact grouped brief.
Do not call kb_search repeatedly to reconstruct the same SSOT context."""
FEATURE_CONTEXT_DESCRIPTION = """Plan a feature with the static service call graph and the RAG index
owned by each affected repository. Use this before cross-service implementation work. It returns
callers, callees, API/event operations, statically linked invocation triggers, exact source
evidence, and compact RAG excerpts grouped by service. Supply start_service when known; otherwise
the tool discovers likely roots. LOW/UNRESOLVED facts and runtime order must be verified."""
logger = logging.getLogger(__name__)


def create_mcp_server(
    service: KnowledgeService | None = None,
    *,
    auth: AuthProvider | None = None,
    knowledge_tools: KnowledgeTools | None = None,
    managed_tools: ManagedToolRegistry | None = None,
    feature_context: FeatureContextPlanner | None = None,
) -> FastMCP:
    """Create a FastMCP server without eagerly loading a model or index."""
    kb_service = service or create_service()
    tools = knowledge_tools or KnowledgeTools(kb_service)
    registry = managed_tools or ManagedToolRegistry(kb_service.settings.managed_tools_path, tools)
    server = FastMCP(
        "corporate-knowledge",
        instructions=(
            "Call kb_feature_context before cross-service implementation work so the service "
            "graph selects the affected RAG indexes. Cite returned source_path/source_url and "
            "graph evidence. Use kb_search for narrower follow-up questions and kb_get_chunk only "
            "when one selected result needs more context."
        ),
        version=__version__,
        auth=auth,
    )

    @server.tool(
        name="ssot_context",
        description=SSOT_DESCRIPTION,
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def ssot_context(
        question: str,
        mode: str = "implementation",
    ) -> dict[str, Any]:
        return tools.ssot_context(question=question, mode=mode)

    @server.tool(
        name="kb_search",
        description=SEARCH_DESCRIPTION,
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_search(
        query: str,
        top_k: int = 3,
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

    if feature_context is not None:

        @server.tool(
            name="kb_feature_context",
            description=FEATURE_CONTEXT_DESCRIPTION,
            annotations={"readOnlyHint": True, "openWorldHint": False},
        )
        def kb_feature_context(
            feature: str,
            start_service: str | None = None,
            max_hops: int = 2,
            top_k_per_service: int = 2,
        ) -> dict[str, Any]:
            return feature_context.build(
                feature=feature,
                start_service=start_service,
                max_hops=max_hops,
                top_k_per_service=top_k_per_service,
            )

    @server.tool(
        name="kb_get_document",
        description=(
            "Return a bounded extract from one normalized document after kb_search identifies its "
            "document_id."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_get_document(
        document_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return tools.get_document(document_id, max_tokens=max_tokens)

    @server.tool(
        name="kb_get_chunk",
        description=(
            "Return a bounded source chunk selected by chunk_id from kb_search. Prefer this over "
            "kb_get_document when only one search result needs more context."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_get_chunk(
        chunk_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return tools.get_chunk(chunk_id, max_tokens=max_tokens)

    @server.tool(
        name="kb_run_context_benchmark",
        description=(
            "Run the protected read-only context benchmark. Before calling, ask the user to enter "
            "the separate benchmark password; never guess or reuse the normal API Bearer token."
        ),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_run_context_benchmark(password: str) -> dict[str, Any]:
        return tools.run_context_benchmark(password)

    @server.tool(
        name="kb_list_documents",
        description="List filtered document metadata without document bodies or embeddings.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
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
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_stats() -> dict[str, Any]:
        return tools.stats()

    for definition in registry.list():
        server.add_tool(registry.create_tool(definition))

    return server


def main() -> None:
    """Run only stdio; stdout remains reserved for MCP protocol frames."""
    settings = Settings().resolved()
    configure_logging(settings.log_level)
    try:
        service = create_service(settings)
        stats = service.load_read_index()
        logger.info(
            "Preloaded knowledge index: documents=%d chunks=%d provider=%s",
            stats.document_count,
            stats.chunk_count,
            stats.embedding_provider,
        )
        ssot_service = None
        if settings.ssot_enabled:
            ssot_service = create_ssot_service(settings, provider=service.provider)
            ssot_stats = ssot_service.load_read_index()
            logger.info(
                "Preloaded global SSOT index: documents=%d chunks=%d",
                ssot_stats.document_count,
                ssot_stats.chunk_count,
            )
        usage = UsageTracker()
        tools = KnowledgeTools(service, ssot_service=ssot_service, usage=usage)
        catalog = RagCatalog(settings, service, tools, usage)
        managed_tools = ManagedToolRegistry(
            settings.managed_tools_path,
            tools,
            index_tools=catalog.tools_for,
            index_exists=catalog.has_index,
        )
        create_mcp_server(
            service,
            knowledge_tools=tools,
            managed_tools=managed_tools,
            feature_context=FeatureContextPlanner(catalog),
        ).run(
            transport="stdio",
            show_banner=False,
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
