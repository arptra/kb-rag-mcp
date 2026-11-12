from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.mcp.managed_tools import ManagedToolDefinition, ManagedToolRegistry
from corporate_kb.mcp.server import create_mcp_server
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService


def definition(name: str) -> dict:
    return {
        "name": name,
        "description": "Existing search for current payment policy documents.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "defaults": {"top_k": 2, "status": "current"},
    }


def indexed(settings_factory, names: list[str]):
    settings = settings_factory(
        a2a_enabled=False, confluence_enabled=False, skills_registry_enabled=False
    )
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "limits.md").write_text(
        "# Limits\n\nThe daily payment limit is 500.", encoding="utf-8"
    )
    service = KnowledgeService(settings, provider=HashEmbeddingProvider(64))
    service.build_index()
    settings.managed_tools_path.write_text(
        json.dumps([definition(name) for name in names]), encoding="utf-8"
    )
    return settings, service


@pytest.mark.asyncio
@pytest.mark.parametrize("occupied_aliases", [[], ["kb_search_versions_builtin"]])
async def test_upgrade_preserves_legacy_search_name_and_exposes_history_alias(
    settings_factory, occupied_aliases, caplog
):
    names = ["kb_search_versions", "kb_payment_policy", *occupied_aliases]
    settings, service = indexed(settings_factory, names)
    tools = KnowledgeTools(service)
    registry = ManagedToolRegistry(settings.managed_tools_path, tools)
    assert {item.name for item in registry.list()} == set(names)
    server = create_mcp_server(service, knowledge_tools=tools, managed_tools=registry)
    alias = "kb_search_versions_builtin_2" if occupied_aliases else "kb_search_versions_builtin"
    assert alias in server.instructions
    assert f"built-in historical search is available as {alias}" in caplog.text
    async with Client(server) as client:
        listed = {tool.name: tool for tool in await client.list_tools()}
        for name in names:
            assert listed[name].inputSchema == definition(name)["input_schema"]
            result = await client.call_tool(name, {"query": "daily payment limit"})
            assert result.structured_content["result_count"] == 1
        result = await client.call_tool(
            alias, {"query": "daily payment limit", "version_scope": "history"}
        )
        assert result.structured_content["version_scope"] == "history"
        assert result.structured_content["result_count"] == 1

    with pytest.raises(ValueError, match="reserved"):
        registry.upsert(ManagedToolDefinition.model_validate(definition(alias)))
    # Saving another tool must preserve legacy names on the following restart too.
    registry.upsert(ManagedToolDefinition.model_validate(definition("kb_more_policy")))
    restored = ManagedToolRegistry(settings.managed_tools_path, tools)
    assert {item.name for item in restored.list()} == set(names) | {"kb_more_policy"}


@pytest.mark.asyncio
async def test_upgrade_allows_editing_existing_reserved_name_but_not_creating_it(
    settings_factory,
):
    settings, service = indexed(settings_factory, ["kb_search_versions", "kb_payment_policy"])
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        edited = definition("kb_search_versions")
        edited["description"] = "Updated existing search for current payment documentation."
        saved = await client.post("/admin/api/tools", json=edited)
        assert saved.status_code == 201, saved.text
        assert saved.json()["description"] == edited["description"]
        collision = await client.post(
            "/admin/api/tools", json=definition("kb_search_versions_builtin")
        )
        assert collision.status_code == 400, collision.text
        with pytest.raises(ValueError, match="reserved"):
            ManagedToolDefinition.model_validate(definition("kb_search_versions"))
        forbidden = await client.post("/admin/api/tools", json=definition("kb_search"))
        assert forbidden.status_code == 400, forbidden.text

    settings.managed_tools_path.write_text("[]", encoding="utf-8")
    fresh_app = create_http_app(service, settings)
    async with (
        fresh_app.router.lifespan_context(fresh_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=fresh_app), base_url="http://testserver"
        ) as client,
    ):
        created = await client.post("/admin/api/tools", json=definition("kb_search_versions"))
        assert created.status_code == 400, created.text
