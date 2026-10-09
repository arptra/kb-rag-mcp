"""Explicit client-certificate enrollment and safe local MCP settings update."""

from __future__ import annotations

import copy
import json
import os
import re
import ssl
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeGuard
from urllib.parse import urlsplit

import httpx
import typer

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="Register a personal certificate with RAG access.",
)
MAX_CONFIG_BYTES = 8 * 1024 * 1024
INCOMPATIBLE_ENTRY_FIELDS = {"url", "command", "args", "cwd", "env", "transport", "type"}
TOKEN_ENV = "KB_ACCESS_TOKEN"


class EnrollmentError(Exception):
    """An error with a safe message that never contains response bodies or credentials."""


@dataclass(frozen=True)
class ConfigSnapshot:
    path: Path
    raw: bytes | None
    identity: tuple[int, int, int, int, int] | None
    document: dict[str, Any]


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_regular(path: Path) -> tuple[bytes | None, tuple[int, int, int, int, int] | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise EnrollmentError("Config must be a regular file, not a symlink or directory.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(info):
            raise EnrollmentError("Config changed while it was being read; retry the command.")
        raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise EnrollmentError("Config is larger than the supported 8 MiB limit.")
        if _identity(os.fstat(stream.fileno())) != _identity(opened):
            raise EnrollmentError("Config changed while it was being read; retry the command.")
    return raw, _identity(opened)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-JSON numeric constant")


def read_config(path: Path) -> ConfigSnapshot:
    """Read the explicit destination without resolving a target-file symlink."""
    try:
        absolute = path.expanduser().absolute()
        # Freeze the parent location while retaining the target's symlink identity.
        absolute = absolute.parent.resolve(strict=True) / absolute.name
        raw, identity = _read_regular(absolute)
        document = (
            json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            if raw is not None
            else {}
        )
    except (OSError, ValueError, UnicodeError) as exc:
        raise EnrollmentError(
            "Cannot read config; check its parent, permissions and JSON syntax."
        ) from exc
    if not isinstance(document, dict):
        raise EnrollmentError("Config must contain a JSON object.")
    if not isinstance(document.get("mcpServers", {}), dict):
        raise EnrollmentError("Config mcpServers must be a JSON object.")
    return ConfigSnapshot(absolute, raw, identity, document)


def _https_origin(value: str, *, base_only: bool = False) -> str:
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise EnrollmentError("Server URL must be an HTTPS URL without credentials or whitespace.")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (
            parts.scheme.lower() != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or "\\" in value
            or (base_only and parts.path not in {"", "/"})
            or port == 0
        ):
            raise ValueError("invalid URL")
        host = host.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as exc:
        raise EnrollmentError(
            "Use an HTTPS server base URL, without credentials, query, fragment or base path."
        ) from exc
    host = f"[{host}]" if ":" in host else host
    return f"https://{host}" + (f":{port}" if port is not None and port != 443 else "")


def _existing_entry(
    snapshot: ConfigSnapshot,
    name: str,
    server_origin: str,
    *,
    replace: bool,
    transport: str = "http",
    service: str = "kb",
) -> tuple[dict[str, Any], str | None]:
    if not name.strip() or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise EnrollmentError("MCP server name must be nonempty and contain no control characters.")
    servers = snapshot.document.get("mcpServers", {})
    existing = servers.get(name, {})
    if not isinstance(existing, dict):
        raise EnrollmentError("Existing MCP server entry must be a JSON object.")
    headers = existing.get("headers", {})
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in headers.items()
    ):
        raise EnrollmentError("Existing MCP headers must be a JSON object containing strings.")
    same_origin = False
    managed_stdio = _is_managed_stdio(existing, server_origin, name, service)
    if managed_stdio and transport == "stdio":
        environment = existing.get("env", {})
        if not isinstance(environment, dict):
            raise EnrollmentError("Existing MCP environment must be a JSON object.")
        token = environment.get(TOKEN_ENV)
        return copy.deepcopy(existing), token if _valid_token(token) else None
    if "httpUrl" in existing:
        try:
            same_origin = _https_origin(existing["httpUrl"]) == server_origin
        except (EnrollmentError, TypeError, AttributeError):
            same_origin = False
    conflict = (
        bool(INCOMPATIBLE_ENTRY_FIELDS & existing.keys())
        or (bool(existing) and not same_origin)
        or (bool(existing) and transport == "stdio")
    )
    if conflict and not replace:
        raise EnrollmentError(
            "Existing MCP entry uses another origin, transport or service; "
            "use --replace to replace it."
        )
    bearer = None
    if same_origin:
        authorizations = [value for key, value in headers.items() if key.lower() == "authorization"]
        if len(authorizations) == 1:
            scheme, separator, supplied = authorizations[0].partition(" ")
            if separator and scheme.lower() == "bearer" and _valid_token(supplied):
                bearer = supplied
    return copy.deepcopy(existing), bearer


def _is_managed_stdio(entry: dict[str, Any], origin: str, name: str, service: str = "kb") -> bool:
    args = entry.get("args")
    if (
        entry.get("command") != sys.executable
        or not isinstance(args, list)
        or not all(isinstance(value, str) for value in args)
        or args[:3] != ["-m", "corporate_kb.access.client", "proxy"]
        or "httpUrl" in entry
        or "url" in entry
    ):
        return False
    try:
        if args.count("--service") > 1:
            return False
        # Entries created before service selection always connected to the knowledge MCP.
        saved_service = args[args.index("--service") + 1] if "--service" in args else "kb"
        return bool(
            args[args.index("--server-url") + 1] == origin
            and args[args.index("--name") + 1] == name
            and saved_service == service
        )
    except (ValueError, IndexError):
        return False


def _valid_token(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value))


