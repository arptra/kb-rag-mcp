"""Safe startup URLs for the normal launcher, using the server's Settings precedence."""

from __future__ import annotations

import sys

from corporate_kb.config import Settings


def startup_summary(settings: Settings) -> str:
    """Describe bind/URLs without exporting settings or printing any credentials."""
    scheme = "https" if settings.mcp_tls_enabled else "http"
    host = settings.mcp_http_host
    # Wildcard binds are not client destinations. Give a local URL and make the
    # remote-hostname requirement explicit, including certificate name matching.
    local_host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    authority = f"[{local_host}]" if ":" in local_host else local_host
    origin = f"{scheme}://{authority}:{settings.mcp_http_port}"
    bind = f"[{host}]" if ":" in host else host
    lines = [
        f"Bind:  {bind}:{settings.mcp_http_port}",
        f"Admin: {origin}/admin",
        f"MCP:   {origin}{settings.mcp_http_path}",
    ]
    if host in {"127.0.0.1", "::1", "localhost"}:
        lines.append(
            "Access: loopback only. Set KB_MCP_HTTP_HOST explicitly for remote connections."
        )
    elif host in {"0.0.0.0", "::"}:
        lines.append(
            "Access: all interfaces. URLs above are local; remote clients must use "
            "the server hostname (matching the TLS certificate when HTTPS is enabled)."
        )
    return "\n".join(lines)


def main() -> None:
    try:
        settings = Settings()
    except ValueError:
        # ValidationError may include raw environment inputs; never echo it here.
        print("Cannot display startup URLs: invalid server configuration.", file=sys.stderr)
        raise SystemExit(2) from None
    print(startup_summary(settings))


if __name__ == "__main__":
    main()
