from __future__ import annotations

import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tool_overrides import (
    BuiltinToolOverride,
    BuiltinToolOverrideRegistry,
)
from corporate_kb.service import KnowledgeService


@pytest.mark.asyncio
async def test_builtin_tool_description_override_persists_and_is_discovered(
    settings_factory,
) -> None:
    settings = settings_factory()
    service = KnowledgeService(
        settings,
        provider=HashEmbeddingProvider(settings.embedding_dimension),
    )
    registry = BuiltinToolOverrideRegistry(settings.builtin_tool_overrides_path)
    definition = registry.upsert(
        BuiltinToolOverride(
            name="kb_stats",
            description="Return a compact health snapshot for RAG diagnostics and support.",
        )
    )
    reloaded = BuiltinToolOverrideRegistry(settings.builtin_tool_overrides_path)
    assert reloaded.list() == [definition]

    server = create_mcp_server(service, builtin_tool_overrides=reloaded)
    async with Client(server) as client:
        listed = await client.list_tools()
        stats = next(item for item in listed if item.name == "kb_stats")
        assert stats.description == definition.description


def test_builtin_tool_override_rejects_unknown_tool() -> None:
    with pytest.raises(ValueError, match="Unknown built-in MCP tool"):
        BuiltinToolOverride(
            name="kb_arbitrary_code",
            description="This must never become an executable tool override.",
        )
