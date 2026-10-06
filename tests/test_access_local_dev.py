"""Isolated localhost certificate bootstrap, launcher and live browser enrollment."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from test_access_proxy import _https_server

from corporate_kb.access import local_dev
from corporate_kb.access.dev_pki import prepare_local_identity
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import create_service

WRITABLE_PATH_FIELDS = (
    "knowledge_dir",
    "cache_dir",
    "ssot_knowledge_dir",
    "ssot_cache_dir",
    "managed_tools_path",
    "builtin_tool_overrides_path",
    "mcp_servers_path",
    "index_catalog_path",
    "managed_indexes_dir",
    "repository_cache_dir",
    "graph_store_path",
    "service_map_path",
    "analysis_archive_dir",
    "job_logs_dir",
    "domscribe_workspace_root",
    "access_db_path",
    "access_client_ca_file",
    "mcp_tls_cert_file",
    "mcp_tls_key_file",
)


@pytest.fixture
def local_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project


def _assert_local_settings(settings, state_dir: Path, *, port: int):
    assert settings.access_enabled is True
    assert settings.mcp_tls_enabled is True
    assert settings.mcp_http_host == "127.0.0.1"
    assert settings.mcp_http_port == port
    assert settings.mcp_http_path == "/mcp"
    assert settings.embedding_provider == "hash"
    assert settings.embedding_local_files_only is True
    assert settings.auto_index is True
    assert settings.ssot_enabled is False
    assert settings.gigacode_enabled is False
    assert settings.domscribe_enabled is False
    assert settings.admin_password is None
    assert settings.mcp_http_bearer_token is None
    for name in WRITABLE_PATH_FIELDS:
        value = getattr(settings, name)
        assert isinstance(value, Path), name
        assert value.is_absolute(), name
        assert value.is_relative_to(state_dir.resolve()), (name, value)


def test_build_local_settings_ignores_hostile_environment_and_dotenv(
    local_project, tmp_path, monkeypatch
):
    state = tmp_path / "isolated"
    identity = prepare_local_identity(state / "identity")
    production = tmp_path / "production"
    production.mkdir()
    sentinel = production / "must-remain-unchanged"
    sentinel.write_text("existing data")
    (local_project / ".env").write_text(
        "KB_MCP_HTTP_HOST=0.0.0.0\n"
        "KB_MCP_HTTP_PORT=9000\n"
        "KB_ACCESS_ENABLED=false\n"
        "KB_MCP_TLS_ENABLED=false\n"
        "KB_EMBEDDING_PROVIDER=sentence_transformers\n"
        "KB_GIGACODE_ENABLED=true\n"
        "KB_SSOT_ENABLED=true\n"
        "KB_DOMSCRIBE_ENABLED=true\n"
        f"KB_ACCESS_DB_PATH={production / 'access.sqlite3'}\n"
        f"KB_CACHE_DIR={production / 'cache'}\n"
    )
    monkeypatch.chdir(local_project)
    hostile = {
        "KB_MCP_HTTP_HOST": "0.0.0.0",
        "KB_MCP_HTTP_PORT": "not-a-port",
        "KB_MCP_TLS_ENABLED": "false",
        "KB_ACCESS_ENABLED": "false",
        "KB_ACCESS_CLIENT_CA_FILE": str(production / "unknown-ca.pem"),
        "KB_ACCESS_BOOTSTRAP_ADMIN_USERNAME": "production-admin",
        "KB_ACCESS_BOOTSTRAP_ADMIN_PASSWORD": "production-secret-must-not-be-used",
        "KB_EMBEDDING_PROVIDER": "sentence_transformers",
        "KB_EMBEDDING_DIMENSION": "4096",
        "KB_EMBEDDING_DEVICE": "cuda",
        "KB_DEFAULT_TOP_K": "19",
        "KB_GIGACODE_ENABLED": "true",
        "KB_SSOT_ENABLED": "true",
        "KB_DOMSCRIBE_ENABLED": "true",
        "KB_ADMIN_PASSWORD": "production-legacy-password",
        "KB_MCP_HTTP_BEARER_TOKEN": "production-legacy-token-that-must-not-be-used",
        "KB_AUTO_INDEX": "false",
        "KB_LOG_LEVEL": "CRITICAL",
    }
    for field in WRITABLE_PATH_FIELDS:
        hostile[f"KB_{field.upper()}"] = str(production / field)
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)

    settings = local_dev.build_local_settings(local_project, state, identity, port=9443)
    _assert_local_settings(settings, state, port=9443)
    assert settings.embedding_dimension == 1024
    assert settings.default_top_k == 3
    assert settings.embedding_device != "cuda"
    assert settings.log_level != "CRITICAL"
    assert settings.access_bootstrap_admin_username == identity.admin_username
    assert settings.access_bootstrap_admin_password.get_secret_value() == identity.admin_password
    assert settings.access_client_ca_file == identity.ca_cert
    assert settings.mcp_tls_cert_file == identity.server_cert
    assert settings.mcp_tls_key_file == identity.server_key
    assert list(production.iterdir()) == [sentinel]
    assert sentinel.read_text() == "existing data"


def test_prepare_creates_sample_and_reuses_personal_identity(local_project, tmp_path):
    state = tmp_path / "local-state"
    first = local_dev.prepare_local_environment(local_project, state, port=8443)
    _assert_local_settings(first, state, port=8443)
    samples = list(first.knowledge_dir.rglob("*.md"))
    assert samples
    assert all(path.read_text().strip() for path in samples)
    identity_before = prepare_local_identity(state / "identity")
    bytes_before = {path.name: path.read_bytes() for path in (state / "identity").iterdir()}

    second = local_dev.prepare_local_environment(local_project, state, port=8444)
    identity_after = prepare_local_identity(state / "identity")
    assert second.mcp_http_port == 8444
    assert identity_before == identity_after
    assert bytes_before == {path.name: path.read_bytes() for path in (state / "identity").iterdir()}
    assert first.access_db_path == second.access_db_path
    assert (
        first.access_bootstrap_admin_password.get_secret_value()
        == second.access_bootstrap_admin_password.get_secret_value()
    )
    assert not (local_project / ".cache").exists()
    assert not (local_project / "certs").exists()


def test_prepare_preserves_existing_sample_edits(local_project, tmp_path):
    state = tmp_path / "local-state"
    settings = local_dev.prepare_local_environment(local_project, state)
    sample = next(settings.knowledge_dir.rglob("*.md"))
    sample.write_text("# My changed local test document\n\nKeep this edit.\n")
    local_dev.prepare_local_environment(local_project, state)
    assert "Keep this edit" in sample.read_text()


def test_prepare_refuses_nonempty_unrelated_directory(local_project, tmp_path):
    state = tmp_path / "other-data"
    state.mkdir()
    original = state / "keep.txt"
    original.write_text("important existing data")
    with pytest.raises(ValueError, match="not an initialized"):
        local_dev.prepare_local_environment(local_project, state)
    assert list(state.iterdir()) == [original]
    assert original.read_text() == "important existing data"


def test_prepare_refuses_project_directory_as_state(local_project):
    with pytest.raises(ValueError, match="dedicated state directory"):
        local_dev.prepare_local_environment(local_project, local_project)
    assert list(local_project.iterdir()) == []


def test_prepare_refuses_symlink_state_without_touching_target(local_project, tmp_path):
    target = tmp_path / "other-data"
    target.mkdir()
    state = tmp_path / "state-link"
    state.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        local_dev.prepare_local_environment(local_project, state)
    assert list(target.iterdir()) == []
    assert state.is_symlink()


def test_prepare_refuses_symlink_cache_escape(local_project, tmp_path):
    state = tmp_path / "local-state"
    settings = local_dev.prepare_local_environment(local_project, state)
    outside = tmp_path / "outside"
    outside.mkdir()
    assert not settings.cache_dir.exists()
    settings.cache_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the local playground"):
        local_dev.prepare_local_environment(local_project, state)
    assert settings.cache_dir.is_symlink()
    assert list(outside.iterdir()) == []


def test_prepare_only_cli_does_not_start_server_or_print_credentials(
    local_project, tmp_path, capsys
):
    state = tmp_path / "local-state"
    local_dev.main(
        [
            "--project-root",
            str(local_project),
            "--state-dir",
            str(state),
            "--port",
            "9443",
            "--prepare-only",
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    identity = prepare_local_identity(state / "identity")
    assert ":9443/connect" in output
    assert ":9443/access-admin" in output
    assert str(identity.ca_cert) in output
    assert str(identity.client_p12) in output
    assert identity.admin_password not in output
    assert identity.p12_password not in output
    assert "PRIVATE KEY" not in output
    assert not (state / "access.sqlite3").exists()
    assert not list(state.rglob("*.sqlite3"))


def test_show_secrets_cli_is_explicit_and_reuses_existing_credentials(
    local_project, tmp_path, capsys
):
    state = tmp_path / "local-state"
    settings = local_dev.prepare_local_environment(local_project, state)
    identity = prepare_local_identity(state / "identity")
    local_dev.main(
        [
            "--project-root",
            str(local_project),
            "--state-dir",
            str(state),
            "--prepare-only",
            "--show-secrets",
        ]
    )
    output = capsys.readouterr().out
    assert identity.admin_password in output
    assert identity.p12_password in output
    assert settings.access_bootstrap_admin_password.get_secret_value() in output
    assert "PRIVATE KEY" not in output


def test_open_certificate_dialogs_require_explicit_flag(
    local_project, tmp_path, monkeypatch, capsys
):
    state = tmp_path / "local-state"
    opened = []
    monkeypatch.setattr(local_dev.sys, "platform", "darwin")
    monkeypatch.setattr(
        local_dev.subprocess, "run", lambda command, **kwargs: opened.append((command, kwargs))
    )
    base = ["--project-root", str(local_project), "--state-dir", str(state), "--prepare-only"]
    local_dev.main(base)
    assert opened == []
    local_dev.main([*base, "--open-certificates"])
    identity = prepare_local_identity(state / "identity")
    assert opened == [(["open", str(identity.ca_cert), str(identity.client_p12)], {"check": True})]
    output = capsys.readouterr().out
    assert identity.admin_password not in output
    assert identity.p12_password not in output


def test_open_certificate_dialogs_on_nonmac_fails_before_writes(
    local_project, tmp_path, monkeypatch
):
    state = tmp_path / "must-not-be-created"
    monkeypatch.setattr(local_dev.sys, "platform", "linux")
    with pytest.raises(SystemExit) as error:
        local_dev.main(
            [
                "--project-root",
                str(local_project),
                "--state-dir",
                str(state),
                "--prepare-only",
                "--open-certificates",
            ]
        )
    assert error.value.code != 0
    assert not state.exists()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "invalid"])
def test_invalid_cli_port_fails_before_creating_files(local_project, tmp_path, port):
    state = tmp_path / "must-not-be-created"
    with pytest.raises(SystemExit) as error:
        local_dev.main(
            [
                "--project-root",
                str(local_project),
                "--state-dir",
                str(state),
                "--port",
                port,
                "--prepare-only",
            ]
        )
    assert error.value.code != 0
    assert not state.exists()


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_api_port_fails_before_creating_identity(local_project, tmp_path, port):
    state = tmp_path / "must-not-be-created"
    with pytest.raises(ValueError):
        local_dev.prepare_local_environment(local_project, state, port=port)
    assert not state.exists()


def test_launcher_dispatches_local_before_legacy_tls_defaults(tmp_path):
    """Execute only a fake Python recorder, never a runtime or real certificate generator."""
    source_project = Path(__file__).resolve().parents[1]
    project = tmp_path / "launcher-project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(source_project / "scripts/start-mcp-http.sh", scripts / "start-mcp-http.sh")
    shutil.copy2(source_project / "scripts/activate-venv.sh", scripts / "activate-venv.sh")
    python_dir = project / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    python = python_dir / "python"
    python.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'args': sys.argv[1:]}}))\n"
    )
    python.chmod(0o700)
    (python_dir / "activate").write_text(f'export VIRTUAL_ENV="{project / ".venv"}"\n')
    process = subprocess.run(
        ["bash", str(scripts / "start-mcp-http.sh"), "local", "--prepare-only", "--port", "9443"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
        env={**os.environ, "KB_MCP_TLS_ENABLED": "invalid-legacy-boolean"},
    )
    assert process.returncode == 0, process.stderr
    args = json.loads(process.stdout)["args"]
    assert args[:2] == ["-m", "corporate_kb.access.local_dev"]
    assert "--prepare-only" in args
    assert args[args.index("--port") + 1] == "9443"
    assert not (project / "certs").exists()
    assert not (project / ".cache").exists()


@pytest.mark.asyncio
async def test_local_generated_identity_works_for_real_browser_https_flow(local_project, tmp_path):
    state = tmp_path / "local-state"
    settings = local_dev.prepare_local_environment(local_project, state)
    identity = prepare_local_identity(state / "identity")
    service = create_service(settings)
    stats = service.load_read_index()
    assert stats.document_count >= 1
    app = create_http_app(service, settings)

    async with _https_server(app, settings) as origin:
        context = ssl.create_default_context(cafile=str(identity.ca_cert))
        async with httpx.AsyncClient(verify=context, base_url=origin, timeout=5) as anonymous:
            page = await anonymous.get("/connect")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "no-store"
            denied = await anonymous.post("/auth/mcp-config", json={}, headers={"origin": origin})
            assert denied.status_code == 403
            status = await anonymous.get("/auth/status")
            assert status.json()["enabled"] is True
            assert status.json()["certificate_present"] is False

        context = ssl.create_default_context(cafile=str(identity.ca_cert))
        context.load_cert_chain(str(identity.client_cert), str(identity.client_key))
        async with httpx.AsyncClient(verify=context, base_url=origin, timeout=5) as browser:
            status = await browser.get("/auth/status")
            assert status.status_code == 200
            assert status.json()["certificate_present"] is True
            response = await browser.post("/auth/mcp-config", json={}, headers={"origin": origin})
            assert response.status_code == 200, response.text
            entry = response.json()["config"]["mcpServers"]["corporate-kb"]
            assert entry["httpUrl"] == origin + "/mcp"

        context = ssl.create_default_context(cafile=str(identity.ca_cert))
        async with httpx.AsyncClient(
            verify=context, base_url=origin, timeout=5, headers=entry["headers"]
        ) as token_client:
            response = await token_client.get("/api/v1/stats")
            assert response.status_code == 200
            assert response.json()["document_count"] == stats.document_count
        assert settings.access_db_path.is_file()
    assert not (local_project / ".cache").exists()
