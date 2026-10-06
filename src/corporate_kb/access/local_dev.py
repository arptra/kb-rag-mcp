"""An isolated, loopback-only HTTPS access playground; never a production profile."""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from corporate_kb.access.dev_pki import LocalIdentity, prepare_local_identity
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


def build_local_settings(
    project_root: Path,
    state_dir: Path,
    identity: LocalIdentity,
    *,
    port: int = 8443,
) -> Settings:
    """Explicit defaults prevent both .env and inherited KB_* from escaping the sandbox."""
    # Supplying every field is intentional: BaseSettings otherwise reads inherited
    # environment variables even with _env_file=None. New fields also start at defaults.
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
        access_client_ca_file=identity.ca_cert,
        access_bootstrap_admin_username=identity.admin_username,
        access_bootstrap_admin_password=identity.admin_password,
        mcp_tls_enabled=True,
        mcp_tls_cert_file=identity.server_cert,
        mcp_tls_key_file=identity.server_key,
        mcp_http_host="127.0.0.1",
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
) -> Settings:
    """Persist a reusable identity and create a tiny, separate sample knowledge base."""
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
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
    identity = prepare_local_identity(state_dir / "identity")
    settings = build_local_settings(project_root, state_dir, identity, port=port)
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


def _instructions(identity: LocalIdentity, settings: Settings, *, show_secrets: bool) -> None:
    url = f"https://localhost:{settings.mcp_http_port}"
    print("Local development only. Binding to 127.0.0.1; .env and KB_* are ignored.")
    print(f"Connect:      {url}/connect")
    print(f"Dashboard:    {url}/admin")
    print(f"Access admin: {url}/access-admin")
    print(f"MCP:          {url}/mcp")
    print(f"Access DB:    {settings.access_db_path}")
    print(f"CA to trust:  {identity.ca_cert}")
    print(f"Client ID:    {identity.client_p12}")
    print(f"Credentials:  {identity.credentials_file} (private file; do not share)")
    print(f"Admin login:  {identity.admin_username}")
    if show_secrets:
        print(f"Admin password: {identity.admin_password}")
        print(f"P12 password:   {identity.p12_password}")
    else:
        print("Passwords are hidden. Use --prepare-only --show-secrets to display them locally.")
    print("First browser use: import ca.pem and trust it for SSL; import client.p12 with its")
    print("password into personal identities, then restart the browser and visit /connect.")
    print("macOS: Keychain Access > login; CA > Trust > Secure Sockets Layer > Always Trust.")
    print("The MCP client also needs to trust this CA. Never share the CA/client private keys.")
    print("Certificates and passwords are reused. Existing admin passwords are never reset.")
    print("No trust store is changed automatically. Stop the foreground server with Ctrl+C.")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port", type=int, default=8443, help="localhost HTTPS port (default: 8443)"
    )
    parser.add_argument(
        "--state-dir", type=Path, help="private state (default: .cache/local-access)"
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--prepare-only", action="store_true", help="prepare files without serving")
    parser.add_argument(
        "--show-secrets", action="store_true", help="print passwords to this terminal"
    )
    parser.add_argument(
        "--open-certificates",
        action="store_true",
        help="macOS: open certificate import dialogs (trust still needs manual confirmation)",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.open_certificates and sys.platform != "darwin":
        parser.error("--open-certificates is macOS-only; import ca.pem and client.p12 manually")
    project_root = args.project_root.resolve()
    state_dir = args.state_dir or project_root / ".cache" / "local-access"
    try:
        settings = prepare_local_environment(project_root, state_dir, port=args.port)
        identity = prepare_local_identity(settings.access_db_path.parent / "identity")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Local setup failed: {exc}\nNo existing identity was replaced.\n")
    _instructions(identity, settings, show_secrets=args.show_secrets)
    if args.open_certificates:
        subprocess.run(["open", str(identity.ca_cert), str(identity.client_p12)], check=True)
    if not args.prepare_only:
        from corporate_kb.mcp.http_server import main as serve

        serve(settings)


if __name__ == "__main__":
    main()
