from __future__ import annotations

import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.service import KnowledgeService


@pytest.mark.asyncio
async def test_mcp_returns_compact_context_and_supports_lazy_chunk_loading(
    settings_factory,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text(
        """---
document_type: service
service: limits-service
domain: payments
status: current
---
# Limits Service

limits-service owns daily limits.
""",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)
    server = create_mcp_server(service)

    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed} == {
            "kb_search",
            "kb_get_document",
            "kb_get_chunk",
            "kb_list_documents",
            "kb_stats",
        }

        search = await client.call_tool("kb_search", {"query": "daily limits"})
        assert search.is_error is False
        assert isinstance(search.data, dict)
        assert search.data["result_count"] == 1
        hit = search.data["results"][0]
        assert hit["source_path"] == "limits.md"
        assert "citation" in hit
        assert "text" not in hit
        assert hit["excerpt_tokens"] <= settings.search_excerpt_tokens
        assert search.data["context_token_count"] <= settings.search_context_tokens

        document = await client.call_tool("kb_get_document", {"document_id": hit["document_id"]})
        assert document.data["title"] == "Limits Service"
        assert document.data["content_tokens"] <= settings.document_context_tokens

        chunk = await client.call_tool("kb_get_chunk", {"chunk_id": hit["chunk_id"]})
        assert chunk.data["document_id"] == hit["document_id"]
        assert chunk.data["text_tokens"] <= settings.document_context_tokens

        stats = await client.call_tool("kb_stats", {})
        assert stats.data["document_count"] == 1
        assert stats.data["embedding_provider"] == "hash"

        documents = await client.call_tool("kb_list_documents", {})
        assert documents.data["document_count"] == 1
