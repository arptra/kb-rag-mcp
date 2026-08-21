from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastmcp import Client, FastMCP


def _load_proxy_module() -> ModuleType:
    path = Path(__file__).parents[1] / "clients" / "corporate_kb_stdio_proxy.py"
    spec = importlib.util.spec_from_file_location("corporate_kb_stdio_proxy", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_normalize_mcp_url_accepts_root_and_exact_endpoint() -> None:
    module = _load_proxy_module()

    assert (
        module.normalize_mcp_url("http://kb.example", variable="CORPORATE_KB_MCP_URL")
        == "http://kb.example/mcp"
    )
    assert (
        module.normalize_mcp_url(
            "https://kb.example/platform/mcp/",
            variable="CORPORATE_KB_MCP_URL",
        )
        == "https://kb.example/platform/mcp"
    )


@pytest.mark.parametrize(
    "url",
    [
        "kb.example/mcp",
        "ftp://kb.example/mcp",
        "https://user:secret@kb.example/mcp",
        "https://kb.example/api/v1",
    ],
)
def test_normalize_mcp_url_rejects_invalid_endpoint(url: str) -> None:
    module = _load_proxy_module()

    with pytest.raises(ValueError, match="CORPORATE_KB_MCP_URL"):
        module.normalize_mcp_url(url, variable="CORPORATE_KB_MCP_URL")


def test_environment_keeps_legacy_api_url_compatible(monkeypatch) -> None:
    module = _load_proxy_module()
    monkeypatch.delenv("CORPORATE_KB_MCP_URL", raising=False)
    monkeypatch.setenv("CORPORATE_KB_API_URL", "http://legacy-kb.example:8000")
    monkeypatch.setenv("CORPORATE_KB_API_TIMEOUT", "45")

    config = module.config_from_environment()

    assert config.url == "http://legacy-kb.example:8000/mcp"
    assert config.timeout_seconds == 45


@pytest.mark.asyncio
async def test_stdio_proxy_mirrors_future_tools_without_hardcoding() -> None:
    module = _load_proxy_module()
    upstream = FastMCP("future-shared-server")

    @upstream.tool(
        name="kb_future_tool",
        description="A tool introduced after the local proxy was distributed.",
        annotations={"readOnlyHint": False, "openWorldHint": True},
    )
    def future_tool(value: str, count: int = 1) -> dict[str, object]:
        return {"value": value, "count": count, "source": "upstream"}

    proxy = module.create_stdio_server(upstream)
    async with Client(proxy) as client:
        listed = await client.list_tools()
        tools = {tool.name: tool for tool in listed}

        assert set(tools) == {"kb_future_tool"}
        assert tools["kb_future_tool"].description == (
            "A tool introduced after the local proxy was distributed."
        )
        assert tools["kb_future_tool"].inputSchema["properties"]["count"]["default"] == 1
        assert tools["kb_future_tool"].annotations is not None
        assert tools["kb_future_tool"].annotations.readOnlyHint is False

        result = await client.call_tool("kb_future_tool", {"value": "fresh", "count": 3})
        assert result.is_error is False
        assert result.data == {"value": "fresh", "count": 3, "source": "upstream"}


@pytest.mark.asyncio
async def test_stdio_proxy_saves_client_ssot_to_temp_before_upload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_proxy_module()
    upstream = FastMCP("ssot-upstream")
    calls: list[dict[str, object]] = []

    @upstream.tool(name="kb_generate_system_ssot")
    def generate_system_ssot(
        action: str,
        job_id: str,
        service_id: str,
        content: str,
        finalize: bool,
    ) -> dict[str, object]:
        payload = {
            "action": action,
            "job_id": job_id,
            "service_id": service_id,
            "content": content,
            "finalize": finalize,
        }
        calls.append(payload)
        return {"status": "indexing", "service_id": service_id}

    monkeypatch.setenv("CORPORATE_KB_SSOT_TEMP_DIR", str(tmp_path))
    proxy = module.create_stdio_server(
        upstream,
        upload_client_factory=lambda: Client(upstream),
    )
    content = (
        "# Local lifecycle SSOT\n\n"
        "The client model generated this source-backed document and keeps a local temp copy "
        "before uploading it to the selected remote index.\n"
    )
    async with Client(proxy) as client:
        listed = {tool.name for tool in await client.list_tools()}
        assert "kb_save_and_upload_ssot" in listed
        result = await client.call_tool(
            "kb_save_and_upload_ssot",
            {
                "session_id": "session-123",
                "service_id": "lifecycle-service",
                "content": content,
                "finalize": True,
            },
        )

    assert result.is_error is False
    local_path = tmp_path / "session-123" / "lifecycle-service.md"
    assert local_path.read_text(encoding="utf-8") == content
    assert calls == [
        {
            "action": "submit",
            "job_id": "session-123",
            "service_id": "lifecycle-service",
            "content": content,
            "finalize": True,
        }
    ]
    assert result.data["local_path"] == str(local_path)
