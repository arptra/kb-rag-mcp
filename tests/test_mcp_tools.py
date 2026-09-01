from __future__ import annotations

import json
from typing import Any

import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.service import KnowledgeService


class _BatchJob:
    def __init__(self, *, name: str, index_id: str | None, position: int) -> None:
        self.id = f"batch-job-{position}"
        self.index_id = index_id or f"{name}-index"
        self.target_id = f"{name}-repository"

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "id": self.id,
            "index_id": self.index_id,
            "target_id": self.target_id,
            "status": "queued",
        }


class _BatchCatalog:
    def __init__(self, *, gigacode_available: bool) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gigacode_available = gigacode_available

    def gigacode_status(self, *, refresh: bool) -> dict[str, Any]:
        assert refresh is True
        return {
            "available": self.gigacode_available,
            "error": None if self.gigacode_available else "GigaCode executable is unavailable",
        }

    def start_repository_ingestion(self, **arguments: Any) -> _BatchJob:
        self.calls.append(arguments)
        return _BatchJob(
            name=str(arguments["name"]),
            index_id=arguments["index_id"],
            position=len(self.calls),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gigacode_available", "queued_analysis", "fallback_count"),
    [(True, "gigacode", 0), (False, "static", 3)],
)
async def test_mcp_connects_service_repositories_in_batch_with_static_fallback(
    settings_factory,
    gigacode_available: bool,
    queued_analysis: str,
    fallback_count: int,
) -> None:
    settings = settings_factory()
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    catalog = _BatchCatalog(gigacode_available=gigacode_available)
    mcp_server = create_mcp_server(service, catalog=catalog)  # type: ignore[arg-type]

    async with Client(mcp_server) as client:
        listed = await client.list_tools()
        names = {item.name for item in listed}
        assert "kb_connect_services_batch" in names, ",".join(sorted(names))
        batch_tool = next(item for item in listed if item.name == "kb_connect_services_batch")
        assert batch_tool.inputSchema["properties"]["default_ref"]["default"] == "master"

        result = await client.call_tool(
            "kb_connect_services_batch",
            {
                "services": [
                    {
                        "service_name": "payments-service",
                        "git_url": "ssh://git.example/payments.git",
                    },
                    {
                        "service_name": "ledger-service",
                        "git_url": "ssh://git.example/ledger.git",
                        "ref": "release/2026.08",
                        "index_id": "ledger-index",
                    },
                    {
                        "service_name": "static-fallback-service",
                        "git_url": "ssh://git.example/static-fallback.git",
                    },
                ]
            },
        )

    assert result.is_error is False
    assert result.data["status"] == "queued"
    assert result.data["service_count"] == 3
    assert result.data["queued_count"] == 3
    assert result.data["failed_count"] == 0
    assert result.data["static_fallback_count"] == fallback_count
    assert result.data["default_ref"] == "master"
    assert [item["queued_analysis"] for item in result.data["queued"]] == [
        queued_analysis,
        queued_analysis,
        queued_analysis,
    ]
    assert result.data["queued"][2]["fallback_reason"] == (
        None if gigacode_available else "GigaCode executable is unavailable"
    )
    assert result.data["queued"][0]["poll"] == {
        "tool": "kb_generate_system_ssot",
        "arguments": {"action": "status", "job_id": "batch-job-1"},
    }
    assert [(item["name"], item["ref"], item["generation_mode"]) for item in catalog.calls] == [
        ("payments-service", "master", queued_analysis),
        ("ledger-service", "release/2026.08", queued_analysis),
        ("static-fallback-service", "master", queued_analysis),
    ]
    assert all(item["validate_gigacode"] is False for item in catalog.calls)


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
            "ssot_context",
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
async def test_mcp_exposes_one_cross_service_ssot_tool(settings_factory) -> None:
    settings = settings_factory(ssot_context_tokens=600)
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "payments.md").write_text(
        """---
document_type: ssot
service: payments-service
status: current
---
# Payments

payments-service calls limits-service before committing a payment.
""",
        encoding="utf-8",
    )
    (settings.knowledge_dir / "limits.md").write_text(
        """---
document_type: ssot
service: limits-service
status: current
---
# Limits

limits-service owns the daily limit decision for payments.
""",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)

    async with Client(create_mcp_server(service)) as client:
        result = await client.call_tool(
            "ssot_context",
            {
                "question": "How should payment use the daily limit?",
                "mode": "implementation",
            },
        )

    assert result.is_error is False
    assert {item["service"] for item in result.data["services"]} == {
        "payments-service",
        "limits-service",
    }
    assert result.data["context_token_count"] <= 600