def _service_name(service: str, name: str | None) -> str:
    if service not in {"kb", "skills"}:
        raise EnrollmentError("Service must be kb or skills.")
    return name if name is not None else f"corporate-{service}"


def _valid_mcp_path(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and bool(re.fullmatch(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?", value))
        and not any(segment in {".", ".."} for segment in value.split("/"))
    )


def _service_path(payload: dict[str, Any], service: str) -> str:
    path = payload.get("skills_mcp_path" if service == "skills" else "mcp_path")
    if service == "skills" and path is None:
        raise EnrollmentError(
            "Skills MCP is not advertised by this server; enable it or update the server first."
        )
    if not _valid_mcp_path(path) or (service == "skills" and path == payload.get("mcp_path")):
        raise EnrollmentError("Server returned an invalid enrollment response.")
    return path


def _enroll(
    origin: str, cert: Path, key: Path, ca: Path | None, bearer: str | None
) -> dict[str, Any]:
    try:
        context = ssl.create_default_context(cafile=str(ca) if ca is not None else None)
        # Empty password avoids an OpenSSL console prompt for encrypted keys.
        context.load_cert_chain(certfile=str(cert), keyfile=str(key), password=lambda: "")
    except (OSError, ValueError) as exc:
        raise EnrollmentError(
            "Cannot load client certificate/key or trusted CA. Check PEM files and permissions."
        ) from exc
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    try:
        with httpx.Client(
            verify=context,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = client.post(f"{origin}/auth/token", headers=headers, json={})
    except httpx.HTTPError as exc:
        raise EnrollmentError(
            "Enrollment connection failed. Check TLS trust, hostname, certificate and network."
        ) from exc
    if response.status_code == 403:
        raise EnrollmentError("Access denied or revoked. Contact an access administrator.")
    if 300 <= response.status_code < 400:
        raise EnrollmentError("Enrollment redirects are refused. Use the final HTTPS server URL.")
    if response.status_code != 200:
        raise EnrollmentError(
            f"Enrollment failed (HTTP {response.status_code}); config was not changed."
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise EnrollmentError("Server returned an invalid enrollment response.") from exc
    if not isinstance(payload, dict):
        raise EnrollmentError("Server returned an invalid enrollment response.")
    token = payload.get("access_token")
    expiry = payload.get("expires_at")
    path = payload.get("mcp_path")
    if (
        not _valid_token(token)
        or payload.get("token_type") != "Bearer"
        or not isinstance(expiry, int)
        or isinstance(expiry, bool)
        or expiry <= time.time()
        or not _valid_mcp_path(path)
    ):
        raise EnrollmentError("Server returned an invalid enrollment response.")
    return payload


def _write_config(snapshot: ConfigSnapshot, document: dict[str, Any]) -> None:
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{snapshot.path.name}.", suffix=".tmp", dir=snapshot.path.parent
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if hasattr(os, "fchmod"):
                os.fchmod(stream.fileno(), 0o600)
            else:
                os.chmod(temporary, 0o600)
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        current_raw, current_identity = _read_regular(snapshot.path)
        if current_raw != snapshot.raw or current_identity != snapshot.identity:
            raise EnrollmentError(
                "Config changed during enrollment; it was not overwritten. Retry."
            )
        os.replace(temporary, snapshot.path)
        temporary = None
    except (OSError, ValueError) as exc:
        raise EnrollmentError(
            "Cannot safely save config; check local permissions and retry."
        ) from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def connect_client(
    *,
    server_url: str,
    cert: Path,
    key: Path,
    config: Path,
    ca: Path | None = None,
    name: str | None = None,
    replace: bool = False,
    transport: str = "http",
    service: str = "kb",
) -> tuple[Path, int]:
    """Enroll over mTLS, then merge a bearer credential into one local MCP entry."""
    origin = _https_origin(server_url, base_only=True)
    name = _service_name(service, name)
    if transport not in {"http", "stdio"}:
        raise EnrollmentError("Transport must be http or stdio.")
    snapshot = read_config(config)
    entry, bearer = _existing_entry(
        snapshot, name, origin, replace=replace, transport=transport, service=service
    )
    payload = _enroll(origin, cert, key, ca, bearer)
    endpoint = origin + _service_path(payload, service)
    if "httpUrl" in entry and not replace and entry["httpUrl"] != endpoint:
        raise EnrollmentError(
            "Existing MCP entry targets another endpoint or service; use --replace to replace it."
        )
    if replace:
        for field in INCOMPATIBLE_ENTRY_FIELDS:
            entry.pop(field, None)
    if transport == "http":
        headers = {
            key: value
            for key, value in entry.get("headers", {}).items()
            if key.lower() != "authorization"
        }
        headers["Authorization"] = f"Bearer {payload['access_token']}"
        entry.update(httpUrl=endpoint, headers=headers)
    else:
        entry.pop("httpUrl", None)
        entry.pop("headers", None)
        args = [
            "-m",
            "corporate_kb.access.client",
            "proxy",
            "--server-url",
            origin,
            "--cert",
            str(cert.expanduser().absolute()),
            "--key",
            str(key.expanduser().absolute()),
            "--config",
            str(snapshot.path),
            "--name",
            name,
            "--service",
            service,
        ]
        if ca is not None:
            args += ["--ca", str(ca.expanduser().absolute())]
        environment = dict(entry.get("env", {}))
        environment[TOKEN_ENV] = payload["access_token"]
        entry.update(command=sys.executable, args=args, env=environment)
    document = copy.deepcopy(snapshot.document)
    document.setdefault("mcpServers", {})[name] = entry
    _write_config(snapshot, document)
    return snapshot.path, payload["expires_at"]


def _run_proxy(url: str, token: str, ca: Path | None) -> None:
    """Use FastMCP's native dynamic proxy; stdout belongs exclusively to MCP."""
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server import create_proxy

    context = ssl.create_default_context(cafile=str(ca) if ca is not None else None)

    def client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
        **_kwargs: Any,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=headers,
            auth=auth,
            timeout=timeout or httpx.Timeout(30.0, read=300.0),
            verify=context,
            follow_redirects=False,
            trust_env=False,
        )

    transport = StreamableHttpTransport(
        url, headers={"Authorization": f"Bearer {token}"}, httpx_client_factory=client_factory
    )
    create_proxy(Client(transport), name="Corporate KB access proxy").run(
        transport="stdio", show_banner=False
    )


def proxy_client(
    *,
    server_url: str,
    cert: Path,
    key: Path,
    config: Path,
    ca: Path | None = None,
    name: str | None = None,
    service: str = "kb",
) -> None:
    """Refresh the saved credential at each stdio startup before opening remote MCP."""
    origin = _https_origin(server_url, base_only=True)
    name = _service_name(service, name)
    snapshot = read_config(config)
    entry, bearer = _existing_entry(
        snapshot, name, origin, replace=False, transport="stdio", service=service
    )
    if not _is_managed_stdio(entry, origin, name, service):
        raise EnrollmentError(
            "Proxy entry is missing or changed; run connect --transport stdio first."
        )
    # Read the newest saved value, not a potentially stale inherited process environment.
    payload = _enroll(origin, cert, key, ca, bearer)
    endpoint = origin + _service_path(payload, service)
    if bearer != payload["access_token"]:
        entry["env"][TOKEN_ENV] = payload["access_token"]
        document = copy.deepcopy(snapshot.document)
        document["mcpServers"][name] = entry
        _write_config(snapshot, document)
    else:
        raw, identity = _read_regular(snapshot.path)
        if raw != snapshot.raw or identity != snapshot.identity:
            raise EnrollmentError("Config changed during enrollment; reconnect the client.")
    _run_proxy(endpoint, payload["access_token"], ca)


@app.callback()
def _application() -> None:
    """Client-side commands; the server never writes remote client settings."""


@app.command()
def connect(
    server_url: Annotated[str, typer.Option(help="HTTPS server base URL, no /mcp.")],
    cert: Annotated[Path, typer.Option(help="Personal client certificate/chain in PEM format.")],
    key: Annotated[Path, typer.Option(help="Local PEM private key; never uploaded.")],
    config: Annotated[Path, typer.Option(help="Explicit local MCP JSON settings file.")],
    ca: Annotated[Path | None, typer.Option(help="Server CA bundle, or system trust.")] = None,
    name: Annotated[
        str | None, typer.Option(help="MCP entry name; default corporate-kb or corporate-skills.")
    ] = None,
    replace: Annotated[
        bool, typer.Option(help="Allow replacing another origin/transport.")
    ] = False,
    transport: Annotated[str, typer.Option(help="http, or stdio for startup renewal.")] = "http",
    service: Annotated[str, typer.Option(help="MCP service: kb or skills.")] = "kb",
) -> None:
    """Obtain or reuse an access token and save it in the chosen MCP configuration."""
    try:
        path, expiry = connect_client(
            server_url=server_url,
            cert=cert,
            key=key,
            config=config,
            ca=ca,
            name=name,
            replace=replace,
            transport=transport,
            service=service,
        )
    except EnrollmentError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"MCP entry '{_service_name(service, name)}' saved to {path}. "
        f"Token expires at Unix timestamp {expiry}."
    )
    typer.echo("Restart or reconnect the MCP client to reload its settings.")


@app.command()
def proxy(
    server_url: Annotated[str, typer.Option()],
    cert: Annotated[Path, typer.Option()],
    key: Annotated[Path, typer.Option()],
    config: Annotated[Path, typer.Option()],
    ca: Annotated[Path | None, typer.Option()] = None,
    name: Annotated[str | None, typer.Option()] = None,
    service: Annotated[str, typer.Option(help="MCP service: kb or skills.")] = "kb",
) -> None:
    """MCP stdio launcher with certificate-based enrollment at every process startup."""
    try:
        proxy_client(
            server_url=server_url,
            cert=cert,
            key=key,
            config=config,
            ca=ca,
            name=name,
            service=service,
        )
    except EnrollmentError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except Exception:
        # Never interpolate third-party exceptions: they may include request credentials.
        typer.echo("MCP proxy failed; check connectivity, server access and reconnect.", err=True)
        raise typer.Exit(code=1) from None


def main() -> None:
    app()


if __name__ == "__main__":
    main()
