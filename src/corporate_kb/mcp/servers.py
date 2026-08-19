"""Persistent registry and discovery for HTTP MCP servers shown in the admin UI."""

from __future__ import annotations

import builtins
import json
import os
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, field_validator

BUILTIN_TOOLS: tuple[dict[str, str], ...] = (
    {"name": "ssot_context", "description": "Build compact cross-service SSOT context."},
    {"name": "kb_search", "description": "Search indexed corporate knowledge."},
    {"name": "kb_get_document", "description": "Read a bounded document extract."},
    {"name": "kb_get_chunk", "description": "Read context around one search chunk."},
    {"name": "kb_run_context_benchmark", "description": "Run the context quality benchmark."},
    {"name": "kb_list_documents", "description": "List indexed document metadata."},
    {"name": "kb_stats", "description": "Read index and runtime statistics."},
)
_SERVER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class McpServerDefinition(BaseModel):
    """User-managed Streamable HTTP MCP endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"mcp-{uuid4().hex[:12]}")
    name: str = Field(min_length=2, max_length=120)
    url: str = Field(min_length=8, max_length=2048)
    transport: Literal["streamable-http"] = "streamable-http"
    status: Literal["unchecked", "online", "offline"] = "unchecked"
    tools: list[dict[str, str]] = Field(default_factory=list, max_length=500)
    checked_at: str | None = None
    error: str | None = Field(default=None, max_length=1000)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _SERVER_ID.fullmatch(value):
            raise ValueError("MCP server id must be a lowercase slug")
        if value == "local":
            raise ValueError("MCP server id 'local' is reserved")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("MCP server name must contain at least 2 characters")
        return value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("MCP server URL must be an absolute http:// or https:// URL")
        if parsed.username or parsed.password:
            raise ValueError("Put credentials in a client proxy; URL credentials are not stored")
        return value

    def payload(self) -> dict[str, Any]:
        result = self.model_dump(mode="json")
        result.update(
            {
                "kind": "external",
                "tool_count": len(self.tools),
                "deletable": True,
            }
        )
        return result


class McpServerRegistry:
    """Persist external endpoints and cache their last discovery result."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._lock = Lock()
        self._definitions = self._load()

    def list(self) -> list[McpServerDefinition]:
        with self._lock:
            return [
                self._definitions[key].model_copy(deep=True)
                for key in sorted(self._definitions)
            ]

    def add(self, definition: McpServerDefinition) -> McpServerDefinition:
        with self._lock:
            if len(self._definitions) >= 50:
                raise ValueError("MCP server limit reached (50)")
            if definition.id in self._definitions:
                raise ValueError(f"MCP server id already exists: {definition.id}")
            if any(item.url == definition.url for item in self._definitions.values()):
                raise ValueError(f"MCP server URL already exists: {definition.url}")
            self._definitions[definition.id] = definition
            self._save_locked()
        return definition.model_copy(deep=True)

    def get(self, server_id: str) -> McpServerDefinition:
        with self._lock:
            definition = self._definitions.get(server_id)
            if definition is None:
                raise KeyError(f"Unknown MCP server: {server_id}")
            return definition.model_copy(deep=True)

    def update_probe(
        self,
        server_id: str,
        *,
        status: Literal["online", "offline"],
        tools: builtins.list[dict[str, str]],
        error: str | None,
    ) -> McpServerDefinition:
        with self._lock:
            current = self._definitions.get(server_id)
            if current is None:
                raise KeyError(f"Unknown MCP server: {server_id}")
            updated = current.model_copy(
                update={"status": status, "tools": tools, "checked_at": _now(), "error": error}
            )
            self._definitions[server_id] = updated
            self._save_locked()
            return updated.model_copy(deep=True)

    def delete(self, server_id: str) -> None:
        with self._lock:
            if server_id not in self._definitions:
                raise KeyError(f"Unknown MCP server: {server_id}")
            del self._definitions[server_id]
            self._save_locked()

    async def probe(self, server_id: str) -> McpServerDefinition:
        definition = self.get(server_id)
        try:
            timeout = httpx.Timeout(6.0)
            async with (
                httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
                streamable_http_client(
                    definition.url,
                    http_client=client,
                ) as (read_stream, write_stream, _get_session_id),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=6),
                ) as session,
            ):
                await session.initialize()
                discovered = await session.list_tools()
            tools = [
                {"name": tool.name, "description": tool.description or ""}
                for tool in discovered.tools
            ]
            return self.update_probe(server_id, status="online", tools=tools, error=None)
        except Exception as exc:
            message = str(exc).strip() or type(exc).__name__
            return self.update_probe(
                server_id,
                status="offline",
                tools=[],
                error=message[:1000],
            )

    def _load(self) -> dict[str, McpServerDefinition]:
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("mcp_servers.json must contain an array")
            definitions = [McpServerDefinition.model_validate(item) for item in raw]
            return {item.id: item for item in definitions}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            self._definitions[key].model_dump(mode="json")
            for key in sorted(self._definitions)
        ]
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            dir=self._path.parent,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def mcp_servers_payload(
    registry: McpServerRegistry,
    *,
    local_url: str,
    managed_tools: list[dict[str, Any]],
) -> dict[str, Any]:
    local_tools = [dict(item, kind="built-in") for item in BUILTIN_TOOLS]
    local_tools.extend(
        {
            "name": str(item["name"]),
            "description": str(item.get("description", "")),
            "kind": "managed",
        }
        for item in managed_tools
    )
    servers = [
        {
            "id": "local",
            "name": "corporate-knowledge",
            "url": local_url,
            "transport": "streamable-http",
            "kind": "local",
            "status": "online",
            "tools": local_tools,
            "tool_count": len(local_tools),
            "checked_at": _now(),
            "error": None,
            "deletable": False,
        },
        *(definition.payload() for definition in registry.list()),
    ]
    return {
        "server_count": len(servers),
        "online_count": sum(item["status"] == "online" for item in servers),
        "servers": servers,
    }
