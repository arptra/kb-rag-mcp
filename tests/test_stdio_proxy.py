from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastmcp import Client


def _load_proxy_module() -> ModuleType:
    path = Path(__file__).parents[1] / "clients" / "corporate_kb_stdio_proxy.py"
    spec = importlib.util.spec_from_file_location("corporate_kb_stdio_proxy", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StubApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object | None]]] = []

    def get_json(self, path: str, params: dict[str, object | None]) -> dict[str, Any]:
        self.calls.append((path, params))
        return {"path": path, "params": params}


def test_remote_api_rejects_mcp_endpoint_as_base_url() -> None:
    module = _load_proxy_module()

    with pytest.raises(ValueError, match="must not include /mcp"):
        module.RemoteKnowledgeApi("http://kb.example/mcp", "token")


@pytest.mark.asyncio
async def test_stdio_proxy_exposes_remote_json_api_as_five_tools() -> None:
    module = _load_proxy_module()
    api = StubApi()
    server = module.create_stdio_server(api)

    async with Client(server) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed} == {
            "kb_search",
            "kb_get_document",
            "kb_get_chunk",
            "kb_list_documents",
            "kb_stats",
        }

        search = await client.call_tool("kb_search", {"query": "daily limits", "top_k": 3})
        assert search.is_error is False
        assert search.data["path"] == "/api/v1/search"
        assert search.data["params"]["query"] == "daily limits"
        assert search.data["params"]["top_k"] == 3

        document = await client.call_tool("kb_get_document", {"document_id": "doc-1"})
        assert document.data["path"] == "/api/v1/document"
        assert document.data["params"]["document_id"] == "doc-1"

        chunk = await client.call_tool("kb_get_chunk", {"chunk_id": "chunk-1"})
        assert chunk.data["path"] == "/api/v1/chunk"
        assert chunk.data["params"]["chunk_id"] == "chunk-1"

        documents = await client.call_tool("kb_list_documents", {"limit": 10})
        assert documents.data["path"] == "/api/v1/documents"
        assert documents.data["params"]["limit"] == 10

        stats = await client.call_tool("kb_stats", {})
        assert stats.data["path"] == "/api/v1/stats"
