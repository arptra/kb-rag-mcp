"""An isolated HTTPS access playground for Linux/macOS; never a production profile."""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from corporate_kb.access.dev_client_bundle import export_client_bundle
from corporate_kb.access.dev_pki import (
    LocalIdentity,
    normalize_server_name,
    prepare_local_identity,
)
from corporate_kb.config import Settings

_SAMPLE = """# Local access playground

This isolated knowledge base is for testing certificate login, personal MCP tokens,
the dashboard and access revocation. No production documents are included.
Open /connect to obtain a personal MCP configuration, or /access-admin to manage access.
"""


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing a symlink directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    path.chmod(0o700)


def _network_options(host: str, server_name: str) -> tuple[str, str]:
    server_name = normalize_server_name(server_name)
    address = ipaddress.ip_address(host)
    if address.is_multicast:
        raise ValueError("Bind address must not be multicast")
    if not address.is_loopback and _loopback_name(server_name):
        raise ValueError("Network binding requires --server-name with the server's real DNS/IP")
    return str(address), server_name


def _loopback_name(server_name: str) -> bool:
    try:
        return ipaddress.ip_address(server_name).is_loopback
    except ValueError:
        return server_name == "localhost" or server_name.endswith(".localhost")


def _server_url(server_name: str, port: int) -> str:
    host = f"[{server_name}]" if ":" in server_name else server_name
    return f"https://{host}:{port}"


def build_local_settings(
    project_root: Path,
    state_dir: Path,
    identity: LocalIdentity,
    *,
    port: int = 8443,
    host: str = "127.0.0.1",
    server_name: str = "localhost",
    client_certificate_mode: Literal["trusted_ca", "presented"] = "presented",
) -> Settings:
    """Explicit defaults prevent both .env and inherited KB_* from escaping the sandbox."""
    # Supplying every field is intentional: BaseSettings otherwise reads inherited
    # environment variables even with _env_file=None. New fields also start at defaults.
    host, _ = _network_options(host, server_name)
    values: dict[str, Any] = {
        name: field.get_default(call_default_factory=True)
        for name, field in Settings.model_fields.items()
    }
    paths = {
        "knowledge_dir": "knowledge",
        "cache_dir": "kb",
        "ssot_knowledge_dir": "ssot",
        "ssot_cache_dir": "ssot-cache",
        "benchmark_questions_path": "evaluation/questions.json",
        "managed_tools_path": "managed_tools.json",
        "builtin_tool_overrides_path": "builtin_tool_overrides.json",
        "mcp_servers_path": "mcp_servers.json",
        "index_catalog_path": "index_catalog.json",
        "managed_indexes_dir": "indexes",
        "repository_cache_dir": "repositories",
        "graph_store_path": "system_graph.json",
        "service_map_path": "service_map.json",
        "analysis_archive_dir": "analysis",
        "job_logs_dir": "job-logs",
        "ssot_skill_path": "skills/build-service-ssot",
        "domscribe_workspace_root": "workspace",
        "access_db_path": "access.sqlite3",
    }
    state_dir = state_dir.absolute()
    for name, relative in paths.items():
        path = state_dir / relative
        if path.is_symlink() or not path.resolve().is_relative_to(state_dir.resolve()):
            raise ValueError(f"Refusing a path outside the local playground: {path}")
        values[name] = path
    values.update(
        access_enabled=True,
        access_client_certificate_mode=client_certificate_mode,
        access_client_ca_file=identity.ca_cert,
        access_bootstrap_admin_username=identity.admin_username,
        access_bootstrap_admin_password=identity.admin_password,
        mcp_tls_enabled=True,
        mcp_tls_cert_file=identity.server_cert,
        mcp_tls_key_file=identity.server_key,
        mcp_http_host=host,
        mcp_http_port=port,
        mcp_http_path="/mcp",
        embedding_provider="hash",
        auto_index=True,
        ssot_enabled=False,
        gigacode_enabled=False,
        domscribe_enabled=False,
    )
    return Settings(_env_file=None, **values).resolved(project_root)


