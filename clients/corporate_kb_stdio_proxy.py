#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "fastmcp==3.4.4",
# ]
# ///
"""One-file stdio MCP proxy for the remote corporate knowledge JSON API."""

from __future__ import annotations

import json
import os
import ssl
import sys
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

SEARCH_DESCRIPTION = """Search remote corporate knowledge before architectural analysis or changes
spanning multiple services. Cite source_path or source_url from the returned results. Call
kb_get_document when the complete document is needed."""


class JsonApi(Protocol):
    """Minimal interface used by the MCP tools and test doubles."""

    def get_json(self, path: str, params: Mapping[str, object | None]) -> dict[str, Any]: ...


class RemoteKnowledgeApi:
    """Synchronous standard-library client for the read-only remote RAG API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        ca_file: str | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("CORPORATE_KB_API_URL must be an http:// or https:// server URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("CORPORATE_KB_API_URL must not include /mcp, a query, or a fragment")
        if not token:
            raise ValueError("CORPORATE_KB_API_TOKEN must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("CORPORATE_KB_API_TIMEOUT must be greater than zero")

        self._base_url = normalized
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._ssl_context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    def get_json(self, path: str, params: Mapping[str, object | None]) -> dict[str, Any]:
        query = urlencode(
            {key: str(value) for key, value in params.items() if value is not None},
            doseq=False,
        )
        url = f"{self._base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "corporate-kb-stdio-proxy/1.0",
            },
        )
        try:
            with urlopen(
                request,
                timeout=self._timeout_seconds,
                context=self._ssl_context,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise ToolError(f"Remote RAG returned HTTP {exc.code}: {detail}") from None
        except URLError as exc:
            raise ToolError(f"Remote RAG connection failed: {exc.reason}") from None
        except (TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ToolError(f"Remote RAG returned an invalid response: {exc}") from None

        if not isinstance(payload, dict):
            raise ToolError("Remote RAG returned JSON that is not an object")
        return payload


def create_stdio_server(api: JsonApi) -> FastMCP:
    """Expose the remote JSON API as a local read-only stdio MCP server."""
    server = FastMCP(
        "corporate-knowledge-stdio-proxy",
        instructions=(
            "Search corporate knowledge before cross-service or architectural work and cite "
            "source_path or source_url from the results."
        ),
    )

    @server.tool(
        name="kb_search",
        description=SEARCH_DESCRIPTION,
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_search(
        query: str,
        top_k: int = 5,
        min_score: float | None = None,
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = "current",
        authority: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        return api.get_json(
            "/api/v1/search",
            {
                "query": query,
                "top_k": top_k,
                "min_score": min_score,
                "service": service,
                "domain": domain,
                "document_type": document_type,
                "status": status,
                "authority": authority,
                "source_type": source_type,
            },
        )

    @server.tool(
        name="kb_get_document",
        description="Return one complete document selected by document_id from kb_search.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_get_document(document_id: str) -> dict[str, Any]:
        return api.get_json("/api/v1/document", {"document_id": document_id})

    @server.tool(
        name="kb_list_documents",
        description="List filtered remote document metadata without document bodies.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_list_documents(
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return api.get_json(
            "/api/v1/documents",
            {
                "service": service,
                "domain": domain,
                "document_type": document_type,
                "status": status,
                "limit": limit,
            },
        )

    @server.tool(
        name="kb_stats",
        description="Return remote knowledge-index counts and identity.",
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    def kb_stats() -> dict[str, Any]:
        return api.get_json("/api/v1/stats", {})

    return server


def main() -> None:
    """Load environment settings and reserve stdout for MCP protocol frames."""
    base_url = os.environ.get("CORPORATE_KB_API_URL", "")
    token = os.environ.get("CORPORATE_KB_API_TOKEN", "")
    ca_file = os.environ.get("CORPORATE_KB_API_CA_FILE") or None
    try:
        timeout_seconds = float(os.environ.get("CORPORATE_KB_API_TIMEOUT", "30"))
        api = RemoteKnowledgeApi(
            base_url,
            token,
            timeout_seconds=timeout_seconds,
            ca_file=ca_file,
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        create_stdio_server(api).run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
