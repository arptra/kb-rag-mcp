"""Hermetic normal-launcher checks: no listeners, installs, or real certificates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from corporate_kb.access import launcher
from corporate_kb.config import Settings

PROJECT = Path(__file__).resolve().parents[1]
TRACKED_SETTINGS = (
    "KB_EMBEDDING_PROVIDER",
    "KB_EMBEDDING_LOCAL_FILES_ONLY",
    "KB_AUTO_INDEX",
    "KB_MCP_HTTP_HOST",
    "KB_MCP_HTTP_PORT",
    "KB_MCP_HTTP_PATH",
    "KB_MCP_TLS_ENABLED",
    "KB_MCP_TLS_CERT_FILE",
    "KB_MCP_TLS_KEY_FILE",
)


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch):
    for key in os.environ:
        if key.startswith("KB_"):
            monkeypatch.delenv(key)


@pytest.fixture
def normal_project(tmp_path):
    project = tmp_path / "normal deployment"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in ("start-mcp-http.sh", "activate-venv.sh"):
        shutil.copy2(PROJECT / "scripts" / name, scripts / name)
    generator = scripts / "generate-dev-certs.sh"
    generator.write_text('#!/usr/bin/env bash\ntouch "$PWD/generator-called"\nexit 90\n')
    generator.chmod(0o700)
    bin_dir = project / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path('python-called').touch()\n"
        "from corporate_kb.config import Settings\n"
        "if sys.argv[1:3] == ['-m', 'corporate_kb.access.launcher']:\n"
        "    from corporate_kb.access.launcher import main\n"
        "    main()\n"
        "    raise SystemExit(0)\n"
        "assert sys.argv[1:3] == ['-m', 'corporate_kb.mcp.http_server']\n"
        "settings = Settings().resolved()\n"
        "if os.environ.get('TEST_VALIDATE_TLS') == '1':\n"
        "    from corporate_kb.mcp.http_server import tls_uvicorn_config\n"
        "    tls_uvicorn_config(settings)\n"
        "print(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'host': settings.mcp_http_host,\n"
        "    'port': settings.mcp_http_port,\n"
        "    'path': settings.mcp_http_path,\n"
        "    'tls': settings.mcp_tls_enabled,\n"
        "    'cert': str(settings.mcp_tls_cert_file),\n"
        "    'key': str(settings.mcp_tls_key_file),\n"
        "    'provider': settings.embedding_provider,\n"
        "    'local_files_only': settings.embedding_local_files_only,\n"
        "    'auto_index': settings.auto_index,\n"
        f"    'environment': {{key: os.environ.get(key) for key in {TRACKED_SETTINGS!r}}},\n"
        "}))\n"
    )
    python.chmod(0o700)
    (bin_dir / "activate").write_text(f'export VIRTUAL_ENV="{project / ".venv"}"\n')
    return project


def run_launcher(project, *args, environment=None):
    return subprocess.run(
        ["bash", str(project / "scripts" / "start-mcp-http.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
        env={
            **os.environ,
            "PYTHONPATH": str(PROJECT / "src"),
            **(environment or {}),
        },
    )


def test_normal_run_honors_dotenv_without_exporting_defaults(normal_project):
    (normal_project / ".env").write_text(
        "KB_MCP_HTTP_HOST=192.0.2.12\n"
        "KB_MCP_HTTP_PORT=9443\n"
        "KB_MCP_HTTP_PATH=/corporate-mcp\n"
        "KB_MCP_TLS_ENABLED=false\n"
        "KB_MCP_TLS_CERT_FILE='deployment certs/server.pem'\n"
        "KB_MCP_TLS_KEY_FILE='deployment certs/server-key.pem'\n"
        "KB_EMBEDDING_PROVIDER=sentence_transformers\n"
        "KB_EMBEDDING_LOCAL_FILES_ONLY=false\n"
        "KB_AUTO_INDEX=true\n"
    )
    process = run_launcher(normal_project, "run", "--unchanged-extra-argument")
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["host"] == "192.0.2.12"
    assert payload["port"] == 9443
    assert payload["path"] == "/corporate-mcp"
    assert payload["tls"] is False
    assert payload["cert"] == str(normal_project / "deployment certs/server.pem")
    assert payload["key"] == str(normal_project / "deployment certs/server-key.pem")
    assert payload["provider"] == "sentence_transformers"
    assert payload["local_files_only"] is False
    assert payload["auto_index"] is True
    assert payload["args"][-1] == "--unchanged-extra-argument"
    assert all(value is None for value in payload["environment"].values())
    assert not (normal_project / "generator-called").exists()
    assert not (normal_project / "certs").exists()
    assert not (normal_project / ".cache/kb/runtime/mcp-http.pid").exists()


def test_explicit_environment_has_priority_over_dotenv(normal_project):
    (normal_project / ".env").write_text(
        "KB_MCP_HTTP_HOST=192.0.2.12\n"
        "KB_MCP_HTTP_PORT=9443\n"
        "KB_MCP_TLS_CERT_FILE=dotenv/server.crt\n"
        "KB_MCP_TLS_KEY_FILE=dotenv/server.key\n"
    )
    overrides = {
        "KB_MCP_HTTP_HOST": "127.0.0.1",
        "KB_MCP_HTTP_PORT": "9001",
        "KB_MCP_TLS_CERT_FILE": "env/server.crt",
        "KB_MCP_TLS_KEY_FILE": "env/server.key",
    }
    process = run_launcher(normal_project, "run", environment=overrides)
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 9001
    assert payload["cert"] == str(normal_project / "env/server.crt")
    assert payload["key"] == str(normal_project / "env/server.key")
    assert all(payload["environment"][key] == value for key, value in overrides.items())
    assert not (normal_project / "generator-called").exists()


def test_normal_run_uses_secure_settings_defaults(normal_project):
    process = run_launcher(normal_project, "run")
    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout)
    assert payload["host"] == Settings.model_fields["mcp_http_host"].default == "127.0.0.1"
    assert payload["port"] == 8000
    assert payload["tls"] is True
    assert payload["provider"] == "hash"
    assert payload["local_files_only"] is True
    assert payload["auto_index"] is False
    assert all(value is None for value in payload["environment"].values())


def test_dotenv_is_data_never_shell_source(normal_project):
    (normal_project / ".env").write_text(
        'KB_MCP_TLS_CERT_FILE="$(touch dotenv-executed).crt"\nKB_MCP_TLS_ENABLED=false\n'
    )
    process = run_launcher(normal_project, "run")
    assert process.returncode == 0, process.stderr
    assert "$(touch dotenv-executed).crt" in json.loads(process.stdout)["cert"]
    assert not (normal_project / "dotenv-executed").exists()


def test_missing_normal_tls_files_fail_without_generating_certificates(normal_project):
    (normal_project / ".env").write_text(
        "KB_MCP_TLS_ENABLED=true\n"
        "KB_MCP_TLS_CERT_FILE=operator/server.crt\n"
        "KB_MCP_TLS_KEY_FILE=operator/server.key\n"
    )
    process = run_launcher(normal_project, "run", environment={"TEST_VALIDATE_TLS": "1"})
    assert process.returncode != 0
    assert "TLS certificate was not found" in process.stderr
    assert str(normal_project / "operator/server.crt") in process.stderr
    assert not (normal_project / "generator-called").exists()
    assert not (normal_project / "certs").exists()
    assert not (normal_project / "operator").exists()
    assert not (normal_project / ".cache/kb/runtime/mcp-http.pid").exists()


@pytest.mark.parametrize("action,expected_code", [("status", 1), ("stop", 0), ("logs", 0)])
def test_management_does_not_load_python_or_tls_settings(normal_project, action, expected_code):
    # No usable runtime or configuration; these operations must still work.
    (normal_project / ".venv/bin/python").unlink()
    (normal_project / ".env").write_text('KB_MCP_TLS_ENABLED="$(touch dotenv-executed)"\n')
    fake_bin = normal_project / "fake-bin"
    fake_bin.mkdir()
    tail = fake_bin / "tail"
    tail.write_text('#!/usr/bin/env bash\nprintf "tail-called\\n"\n')
    tail.chmod(0o700)
    process = run_launcher(
        normal_project,
        action,
        environment={
            "KB_MCP_TLS_ENABLED": "not-a-boolean",
            "KB_ACCESS_ENABLED": "not-a-boolean",
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert process.returncode == expected_code, process.stderr
    assert "not initialized" not in process.stderr
    assert not (normal_project / "python-called").exists()
    assert not (normal_project / "generator-called").exists()
    assert not (normal_project / "dotenv-executed").exists()
    if action == "logs":
        assert "tail-called" in process.stdout
        assert (normal_project / ".cache/kb/runtime/mcp-http.log").is_file()
    else:
        assert not (normal_project / ".cache").exists()


def test_already_running_start_does_not_load_configuration(normal_project):
    runtime = normal_project / ".cache/kb/runtime"
    runtime.mkdir(parents=True)
    (runtime / "mcp-http.pid").write_text("12345\n")
    fake_bin = normal_project / "fake-bin"
    fake_bin.mkdir()
    ps = fake_bin / "ps"
    ps.write_text('#!/usr/bin/env bash\necho "python -m corporate_kb.mcp.http_server"\n')
    ps.chmod(0o700)
    (normal_project / ".venv/bin/python").unlink()
    process = run_launcher(
        normal_project,
        "start",
        environment={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
    )
    assert process.returncode == 0, process.stderr
    assert "already running (PID 12345)" in process.stdout
    assert (runtime / "mcp-http.pid").read_text() == "12345\n"
    assert not (normal_project / "python-called").exists()


@pytest.mark.parametrize(
    "host,scheme,expected,notice",
    [
        ("127.0.0.1", True, "https://127.0.0.1:9443", "loopback only"),
        ("::1", False, "http://[::1]:9443", "loopback only"),
        ("0.0.0.0", True, "https://127.0.0.1:9443", "all interfaces"),
        ("::", True, "https://[::1]:9443", "all interfaces"),
        ("mcp.example.test", True, "https://mcp.example.test:9443", "Bind:"),
    ],
)
def test_startup_summary_urls_are_valid_and_do_not_include_secrets(host, scheme, expected, notice):
    settings = Settings(
        _env_file=None,
        mcp_http_host=host,
        mcp_http_port=9443,
        mcp_http_path="/corporate-mcp",
        mcp_tls_enabled=scheme,
        admin_password="NEVER_DISPLAY_THIS_VALUE",
        mcp_http_bearer_token="NEVER_DISPLAY_THIS_VALUE",
    )
    summary = launcher.startup_summary(settings)
    assert f"Admin: {expected}/admin" in summary
    assert f"MCP:   {expected}/corporate-mcp" in summary
    assert notice in summary
    assert "NEVER_DISPLAY_THIS_VALUE" not in summary


def test_startup_summary_reads_the_same_dotenv_precedence(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "KB_MCP_HTTP_HOST=192.0.2.12\n"
        "KB_MCP_HTTP_PORT=9443\n"
        "KB_MCP_HTTP_PATH=/custom\n"
        "KB_MCP_TLS_ENABLED=false\n"
        "KB_ADMIN_PASSWORD=NEVER_DISPLAY_THIS_VALUE\n"
    )
    monkeypatch.setenv("KB_MCP_HTTP_PORT", "9001")
    launcher.main()
    output = capsys.readouterr()
    assert "Admin: http://192.0.2.12:9001/admin" in output.out
    assert "MCP:   http://192.0.2.12:9001/custom" in output.out
    assert "NEVER_DISPLAY_THIS_VALUE" not in output.out + output.err


def test_startup_summary_validation_errors_do_not_echo_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KB_MCP_HTTP_PORT", "NEVER_DISPLAY_THIS_VALUE")
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "invalid server configuration" in output.err
    assert "NEVER_DISPLAY_THIS_VALUE" not in output.out + output.err