def prepare_local_environment(
    project_root: Path,
    state_dir: Path,
    *,
    port: int = 8443,
    host: str = "127.0.0.1",
    server_name: str = "localhost",
    client_certificate_mode: Literal["trusted_ca", "presented"] = "presented",
) -> Settings:
    """Persist a reusable identity and create a tiny, separate sample knowledge base."""
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    host, server_name = _network_options(host, server_name)
    project_root = project_root.resolve()
    state_dir = state_dir.expanduser().absolute()
    if state_dir.is_symlink():
        raise ValueError(f"Refusing a symlink state directory: {state_dir}")
    state_dir = state_dir.resolve()
    if state_dir in (project_root, Path.home().resolve()) or state_dir in project_root.parents:
        raise ValueError("Choose a dedicated state directory, not a project/home/root directory")
    if state_dir.exists() and any(state_dir.iterdir()) and not (state_dir / "identity").exists():
        raise ValueError("State directory is not an initialized local playground; use an empty one")
    _private_directory(state_dir)
    identity = prepare_local_identity(state_dir / "identity", server_names=(server_name,))
    settings = build_local_settings(
        project_root,
        state_dir,
        identity,
        port=port,
        host=host,
        server_name=server_name,
        client_certificate_mode=client_certificate_mode,
    )
    _private_directory(settings.knowledge_dir)
    sample = settings.knowledge_dir / "local-access.md"
    if sample.is_symlink():
        raise ValueError(f"Refusing a symlink sample document: {sample}")
    try:
        descriptor = os.open(sample, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not stat.S_ISREG(sample.stat().st_mode):
            raise ValueError(f"Not a regular sample document: {sample}") from None
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(_SAMPLE)
    return settings


def _instructions(
    identity: LocalIdentity,
    settings: Settings,
    *,
    server_name: str,
    bundle: Path,
    show_secrets: bool,
) -> None:
    url = _server_url(server_name, settings.mcp_http_port)
    print(f"Development only. Binding to {settings.mcp_http_host}; .env and KB_* are ignored.")
    print(f"Connect:      {url}/connect")
    print(f"Dashboard:    {url}/admin")
    print(f"Access admin: {url}/access-admin")
    print(f"MCP:          {url}/mcp")
    print(f"Access DB:    {settings.access_db_path}")
    print(f"Client certificate policy: {settings.access_client_certificate_mode}")
    print(f"CA to trust:  {identity.ca_cert}")
    print(f"Client ID:    {identity.client_p12}")
    print(f"Client ZIP:   {bundle} (contains a personal private key; transfer securely)")
    print(f"Credentials:  {identity.credentials_file} (private file; do not share)")
    print(f"Admin login:  {identity.admin_username}")
    if show_secrets:
        print(f"Admin password: {identity.admin_password}")
        print(f"P12 password:   {identity.p12_password}")
    else:
        print("Passwords are hidden. Use --prepare-only --show-secrets to display them locally.")
    if settings.access_client_certificate_mode == "presented":
        print("Existing browser client certificates from ANY issuer provide the CN account name;")
        print("certificate dates/EKU are metadata, not access checks. No P12 import")
        print("is needed if your browser already has a suitable client certificate and key.")
        print("WARNING: open enrollment, not verified employee identity. The SAME CN means the")
        print("same account even on another certificate/key. Another CN creates a new account.")
        print("The browser must still accept the SERVER certificate. If it already allows")
        print("development HTTPS on localhost, no browser trust change is needed there.")
        print("If you have no existing client identity, the optional test ZIP can be imported:")
    print("On the BROWSER computer: extract the test ZIP; trust ca.crt for SSL if needed and")
    print("import client.p12 with its password. Restart the browser and open the Connect URL.")
    print("macOS: Keychain Access > login; CA > Trust > Secure Sockets Layer > Always Trust.")
    print("Windows: current-user Trusted Root Certification Authorities (CA), Personal (P12).")
    print("Linux/Firefox: Settings > Privacy & Security > Certificates > View Certificates;")
    print("Authorities (CA) and Your Certificates (P12). Other browsers have their own stores.")
    print("Send only the ZIP to YOUR client computer; communicate its P12 password separately.")
    print("Do not send credentials.json, server.key or the entire state directory.")
    print("The MCP client also needs CA trust. The ZIP does not contain an MCP token yet.")
    if not ipaddress.ip_address(settings.mcp_http_host).is_loopback:
        print("NETWORK MODE: allow this port only from your test network/client in the firewall.")
        print(
            "DNS/IP must resolve to this server. "
            "'localhost' on another computer is NOT this server."
        )
    print("Certificates and passwords are reused. Existing admin passwords are never reset.")
    print("No trust store is changed automatically. Stop the foreground server with Ctrl+C.")
    sys.stdout.flush()


def _open_certificates(identity: LocalIdentity) -> None:
    """Optional desktop convenience; a headless Linux server must keep working."""
    if sys.platform == "darwin":
        commands = [["open", str(identity.ca_cert), str(identity.client_p12)]]
    elif sys.platform.startswith("linux") and (
        os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")
    ):
        opener = shutil.which("xdg-open")
        commands = (
            [[opener, str(path)] for path in (identity.ca_cert, identity.client_p12)]
            if opener
            else []
        )
    else:
        commands = []
    if not commands:
        print(
            "No desktop certificate opener available. "
            "Import the client ZIP on the browser computer."
        )
    for command in commands:
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError):
            print(
                "Certificate opener failed; import the client ZIP manually on the browser computer."
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8443, help="HTTPS port (default: 8443)")
    parser.add_argument(
        "--host", default="127.0.0.1", help="bind IP; 0.0.0.0 for explicit LAN access"
    )
    parser.add_argument(
        "--server-name",
        default="localhost",
        help="bare DNS/IP used by clients and included in TLS SAN",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="private state (default: .cache/local-access or network-access)",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--prepare-only", action="store_true", help="prepare files without serving")
    parser.add_argument(
        "--client-certificate-mode",
        choices=("presented", "trusted_ca"),
        default="presented",
        help="presented accepts existing browser certificates from any CA; trusted_ca uses test CA",
    )
    parser.add_argument(
        "--show-secrets", action="store_true", help="print passwords to this terminal"
    )
    parser.add_argument(
        "--open-certificates",
        action="store_true",
        help="optional desktop import helper; headless servers print instructions instead",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        host, server_name = _network_options(args.host, args.server_name)
    except ValueError as exc:
        parser.error(str(exc))
    project_root = args.project_root.resolve()
    profile = "local-access" if _loopback_name(server_name) else "network-access"
    state_dir = args.state_dir or project_root / ".cache" / profile
    try:
        settings = prepare_local_environment(
            project_root,
            state_dir,
            port=args.port,
            host=host,
            server_name=server_name,
            client_certificate_mode=args.client_certificate_mode,
        )
        identity = prepare_local_identity(
            settings.access_db_path.parent / "identity",
            server_names=(server_name,),
        )
        bundle = export_client_bundle(
            identity,
            settings.access_db_path.parent,
            _server_url(server_name, args.port),
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Local setup failed: {exc}\nNo existing identity was replaced.\n")
    _instructions(
        identity,
        settings,
        server_name=server_name,
        bundle=bundle,
        show_secrets=args.show_secrets,
    )
    if args.open_certificates:
        _open_certificates(identity)
    if not args.prepare_only:
        from corporate_kb.mcp.http_server import main as serve

        serve(settings)


if __name__ == "__main__":
    main()
