from __future__ import annotations

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService


def test_legacy_managed_and_version_searches_share_mcp_search_statistics(settings_factory):
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text(
        "# Limits\n\nThe daily payment limit is 500.", encoding="utf-8"
    )
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    tools = KnowledgeTools(service)
    registry = ManagedToolRegistry(settings.managed_tools_path, tools)
    registry.upsert(
        ManagedToolDefinition(
            name="kb_payment_policy",
            description="Search the current payment policy documents.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
    )

    payloads = [
        tools.search(query="daily payment limit"),
        tools.search_versions(query="daily payment limit", version_scope="history"),
        registry.execute("kb_payment_policy", {"query": "daily payment limit"}),
    ]
    assert all(payload["result_count"] == 1 for payload in payloads)
    assert payloads[0]["results"][0]["document_id"] == payloads[2]["results"][0]["document_id"]
    snapshot = tools.usage.snapshot()
    assert snapshot["search_count"] == 3
    assert snapshot["total_calls"] == 4  # Three retrievals and the managed-tool invocation.
    assert snapshot["calls_by_tool"] == {
        "kb_search": 1,
        "kb_search_versions": 2,
        "kb_payment_policy": 1,
    }
    tokens = sum(payload["context_token_count"] for payload in payloads)
    assert snapshot["average_context_tokens"] == round(tokens / 3, 2)
    assert snapshot["average_results"] == 1.0
