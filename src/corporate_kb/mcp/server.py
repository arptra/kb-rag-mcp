"""Corporate knowledge server built with standalone FastMCP."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider

from corporate_kb import __version__
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.feature_context import FeatureContextPlanner
from corporate_kb.mcp.managed_tools import ManagedToolRegistry
from corporate_kb.mcp.tool_overrides import BuiltinToolOverrideRegistry
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
the tool discovers likely roots. The stable graph_revision changes after rebuild and next_calls
contains exact kb_search_index calls for affected indexes. LOW/UNRESOLVED facts and runtime order
must be verified."""
SYSTEM_GRAPH_DESCRIPTION = """Read the standalone system dependency graph for feature planning.
This tool never reads, writes, embeds, or rebuilds a RAG index. It identifies root and affected
services, HTTP/Kafka calls, confidence and source evidence from the latest graph snapshot, then
returns explicit next_calls to kb_search_index for implementation details when an index is bound.
Call this first for a complex or cross-service feature."""
INDEX_SEARCH_DESCRIPTION = """Search one exact RAG index selected by kb_system_graph. Pass the
returned index_id instead of guessing a managed MCP tool name. Use this for follow-up questions
after graph routing and cite source_path/source_url from the results."""
GENERATE_SSOT_DESCRIPTION = """Coordinate source-backed SSOT generation with the model calling this
MCP tool. The RAG service never calls an LLM HTTP endpoint directly. Use action='options' to list
indexes, cloned repositories and services; action='clone' for a missing Git repository;
action='prepare' to scan selected
repository_ids/service_ids or all_services; action='status' to poll; action='context' for analysis
and the file manifest; action='read_file' for more source; and action='submit' to save Markdown and
rebuild the index. For action='prepare', generation_mode='client' keeps generation in the calling
client, while generation_mode='gigacode' launches an installed GigaCode CLI headlessly on the
server, scans the checkout read-only, writes structured SSOT, and rebuilds the index automatically.
Check
workflow.gigacode.available in action='options' first. With client mode and the distributed stdio
proxy, finish through kb_save_and_upload_ssot so a temp copy exists on the user's machine."""
BUILTIN_TOOL_DESCRIPTIONS = {
    "ssot_context": SSOT_DESCRIPTION,
    "kb_feature_context": FEATURE_CONTEXT_DESCRIPTION,
    "kb_system_graph": SYSTEM_GRAPH_DESCRIPTION,
    "kb_search_index": INDEX_SEARCH_DESCRIPTION,
    "kb_generate_system_ssot": GENERATE_SSOT_DESCRIPTION,
    "kb_search": SEARCH_DESCRIPTION,
    "kb_get_document": (
        "Return a bounded extract from one normalized document after kb_search identifies its "
        "document_id."
    ),
    "kb_get_chunk": (
        "Return a bounded source chunk selected by chunk_id from kb_search. Prefer this over "
        "kb_get_document when only one search result needs more context."
    ),
    "kb_run_context_benchmark": (
        "Run the protected read-only context benchmark. Before calling, ask the user to enter "
        "the separate benchmark password; never guess or reuse the normal API Bearer token."
    ),
    "kb_list_documents": "List filtered document metadata without document bodies or embeddings.",
    "kb_stats": "Return index counts, identity, timestamps, and resolved local directories.",
}
logger = logging.getLogger(__name__)


