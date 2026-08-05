from __future__ import annotations

import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService


def _definition() -> ManagedToolDefinition:
    return ManagedToolDefinition.model_validate(
        {
            "name": "kb_search_runbooks",
            "description": "Search operational runbooks for incident response evidence.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Incident or operational question",
                    }
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "defaults": {"top_k": 2, "document_type": "runbook", "status": "current"},
        }
    )


@pytest.mark.asyncio
async def test_managed_tool_persists_schema_and_executes_filtered_search(
    settings_factory,
) -> None:
    settings = settings_factory()
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "payments-runbook.md").write_text(
        """---
document_type: runbook
status: current
---
# Payments Runbook

Restart payments-worker after checking the queue depth.
""",
        encoding="utf-8",
    )
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    service.build_index(force=True)
    tools = KnowledgeTools(service)
    registry = ManagedToolRegistry(settings.managed_tools_path, tools)
    definition = registry.upsert(_definition())
    server = create_mcp_server(service, knowledge_tools=tools, managed_tools=registry)

    assert settings.managed_tools_path.is_file()
    assert ManagedToolRegistry(settings.managed_tools_path, tools).list() == [definition]

    async with Client(server) as client:
        listed = await client.list_tools()
        managed = next(tool for tool in listed if tool.name == definition.name)
        assert managed.inputSchema == definition.input_schema

        result = await client.call_tool(definition.name, {"query": "restart payments worker"})
        assert result.is_error is False
        assert result.structured_content is not None
        payload = result.structured_content
        assert payload["result_count"] == 1
        assert payload["results"][0]["source_path"] == "payments-runbook.md"


def test_managed_tool_rejects_unsafe_names_and_arbitrary_schema() -> None:
    payload = _definition().model_dump(mode="json")
    payload["name"] = "../../shell"
    with pytest.raises(ValueError, match="Tool name"):
        ManagedToolDefinition.model_validate(payload)

    payload = _definition().model_dump(mode="json")
    payload["input_schema"]["properties"]["command"] = {"type": "string"}
    with pytest.raises(ValueError, match="Unsupported schema properties"):
        ManagedToolDefinition.model_validate(payload)
