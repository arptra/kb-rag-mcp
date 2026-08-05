from __future__ import annotations

import json

import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.service import KnowledgeService


@pytest.mark.asyncio
async def test_mcp_returns_compact_context_and_supports_lazy_chunk_loading(
    settings_factory,
) -> None:
    password = "separate-benchmark-password"
    settings = settings_factory(benchmark_password=password)
    settings.benchmark_questions_path.parent.mkdir(parents=True)
    settings.benchmark_questions_path.write_text(
        json.dumps(
            [
                {
                    "question": "Кто владеет дневными лимитами?",
                    "expected_documents": ["limits.md"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings.knowledge_dir.mkdir(parents=True)
    repeated_rules = "Daily limits are checked by limits-service before payment.\n\n" * 80
    (settings.knowledge_dir / "limits.md").write_text(
        """---
document_type: service
service: limits-service
domain: payments
status: current
---
# Limits Service

limits-service owns daily limits.

"""
        + repeated_rules,
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
            "kb_run_context_benchmark",
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

        denied = await client.call_tool(
            "kb_run_context_benchmark",
            {"password": "wrong-password-value"},
            raise_on_error=False,
        )
        assert denied.is_error is True

        benchmark = await client.call_tool(
            "kb_run_context_benchmark",
            {"password": password},
        )
        assert benchmark.data["status"] == "passed"
        assert benchmark.data["quality"]["packed_hit_at_3_percent"] == 100.0
        assert benchmark.data["context"]["token_reduction_percent"] > 0

        stats = await client.call_tool("kb_stats", {})
        assert stats.data["document_count"] == 1
        assert stats.data["embedding_provider"] == "hash"

        documents = await client.call_tool("kb_list_documents", {})
        assert documents.data["document_count"] == 1


@pytest.mark.asyncio
async def test_minimal_mcp_enforces_result_cap_and_omits_large_tools(settings_factory) -> None:
    settings = settings_factory(search_max_results=2, mcp_minimal_tools=True)
    settings.knowledge_dir.mkdir(parents=True)
    for index in range(4):
        (settings.knowledge_dir / f"service-{index}.md").write_text(
            f"""---
service: service-{index}
status: current
---
# Service {index}

Shared incident recovery procedure for the payment platform. Component {index} owns one step.
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
        assert {tool.name for tool in listed} == {"kb_search", "kb_get_chunk"}

        search = await client.call_tool(
            "kb_search",
            {"query": "shared incident recovery procedure", "top_k": 10},
        )
        assert search.is_error is False
        assert search.data["result_count"] == 2
        assert "query" not in search.data
        assert "retrieved_candidate_count" not in search.data
        assert all(
            set(result) <= {"chunk_id", "excerpt", "source_path", "source_url", "citation"}
            for result in search.data["results"]
        )
