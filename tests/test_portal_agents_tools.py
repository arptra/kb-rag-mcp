from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastmcp import FastMCP

from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.mcp.servers import McpServerDefinition, McpServerRegistry
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeService
from corporate_kb.usage import UsageTracker
from portal_agents.tools import build_tools


async def permitted() -> bool:
    return True


def real_catalog(settings_factory: Callable[..., Settings]) -> RagCatalog:
    settings = settings_factory()
    settings.knowledge_dir.mkdir()
    (settings.knowledge_dir / "orders.md").write_text(
        "# Order updates\n\nThe final delivery confirmation occurs after settlement."
    )
    provider = HashEmbeddingProvider(settings.embedding_dimension)
    service = KnowledgeService(settings, provider=provider)
    service.build_index(force=True)
    usage = UsageTracker()
    return RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)


async def test_scoped_catalog_search_and_document_paging(settings_factory) -> None:
    catalog = real_catalog(settings_factory)
    public = catalog.index_directory()[0]["id"]
    private = catalog.create_index(name="Archive").id
    tools = {t.name: t for t in build_tools(catalog, authorize=permitted, index_ids=[public])}
    assert [row["id"] for row in await tools["list_indexes"].ainvoke({})] == [public]
    args = {"index_id": public, "query": "delivery confirmation"}
    entry = (await tools["search_index"].ainvoke(args))["results"][0]
    assert entry["document_id"] and entry["source_path"].endswith("orders.md")
    doc = {"index_id": public, "document_id": entry["document_id"]}
    first_page = await tools["read_document"].ainvoke({**doc, "length": 8})
    last_page = await tools["read_document"].ainvoke({**doc, "offset": first_page["next_offset"]})
    assert first_page["next_offset"] == 8 and last_page["next_offset"] is None
    assert (first_page["content"] + last_page["content"]).endswith("after settlement.")
    assert "not available" in await tools["search_index"].ainvoke({**args, "index_id": private})
    assert "not available" in await tools["read_document"].ainvoke({**doc, "index_id": private})
    assert "not available" in await tools["read_document"].ainvoke({**doc, "document_id": "absent"})


async def test_revoked_grant_withholds_read_result_and_graph_has_separate_grant() -> None:
    catalog = Mock()
    catalog.index_directory.return_value = [{"id": "a", "name": "private"}]
    check = AsyncMock(side_effect=[True, False])
    tools = {t.name: t for t in build_tools(catalog, authorize=check,
                                             authorize_graph=AsyncMock(return_value=False))}
    check.assert_not_awaited()
    catalog.index_directory.assert_not_called()
    with pytest.raises(PermissionError):
        await tools["list_indexes"].ainvoke({})
    check.side_effect = None
    check.return_value = True
    with pytest.raises(PermissionError):
        await tools["search_service_graph"].ainvoke({})
    catalog.graph_overview.assert_not_called()


async def test_published_environment_calls_live_mcp_and_rechecks_capabilities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = FastMCP("test environment")
    calls: list[str] = []

    @mcp.tool(annotations={"readOnlyHint": True})
    def release_version(service: str) -> dict[str, str]:
        """Read the version currently deployed."""
        calls.append(service)
        return {"version": "r27", "service": service}

    @mcp.tool()
    def release_notes() -> str:
        """This operator-approved read tool is served by a legacy MCP without hints."""
        return "Order reporting is available."

    @mcp.tool(annotations={"readOnlyHint": False})
    def rollback() -> str:
        raise AssertionError("Write operations must never be exposed")

    endpoint = mcp.http_app(path="/mcp", json_response=True, stateless_http=True)
    original_client = httpx.AsyncClient

    def client(**options):
        assert options["follow_redirects"] is False and "headers" not in options
        return original_client(**options, transport=httpx.ASGITransport(app=endpoint))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    registry = McpServerRegistry(tmp_path / "mcp_servers.json")
    registry.add(McpServerDefinition(id="prod", name="Releases", url="http://testserver/mcp"))
    registry.add(McpServerDefinition(id="other", name="Hidden", url="http://hidden.test/mcp"))
    tools = {t.name: t for t in build_tools(
        Mock(), authorize=permitted, mcp_servers=registry,
        mcp_read_tools={"prod": ["release_version", "release_notes", "rollback"]},
    )}
    discover = tools["list_environment_tools"]
    invoke = tools["call_environment_tool"]
    async with endpoint.router.lifespan_context(endpoint):
        assert await discover.ainvoke({}) == [{"id": "prod", "name": "Releases"}]
        specs = await discover.ainvoke({"server_id": "prod"})
        by_name = {spec["name"]: spec for spec in specs}
        assert set(by_name) == {"release_notes", "release_version"}
        assert "service" in by_name["release_version"]["inputSchema"]["properties"]
        args = {"server_id": "prod", "tool_name": "release_version",
                "arguments": {"service": "pay"}}
        assert await invoke.ainvoke(args) == {"service": "pay", "version": "r27"}
        assert "error" in await invoke.ainvoke({**args, "arguments": {}})
        assert "error" in await invoke.ainvoke({**args, "tool_name": "rollback"})
        assert "not available" in await invoke.ainvoke({**args, "server_id": "other"})
        registry.delete("prod")
        assert "not available" in await invoke.ainvoke(args)
        assert calls == ["pay"]
