"""Lazy, request-scoped LangChain tools for the portal's published data sources."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any, Literal

import httpx
from jsonschema import Draft202012Validator, ValidationError
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import ClientSession
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field
from referencing import Registry

from corporate_kb.catalog import RagCatalog
from corporate_kb.mcp.servers import McpServerRegistry
from corporate_kb.models import Document, SearchFilters

Authorize = Callable[[], Awaitable[bool]]
ConnectionKind = Literal["all", "workspaces", "mcp"]
Query = Annotated[str, Field(min_length=1, max_length=4000)]
Limit = Annotated[int, Field(ge=1, le=20)]
_SOURCE_FIELDS = {"document_id", "title", "source_path", "source_url", "source_version",
                  "source_updated_at", "page_id", "revision_id"}
_MCP_TIMEOUT = 30
DOCUMENT_NOT_FOUND = ("The requested document is not available in this index. "
                      "Use a document_id from search_index, not a reference inside a document.")


async def _require(authorize: Authorize) -> None:
    if not await authorize():
        raise PermissionError("Source access has been revoked.")


def _bounded(value: Any, limit: int = 24000) -> Any:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value if len(text) <= limit else {"truncated": True, "preview": text[:limit]}


@asynccontextmanager
async def _session(url: str, token: str = "") -> AsyncIterator[ClientSession]:
    if token and not url.lower().startswith("https://"):
        raise ValueError("MCP bearer credentials require HTTPS.")
    async with (
        httpx.AsyncClient(timeout=_MCP_TIMEOUT, follow_redirects=False,
                          **({"headers": {"Authorization": f"Bearer {token}"}} if token else {})
                          ) as client,
        streamable_http_client(url, http_client=client) as (reader, writer, _),
        ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=_MCP_TIMEOUT)) as mcp,
    ):
        await mcp.initialize()
        yield mcp


def _self_contained_schema(value: Any) -> bool:
    # The MCP SDK validates output schemas internally. Only allow local references
    # so a schema supplied by a tool cannot cause an HTTP request to another host.
    if isinstance(value, dict):
        return all(
            isinstance(item, str) and item.startswith("#")
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} else _self_contained_schema(item)
            for key, item in value.items()
        )
    return not isinstance(value, list) or all(_self_contained_schema(item) for item in value)


async def _discover(
    session: ClientSession, allowed: Collection[str] | None,
) -> dict[str, mcp_types.Tool]:
    found: dict[str, mcp_types.Tool] = {}
    cursors: set[str] = set()
    params = None
    while True:
        page = await session.list_tools(params=params)
        for spec in page.tools:
            hints = spec.annotations
            # The operator publishes each read tool by exact name. Some MCPs
            # omit annotations; an explicit conflicting declaration wins.
            writes = hints and (hints.readOnlyHint is False or hints.destructiveHint is True)
            if ((allowed is None or spec.name in allowed) and not writes
                    and _self_contained_schema(spec.inputSchema)
                    and _self_contained_schema(spec.outputSchema)):
                found[spec.name] = spec
        if not page.nextCursor or (allowed is not None and found.keys() == set(allowed)):
            return found
        if page.nextCursor in cursors or len(cursors) >= 100 or len(found) > 2000:
            raise ToolException("The MCP tool catalog could not be read within its limits.")
        cursors.add(page.nextCursor)
        params = mcp_types.PaginatedRequestParams(cursor=page.nextCursor)


async def discover_read_tools(url: str, token: str = "") -> list[str]:
    async with asyncio.timeout(_MCP_TIMEOUT), _session(url, token) as session:
        return sorted(await _discover(session, None))


def _remote_tools(
    registry: McpServerRegistry, published: Mapping[str, Sequence[str]], authorize: Authorize,
    credential: Callable[[str, str], str] | None,
) -> list[BaseTool]:
    allowed = {key: frozenset(names) for key, names in published.items() if names}

    async def connection(server_id: str) -> str:
        await _require(authorize)
        if server_id not in allowed:
            raise ToolException("This MCP server is not available.")
        try:
            return registry.get(server_id).url
        except KeyError:
            raise ToolException("This MCP server is not available.") from None

    async def request(
        server_id: str, tool_name: str | None = None, arguments: dict[str, Any] | None = None,
    ) -> Any:
        url = await connection(server_id)
        try:
            token = await asyncio.to_thread(credential, server_id, url) if credential else ""
            async with asyncio.timeout(_MCP_TIMEOUT), _session(url, token) as mcp:
                current = await _discover(mcp, allowed[server_id])
                if tool_name is None:
                    result: Any = [{"name": t.name, "description": (t.description or "")[:1200],
                                    "inputSchema": t.inputSchema} for t in current.values()]
                elif tool_name not in current:
                    result = {"error": "This read-only tool is no longer available."}
                else:
                    spec = current[tool_name]
                    try:
                        Draft202012Validator(spec.inputSchema, registry=Registry()).validate(
                            arguments or {},
                        )
                    except ValidationError:
                        return {"error": "Arguments do not match the current tool schema."}
                    if await connection(server_id) != url:
                        raise ToolException("The MCP connection has changed.")
                    reply = await mcp.call_tool(tool_name, arguments or {})
                    result = ({"error": "The MCP tool reported a failure."} if reply.isError else
                              reply.structuredContent if reply.structuredContent is not None else
                              [block.text for block in reply.content if block.type == "text"])
        except Exception:
            # Transport exceptions (including grouped exceptions from the MCP
            # session) may contain the private connection URL or response body.
            raise ToolException("Could not complete the MCP request.") from None
        if await connection(server_id) != url:
            raise ToolException("The MCP connection has changed.")
        return _bounded(result)

    async def list_environment_tools(server_id: str = "") -> Any:
        """List published MCP server IDs, or discover live read-only tool schemas for one ID."""
        if server_id:
            return await request(server_id)
        await _require(authorize)
        return [{"id": server.id, "name": server.name} for server in registry.list()
                if server.id in allowed]

    async def call_environment_tool(
        server_id: str, tool_name: str, arguments: dict[str, Any],
    ) -> Any:
        """Call one published read-only MCP tool with arguments matching its current schema."""
        if tool_name not in allowed.get(server_id, ()):
            raise ToolException("This read-only tool is not available.")
        return await request(server_id, tool_name, arguments)

    return [StructuredTool.from_function(coroutine=fn, handle_tool_error=True)
            for fn in (list_environment_tools, call_environment_tool)]


async def connection_inventory(
    kind: ConnectionKind = "all", *, authorize: Authorize, workspace: tuple[str, str] | None,
    mcp_servers: McpServerRegistry | None, mcp_read_tools: Mapping[str, Sequence[str]] | None,
) -> dict[str, Any]:
    """Read the locally published connection metadata visible to the current principal."""
    await _require(authorize)
    result: dict[str, Any] = {}
    if kind in {"all", "workspaces"}:
        result["workspaces"] = ([{"name": workspace[0], "image": workspace[1]}]
                                if workspace else [])
    if kind in {"all", "mcp"}:
        servers = []
        if mcp_servers and mcp_read_tools:
            for server in await asyncio.to_thread(mcp_servers.list):
                if names := mcp_read_tools.get(server.id):
                    servers.append({"id": server.id, "name": server.name,
                                    "tools": sorted(set(names))})
        result["mcp"] = servers
    await _require(authorize)
    return result


def build_tools(
    catalog: RagCatalog, *, authorize: Authorize, authorize_graph: Authorize | None = None,
    index_ids: Collection[str] | None = None, mcp_servers: McpServerRegistry | None = None,
    mcp_read_tools: Mapping[str, Sequence[str]] | None = None,
    mcp_credential: Callable[[str, str], str] | None = None,
    workspace: tuple[str, str] | None = None,
) -> list[BaseTool]:
    """Bind the actual caller's grants; constructing the catalog never opens a source."""
    scope = frozenset(index_ids) if index_ids is not None else None
    default_index = next(iter(scope)) if scope and len(scope) == 1 else ""

    async def local[T](index_id: str, action: Callable[[], T]) -> T:
        await _require(authorize)
        if scope is not None and index_id not in scope:
            raise ToolException("This index is not available. Call list_indexes and retry "
                                "using a returned ID and the original search terms.")
        try:
            value = await asyncio.to_thread(action)
        except KeyError:
            raise ToolException("The requested index or document is not available. "
                                "Use IDs returned by list_indexes and search_index.") from None
        await _require(authorize)
        return value

    async def list_indexes() -> list[dict[str, str]]:
        """List the available knowledge index IDs, names, descriptions and revisions."""
        await _require(authorize)
        rows = await asyncio.to_thread(catalog.index_directory)
        await _require(authorize)
        return [row for row in rows if scope is None or row["id"] in scope]

    async def list_connections(kind: ConnectionKind = "all") -> dict[str, Any]:
        """List this chat's published Docker sandboxes, MCP servers and approved MCP tool names."""
        return await connection_inventory(kind, authorize=authorize, workspace=workspace,
                                          mcp_servers=mcp_servers, mcp_read_tools=mcp_read_tools)

    async def search_index(query: Query, index_id: str = default_index,
                           limit: Limit = 5) -> dict[str, Any]:
        """Search current text in one index. Use a document ID from the results to read further."""
        hits = await local(index_id, lambda: catalog.tools_for(index_id).search_candidates(
            query, limit, filters=SearchFilters(status="current"), full_text=True,
        ))
        return {"index_id": index_id, "results": [
            {**hit.model_dump(mode="json", include=_SOURCE_FIELDS | {"heading_path", "chunk_id"}),
             "excerpt": hit.text[:1000], "excerpt_truncated": len(hit.text) > 1000}
            for hit in hits[:limit]
        ]}

    async def read_document(
        document_id: str, index_id: str = default_index, offset: Annotated[int, Field(ge=0)] = 0,
        length: Annotated[int, Field(ge=1, le=16000)] = 6000,
    ) -> dict[str, Any]:
        """Read a current document by ID. Pass next_offset to read the next section in full."""
        def current_document() -> Document:
            kb = catalog.tools_for(index_id)
            try:
                return kb.read_current_document(document_id=document_id)
            except KeyError:
                raise ToolException(DOCUMENT_NOT_FOUND) from None

        doc = await local(index_id, current_document)
        end = min(offset + length, len(doc.content))
        return {**doc.model_dump(mode="json", include=_SOURCE_FIELDS), "index_id": index_id,
                "content": doc.content[offset:end], "offset": offset,
                "next_offset": end if end < len(doc.content) else None}

    tools: list[BaseTool] = [StructuredTool.from_function(coroutine=fn, handle_tool_error=True)
                             for fn in (list_indexes, search_index, read_document)]
    tools[-1].metadata = {"portal_rag_document": True}
    if scope:
        names = ", ".join(sorted(scope))
        hint = (f" The available index IDs are: {names}." if len(names) <= 1500 else
                " Use list_indexes to get exact index IDs.")
        for index_tool in tools[1:]:
            index_tool.description += hint
    tools.append(StructuredTool.from_function(coroutine=list_connections))
    if authorize_graph is not None:
        graph_grant = authorize_graph

        async def search_service_graph(query: str = "", limit: Limit = 10) -> Any:
            """Read published graph counts or search services and their code evidence by name."""
            await _require(authorize)
            await _require(graph_grant)
            result = (await asyncio.to_thread(catalog.graph_search, query, limit=limit)
                      if query.strip() else await asyncio.to_thread(catalog.graph_overview))
            await _require(authorize)
            await _require(graph_grant)
            return _bounded(result)

        tools.append(StructuredTool.from_function(coroutine=search_service_graph))
    if mcp_servers is not None and mcp_read_tools:
        tools.extend(_remote_tools(mcp_servers, mcp_read_tools, authorize, mcp_credential))
    return tools
