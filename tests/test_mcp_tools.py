from __future__ import annotations

import pytest
from mcp.client import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.service import KnowledgeService


@pytest.mark.asyncio
async def test_mcp_lists_and_calls_four_structured_tools(settings_factory) -> None:
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
        assert {tool.name for tool in listed.tools} == {
            "kb_search",
            "kb_get_document",
            "kb_list_documents",
            "kb_stats",
        }

        search = await client.call_tool("kb_search", {"query": "daily limits"})
        assert search.is_error is False
        assert isinstance(search.structured_content, dict)
        assert search.structured_content["result_count"] == 1
        hit = search.structured_content["results"][0]
        assert hit["source_path"] == "limits.md"
        assert "citation" in hit

        document = await client.call_tool("kb_get_document", {"document_id": hit["document_id"]})
        assert document.structured_content["title"] == "Limits Service"

        stats = await client.call_tool("kb_stats", {})
        assert stats.structured_content["document_count"] == 1
        assert stats.structured_content["embedding_provider"] == "hash"

        documents = await client.call_tool("kb_list_documents", {})
        assert documents.structured_content["document_count"] == 1