def create_mcp_server(
    service: KnowledgeService | None = None,
    *,
    auth: AuthProvider | None = None,
    knowledge_tools: KnowledgeTools | None = None,
    managed_tools: ManagedToolRegistry | None = None,
    feature_context: FeatureContextPlanner | None = None,
    catalog: RagCatalog | None = None,
    builtin_tool_overrides: BuiltinToolOverrideRegistry | None = None,
) -> FastMCP:
    """Create a FastMCP server without eagerly loading a model or index."""
    kb_service = service or create_service()
    tools = knowledge_tools or KnowledgeTools(kb_service)
    registry = managed_tools or ManagedToolRegistry(kb_service.settings.managed_tools_path, tools)

    def description(name: str) -> str:
        default = BUILTIN_TOOL_DESCRIPTIONS[name]
        if builtin_tool_overrides is None:
            return default
        return builtin_tool_overrides.description_for(name, default)

    server = FastMCP(
        "corporate-knowledge",
        instructions=(
            "Call kb_system_graph before cross-service implementation work. It reads the "
            "standalone graph and returns explicit kb_search_index next_calls without querying "
            "RAG itself. Cite graph evidence and later RAG source_path/source_url separately."
        ),
        version=__version__,
        auth=auth,
    )

    @server.tool(
        name="ssot_context",
        description=description("ssot_context"),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def ssot_context(
        question: str,
        mode: str = "implementation",
    ) -> dict[str, Any]:
        return tools.ssot_context(question=question, mode=mode)

    @server.tool(
        name="kb_search",
        description=description("kb_search"),
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
            name="kb_system_graph",
            description=description("kb_system_graph"),
            annotations={"readOnlyHint": True, "openWorldHint": False},
        )
        def kb_system_graph(
            feature: str,
            start_service: str | None = None,
            max_hops: int = 2,
            direction: Literal["incoming", "outgoing", "both"] = "both",
            min_confidence: Literal[
                "DECLARED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"
            ] = "LOW",
            include_unresolved: bool = True,
        ) -> dict[str, Any]:
            return feature_context.graph_route(
                feature=feature,
                start_service=start_service,
                max_hops=max_hops,
                direction=direction,
                min_confidence=min_confidence,
                include_unresolved=include_unresolved,
            )

        @server.tool(
            name="kb_feature_context",
            description=description("kb_feature_context"),
            annotations={"readOnlyHint": True, "openWorldHint": False},
        )
        def kb_feature_context(
            feature: str,
            start_service: str | None = None,
            max_hops: int = 2,
            top_k_per_service: int = 2,
            direction: Literal["incoming", "outgoing", "both"] = "both",
            min_confidence: Literal[
                "DECLARED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"
            ] = "LOW",
            include_unresolved: bool = True,
        ) -> dict[str, Any]:
            return feature_context.build(
                feature=feature,
                start_service=start_service,
                max_hops=max_hops,
                top_k_per_service=top_k_per_service,
                direction=direction,
                min_confidence=min_confidence,
                include_unresolved=include_unresolved,
            )

    if catalog is not None:

        @server.tool(
            name="kb_search_index",
            description=description("kb_search_index"),
            annotations={"readOnlyHint": True, "openWorldHint": False},
        )
        def kb_search_index(
            index_id: str,
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
            result = catalog.tools_for(index_id).search(
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
            result["index_id"] = index_id
            return result

        @server.tool(
            name="kb_generate_system_ssot",
            description=description("kb_generate_system_ssot"),
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
        )
        def kb_generate_system_ssot(
            action: Literal[
                "options",
                "clone",
                "prepare",
                "status",
                "context",
                "read_file",
                "submit",
            ] = "options",
            index_id: str | None = None,
            repository_ids: list[str] | None = None,
            service_ids: list[str] | None = None,
            all_services: bool = False,
            refresh_analysis: bool = True,
            generation_mode: Literal["client", "gigacode"] = "client",
            job_id: str | None = None,
            repository_name: str | None = None,
            git_url: str | None = None,
            ref: str | None = None,
            service_id: str | None = None,
            repository_id: str | None = None,
            file_path: str | None = None,
            offset: int = 0,
            max_chars: int = 20_000,
            content: str | None = None,
            finalize: bool = True,
        ) -> dict[str, Any]:
            return catalog.ssot_generation_request(
                action=action,
                index_id=index_id,
                repository_ids=repository_ids,
                service_ids=service_ids,
                all_services=all_services,
                refresh_analysis=refresh_analysis,
                generation_mode=generation_mode,
                job_id=job_id,
                repository_name=repository_name,
                git_url=git_url,
                ref=ref,
                service_id=service_id,
                repository_id=repository_id,
                file_path=file_path,
                offset=offset,
                max_chars=max_chars,
                content=content,
                finalize=finalize,
            )

    @server.tool(
        name="kb_get_document",
        description=description("kb_get_document"),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_get_document(
        document_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return tools.get_document(document_id, max_tokens=max_tokens)

    @server.tool(
        name="kb_get_chunk",
        description=description("kb_get_chunk"),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_get_chunk(
        chunk_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        return tools.get_chunk(chunk_id, max_tokens=max_tokens)

    @server.tool(
        name="kb_run_context_benchmark",
        description=description("kb_run_context_benchmark"),
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_run_context_benchmark(password: str) -> dict[str, Any]:
        return tools.run_context_benchmark(password)

    @server.tool(
        name="kb_list_documents",
        description=description("kb_list_documents"),
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
        description=description("kb_stats"),
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
        builtin_tool_overrides = BuiltinToolOverrideRegistry(
            settings.builtin_tool_overrides_path
        )
        create_mcp_server(
            service,
            knowledge_tools=tools,
            managed_tools=managed_tools,
            feature_context=FeatureContextPlanner(catalog),
            catalog=catalog,
            builtin_tool_overrides=builtin_tool_overrides,
        ).run(
            transport="stdio",
            show_banner=False,
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
