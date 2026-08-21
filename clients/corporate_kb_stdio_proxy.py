#!/usr/bin/env python3
"""One-file stdio proxy that mirrors every tool from a remote MCP server."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy


def normalize_mcp_url(value: str, *, variable: str) -> str:
    """Accept a server root or exact MCP endpoint and return the endpoint URL."""
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{variable} must be an absolute http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{variable} must not contain credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path in {"", "/"}:
        return f"{normalized}/mcp"
    if not path.endswith("/mcp"):
        raise ValueError(f"{variable} must be a server root or end with /mcp")
    return normalized


@dataclass(frozen=True, slots=True)
class RemoteMcpConfig:
    """Connection settings for the shared remote MCP server."""

    url: str
    token: str = ""
    timeout_seconds: float = 120.0
    ca_file: str | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("CORPORATE_KB_MCP_TIMEOUT must be greater than zero")

    def client(self) -> Client[StreamableHttpTransport]:
        transport = StreamableHttpTransport(
            self.url,
            auth=self.token or None,
            verify=self.ca_file,
        )
        return Client(
            transport,
            name="corporate-kb-stdio-upstream",
            timeout=self.timeout_seconds,
        )


def create_stdio_server(target: Any) -> FastMCP:
    """Mirror the complete upstream MCP surface through local stdio."""
    return create_proxy(
        target,
        name="corporate-knowledge-stdio-proxy",
        instructions=(
            "This is a transparent proxy to the shared corporate knowledge MCP server. "
            "Tool names, schemas, annotations, and calls come from the upstream server."
        ),
        provider_error_strategy="raise",
    )


def config_from_environment() -> RemoteMcpConfig:
    """Load preferred MCP variables while preserving the old API variable names."""
    direct_url = os.environ.get("CORPORATE_KB_MCP_URL", "").strip()
    legacy_url = os.environ.get("CORPORATE_KB_API_URL", "").strip()
    raw_url = direct_url or legacy_url
    variable = "CORPORATE_KB_MCP_URL" if direct_url else "CORPORATE_KB_API_URL"
    if not raw_url:
        raise ValueError(
            "set CORPORATE_KB_MCP_URL to the shared server endpoint, for example "
            "http://10.20.30.40:8000/mcp"
        )
    token = os.environ.get("CORPORATE_KB_MCP_TOKEN")
    if token is None:
        token = os.environ.get("CORPORATE_KB_API_TOKEN", "")
    ca_file = os.environ.get("CORPORATE_KB_MCP_CA_FILE")
    if ca_file is None:
        ca_file = os.environ.get("CORPORATE_KB_API_CA_FILE") or None
    raw_timeout = os.environ.get("CORPORATE_KB_MCP_TIMEOUT")
    if raw_timeout is None:
        raw_timeout = os.environ.get("CORPORATE_KB_API_TIMEOUT", "120")
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        raise ValueError("CORPORATE_KB_MCP_TIMEOUT must be a number") from None
    return RemoteMcpConfig(
        url=normalize_mcp_url(raw_url, variable=variable),
        token=token,
        timeout_seconds=timeout_seconds,
        ca_file=ca_file,
    )


def main() -> None:
    """Start the local stdio endpoint; stdout stays reserved for MCP frames."""
    try:
        config = config_from_environment()
        server = create_stdio_server(config.client())
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    try:
        server.run(transport="stdio", show_banner=False)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
