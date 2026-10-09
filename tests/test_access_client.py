from __future__ import annotations

import json
import os
import ssl
import stat
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from corporate_kb.access import client

TOKEN = "example-new-access-token-that-is-not-a-real-secret"
OLD_TOKEN = "example-old-access-token-that-is-not-a-real-secret"
SERVER = "https://rag.example.test"


@pytest.fixture
def fake_enrollment(monkeypatch):
    calls: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "status": 200,
        "payload": {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "expires_at": int(time.time()) + 3600,
            "mcp_path": "/mcp",
            "user": {"id": "test-user"},
        },
    }

    class FakeContext:
        def load_cert_chain(self, **kwargs):
            calls.append({"cert": kwargs})

    def fake_context(**kwargs):
        calls.append({"context": kwargs})
        return FakeContext()

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append({"client": kwargs})

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            if "hook" in state:
                state["hook"]()
            if "error" in state:
                raise state["error"]
            if "content" in state:
                return httpx.Response(state["status"], content=state["content"])
            return httpx.Response(state["status"], json=state["payload"])

    monkeypatch.setattr(client.ssl, "create_default_context", fake_context)
    monkeypatch.setattr(client.httpx, "Client", FakeClient)
    return calls, state


def _connect(config: Path, **kwargs: Any) -> tuple[Path, int]:
    return client.connect_client(
        server_url=kwargs.pop("server_url", SERVER),
        cert=config.parent / "client.crt",
        key=config.parent / "client.key",
        config=config,
        **kwargs,
    )


def _write(path: Path, document: Any) -> bytes:
    raw = json.dumps(document).encode()
    path.write_bytes(raw)
    return raw


def test_connect_preserves_settings_and_reuses_only_same_origin_bearer(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    document = {
        "theme": "dark",
        "mcpServers": {
            "another": {"command": "unchanged"},
            "corporate-kb": {
                "httpUrl": SERVER + "/mcp",
                "timeout": 120000,
                "headers": {"authorization": f"Bearer {OLD_TOKEN}", "X-Team": "test"},
            },
        },
    }
    _write(config, document)
    path, expiry = _connect(config)
    result = json.loads(config.read_text())
    assert path == config
    assert expiry > time.time()
    assert result["theme"] == "dark"
    assert result["mcpServers"]["another"] == document["mcpServers"]["another"]
    entry = result["mcpServers"]["corporate-kb"]
    assert entry == {
        "httpUrl": SERVER + "/mcp",
        "timeout": 120000,
        "headers": {"Authorization": f"Bearer {TOKEN}", "X-Team": "test"},
    }
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    calls, _state = fake_enrollment
    request = next(call for call in calls if "url" in call)
    assert request == {
        "url": SERVER + "/auth/token",
        "json": {},
        "headers": {"Authorization": f"Bearer {OLD_TOKEN}"},
    }
    assert next(call["client"] for call in calls if "client" in call)["follow_redirects"] is False
    assert next(call["client"] for call in calls if "client" in call)["trust_env"] is False


def test_connect_new_config_uses_optional_ca_and_certificate(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    ca = tmp_path / "trusted-ca.pem"
    _connect(config, ca=ca)
    calls, _state = fake_enrollment
    assert {"context": {"cafile": str(ca)}} in calls
    certificate = next(call["cert"] for call in calls if "cert" in call)
    assert certificate["certfile"] == str(tmp_path / "client.crt")
    assert certificate["keyfile"] == str(tmp_path / "client.key")
    assert not next(call["headers"] for call in calls if "url" in call)


@pytest.mark.parametrize(
    "url",
    [
        "http://rag.example.test",
        "https://user:password@rag.example.test",
        SERVER + "/mcp",
        SERVER + "?token=secret",
        SERVER + "#fragment",
        SERVER + "\n",
        "https://",
        "https://rag.example.test:0",
        "https://rag.example.test:99999",
        "https://rag.example.test\\evil",
    ],
)
def test_invalid_server_url_never_sends_request(tmp_path, fake_enrollment, url):
    with pytest.raises(client.EnrollmentError):
        _connect(tmp_path / "settings.json", server_url=url)
    assert fake_enrollment[0] == []


@pytest.mark.parametrize(
    "content",
    [b"not json", b"[]", b'{"mcpServers": []}', b'{"a":1,"a":2}', b"\xff", b'{"a":NaN}'],
)
def test_malformed_config_is_never_rewritten(tmp_path, fake_enrollment, content):
    config = tmp_path / "settings.json"
    config.write_bytes(content)
    with pytest.raises(client.EnrollmentError):
        _connect(config)
    assert config.read_bytes() == content
    assert fake_enrollment[0] == []


def test_symlink_config_is_rejected(tmp_path, fake_enrollment):
    target = tmp_path / "real.json"
    target.write_text("{}")
    config = tmp_path / "settings.json"
    config.symlink_to(target)
    with pytest.raises(client.EnrollmentError, match="regular file"):
        _connect(config)
    assert target.read_text() == "{}"
    assert config.is_symlink()
    assert fake_enrollment[0] == []


def test_different_origin_refuses_without_replace_and_never_forwards_old_token(
    tmp_path, fake_enrollment
):
    config = tmp_path / "settings.json"
    original = _write(
        config,
        {
            "mcpServers": {
                "corporate-kb": {
                    "httpUrl": "https://other.example.test/mcp",
                    "headers": {"Authorization": f"Bearer {OLD_TOKEN}"},
                }
            }
        },
    )
    with pytest.raises(client.EnrollmentError, match="--replace"):
        _connect(config)
    assert config.read_bytes() == original
    assert fake_enrollment[0] == []
    _connect(config, replace=True)
    assert next(call["headers"] for call in fake_enrollment[0] if "url" in call) == {}


def test_competing_transport_requires_explicit_replace(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    _write(
        config,
        {
            "mcpServers": {
                "corporate-kb": {
                    "command": "old",
                    "args": [],
                    "cwd": "/old",
                    "env": {"OLD": "x"},
                    "timeout": 12,
                }
            }
        },
    )
    with pytest.raises(client.EnrollmentError, match="--replace"):
        _connect(config)
    _connect(config, replace=True)
    entry = json.loads(config.read_text())["mcpServers"]["corporate-kb"]
    assert not (client.INCOMPATIBLE_ENTRY_FIELDS & entry.keys())
    assert entry["timeout"] == 12


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 429, 500])
def test_http_failures_do_not_rewrite_or_leak_response(tmp_path, fake_enrollment, status):
    config = tmp_path / "settings.json"
    config.write_text("{}")
    fake_enrollment[1].update(status=status, content=f"server-sensitive-detail {TOKEN}".encode())
    with pytest.raises(client.EnrollmentError) as caught:
        _connect(config)
    assert TOKEN not in str(caught.value)
    assert "server-sensitive-detail" not in str(caught.value)
    assert config.read_text() == "{}"


def test_network_failure_and_cli_never_show_secrets(tmp_path, fake_enrollment):
    fake_enrollment[1]["error"] = httpx.ConnectError(f"secret exception {TOKEN}")
    result = CliRunner().invoke(
        client.app,
        [
            "connect",
            "--server-url",
            SERVER,
            "--cert",
            "c.pem",
            "--key",
            "k.pem",
            "--config",
            str(tmp_path / "settings.json"),
        ],
    )
    assert result.exit_code == 1
    assert "connection failed" in result.output
    assert TOKEN not in result.output
    assert "secret exception" not in result.output
    assert not (tmp_path / "settings.json").exists()


@pytest.mark.parametrize(
    "change",
    [
        {"access_token": "secret\r\nheader: value"},
        {"access_token": ""},
        {"token_type": "Basic"},
        {"expires_at": 1},
        {"expires_at": True},
        {"mcp_path": "https://evil.example.test/mcp"},
        {"mcp_path": "//evil/mcp"},
        {"mcp_path": "/../auth"},
        {"mcp_path": "/mcp?secret=x"},
    ],
)
def test_invalid_enrollment_response_does_not_write(tmp_path, fake_enrollment, change):
    fake_enrollment[1]["payload"].update(change)
    config = tmp_path / "settings.json"
    with pytest.raises(client.EnrollmentError, match="invalid enrollment"):
        _connect(config)
    assert not config.exists()


def test_concurrent_settings_edit_is_preserved(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    config.write_text("{}")
    fake_enrollment[1]["hook"] = lambda: config.write_text('{"new-user-setting": true}')
    with pytest.raises(client.EnrollmentError, match="changed during enrollment"):
        _connect(config)
    assert config.read_text() == '{"new-user-setting": true}'
    assert list(tmp_path.iterdir()) == [config]


def test_config_created_concurrently_is_preserved(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["hook"] = lambda: config.write_text('{"new-user-setting": true}')
    with pytest.raises(client.EnrollmentError, match="changed during enrollment"):
        _connect(config)
    assert config.read_text() == '{"new-user-setting": true}'


def test_tls_load_failure_is_sanitized(tmp_path, fake_enrollment, monkeypatch):
    def fail(**_kwargs):
        raise ssl.SSLError(f"secret {TOKEN}")

    monkeypatch.setattr(client.ssl, "create_default_context", fail)
    with pytest.raises(client.EnrollmentError) as caught:
        _connect(tmp_path / "settings.json")
    assert TOKEN not in str(caught.value)


def test_cli_success_prints_no_token(tmp_path, fake_enrollment):
    result = CliRunner().invoke(
        client.app,
        [
            "connect",
            "--server-url",
            SERVER,
            "--cert",
            "c.pem",
            "--key",
            "k.pem",
            "--config",
            str(tmp_path / "settings.json"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "saved to" in result.output
    assert TOKEN not in result.output
    assert OLD_TOKEN not in result.output


def test_stdio_setup_keeps_token_out_of_arguments(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    entry = json.loads(config.read_text())["mcpServers"]["corporate-kb"]
    assert entry["command"] == sys.executable
    assert entry["args"][:3] == ["-m", "corporate_kb.access.client", "proxy"]
    assert TOKEN not in " ".join(entry["args"])
    assert entry["env"][client.TOKEN_ENV] == TOKEN
    assert "httpUrl" not in entry


def test_stdio_proxy_renews_each_start_and_preserves_settings(
    tmp_path, fake_enrollment, monkeypatch
):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    document = json.loads(config.read_text())
    document["other"] = True
    _write(config, document)
    calls, state = fake_enrollment
    calls.clear()
    state["payload"]["access_token"] = OLD_TOKEN
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    monkeypatch.setenv(client.TOKEN_ENV, "outdated-inherited-token")
    client.proxy_client(
        server_url=SERVER, cert=tmp_path / "client.crt", key=tmp_path / "client.key", config=config
    )
    request = next(call for call in calls if "url" in call)
    assert request["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert proxies == [(SERVER + "/mcp", OLD_TOKEN, None)]
    result = json.loads(config.read_text())
    assert result["other"] is True
    assert result["mcpServers"]["corporate-kb"]["env"][client.TOKEN_ENV] == OLD_TOKEN


def test_stdio_revocation_does_not_open_proxy_or_rewrite(tmp_path, fake_enrollment, monkeypatch):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    raw = config.read_bytes()
    fake_enrollment[1]["status"] = 403
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    with pytest.raises(client.EnrollmentError, match="revoked"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
        )
    assert not proxies
    assert config.read_bytes() == raw


def test_stdio_valid_token_reuse_does_not_rewrite(tmp_path, fake_enrollment, monkeypatch):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    previous = config.stat()
    monkeypatch.setattr(client, "_run_proxy", lambda *_args: None)
    client.proxy_client(
        server_url=SERVER, cert=tmp_path / "client.crt", key=tmp_path / "client.key", config=config
    )
    assert config.stat().st_ino == previous.st_ino
    assert config.stat().st_mtime_ns == previous.st_mtime_ns


def test_stdio_reconnect_reads_newest_saved_token(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    fake_enrollment[0].clear()
    _connect(config, transport="stdio")
    request = next(call for call in fake_enrollment[0] if "url" in call)
    assert request["headers"] == {"Authorization": f"Bearer {TOKEN}"}


def test_stdio_same_token_concurrent_config_change_aborts_proxy(
    tmp_path, fake_enrollment, monkeypatch
):
    config = tmp_path / "settings.json"
    _connect(config, transport="stdio")
    fake_enrollment[1]["hook"] = lambda: config.write_text('{"changed": true}')
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    with pytest.raises(client.EnrollmentError, match="changed during enrollment"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
        )
    assert not proxies
    assert config.read_text() == '{"changed": true}'


def test_stdio_proxy_missing_entry_never_enrolls(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    with pytest.raises(client.EnrollmentError, match="Proxy entry is missing"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
        )
    assert fake_enrollment[0] == []


def test_proxy_errors_are_stderr_only_without_third_party_secrets(tmp_path, monkeypatch):
    def failed_proxy(**_kwargs):
        raise RuntimeError(f"secret detail {TOKEN}")

    monkeypatch.setattr(client, "proxy_client", failed_proxy)
    result = CliRunner().invoke(
        client.app,
        [
            "proxy",
            "--server-url",
            SERVER,
            "--cert",
            "client.crt",
            "--key",
            "client.key",
            "--config",
            str(tmp_path / "settings.json"),
        ],
    )
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "proxy failed" in result.stderr
    assert TOKEN not in result.output


def test_actual_proxy_construction_disables_redirects_and_keeps_ca(tmp_path, monkeypatch):
    import fastmcp
    import fastmcp.client.transports
    import fastmcp.server

    captured: dict[str, Any] = {}

    class FakeProxy:
        def run(self, **kwargs):
            captured["run"] = kwargs

    def fake_transport(url, **kwargs):
        captured.update(url=url, **kwargs)
        return "transport"

    def fake_proxy(target, **kwargs):
        captured["target"] = target
        return FakeProxy()

    monkeypatch.setattr(fastmcp.client.transports, "StreamableHttpTransport", fake_transport)
    monkeypatch.setattr(fastmcp, "Client", lambda value: value)
    monkeypatch.setattr(fastmcp.server, "create_proxy", fake_proxy)
    client._run_proxy(SERVER + "/mcp", TOKEN, None)
    assert captured["run"] == {"transport": "stdio", "show_banner": False}
    assert captured["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    factory = captured["httpx_client_factory"]
    instance = factory(follow_redirects=True)
    assert isinstance(instance, httpx.AsyncClient)
    assert instance.follow_redirects is False
    assert instance._trust_env is False


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode semantics")
def test_existing_world_readable_config_becomes_private(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    config.write_text("{}")
    config.chmod(0o644)
    _connect(config)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_skills_http_uses_advertised_path_and_preserves_knowledge(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    knowledge = {"httpUrl": SERVER + "/mcp", "headers": {"Authorization": f"Bearer {OLD_TOKEN}"}}
    _write(config, {"theme": "dark", "mcpServers": {"corporate-kb": knowledge}})
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/registry/custom-skills"

    _connect(config, service="skills")

    result = json.loads(config.read_text())
    assert result["theme"] == "dark"
    assert result["mcpServers"]["corporate-kb"] == knowledge
    assert result["mcpServers"]["corporate-skills"] == {
        "httpUrl": SERVER + "/registry/custom-skills",
        "headers": {"Authorization": f"Bearer {TOKEN}"},
    }


@pytest.mark.parametrize(
    "path",
    [
        None,
        "",
        "/mcp",
        "//evil/mcp",
        "https://evil/mcp",
        "/../mcp",
        "/./mcp",
        "/skills/../mcp",
        "/skills/./mcp",
        "/skills/..",
        "/skills/.",
        "/skills//mcp",
        "/skills/%2e%2e/mcp",
        "/skills/mcp?token=x",
        "/skills/mcp#fragment",
        "/skills\\mcp",
        "/skills/mcp\n",
        4,
    ],
)
def test_missing_disabled_or_unsafe_skills_endpoint_never_changes_config(
    tmp_path,
    fake_enrollment,
    path,
):
    config = tmp_path / "settings.json"
    original = _write(config, {"mcpServers": {"corporate-kb": {"httpUrl": SERVER + "/mcp"}}})
    if path is not None:
        fake_enrollment[1]["payload"]["skills_mcp_path"] = path
    with pytest.raises(client.EnrollmentError):
        _connect(config, service="skills")
    assert config.read_bytes() == original


@pytest.mark.parametrize("original_service,selected_service", [("kb", "skills"), ("skills", "kb")])
def test_http_cannot_silently_change_services_in_one_named_entry(
    tmp_path,
    fake_enrollment,
    original_service,
    selected_service,
):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/skills/mcp"
    _connect(config, name="chosen", service=original_service)
    original = config.read_bytes()

    with pytest.raises(client.EnrollmentError, match="another endpoint or service"):
        _connect(config, name="chosen", service=selected_service)
    assert config.read_bytes() == original

    _connect(config, name="chosen", service=selected_service, replace=True)
    path = "/skills/mcp" if selected_service == "skills" else "/mcp"
    assert json.loads(config.read_text())["mcpServers"]["chosen"]["httpUrl"] == SERVER + path


def test_skills_stdio_renews_against_selected_advertised_endpoint(
    tmp_path,
    fake_enrollment,
    monkeypatch,
):
    config = tmp_path / "settings.json"
    calls, state = fake_enrollment
    state["payload"]["skills_mcp_path"] = "/skills/mcp"
    _connect(config, transport="stdio", service="skills")
    entry = json.loads(config.read_text())["mcpServers"]["corporate-skills"]
    assert entry["args"][entry["args"].index("--service") + 1] == "skills"
    assert entry["args"][entry["args"].index("--name") + 1] == "corporate-skills"
    assert TOKEN not in " ".join(entry["args"])
    state["payload"].update(skills_mcp_path="/registry/new-path", access_token=OLD_TOKEN)
    calls.clear()
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))

    client.proxy_client(
        server_url=SERVER,
        cert=tmp_path / "client.crt",
        key=tmp_path / "client.key",
        config=config,
        service="skills",
    )

    assert next(call["headers"] for call in calls if "url" in call) == {
        "Authorization": f"Bearer {TOKEN}"
    }
    assert proxies == [(SERVER + "/registry/new-path", OLD_TOKEN, None)]
    entry = json.loads(config.read_text())["mcpServers"]["corporate-skills"]
    assert entry["env"][client.TOKEN_ENV] == OLD_TOKEN
    assert entry["args"][entry["args"].index("--service") + 1] == "skills"


@pytest.mark.parametrize("missing", [True, False])
def test_skills_stdio_disabled_at_renewal_preserves_config_and_does_not_proxy(
    tmp_path,
    fake_enrollment,
    monkeypatch,
    missing,
):
    config = tmp_path / "settings.json"
    state = fake_enrollment[1]
    state["payload"]["skills_mcp_path"] = "/skills/mcp"
    _connect(config, service="skills", transport="stdio")
    original = config.read_bytes()
    state["payload"]["access_token"] = OLD_TOKEN
    if missing:
        del state["payload"]["skills_mcp_path"]
    else:
        state["payload"]["skills_mcp_path"] = None
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))

    with pytest.raises(client.EnrollmentError, match="not advertised"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
            service="skills",
        )
    assert proxies == []
    assert config.read_bytes() == original


@pytest.mark.parametrize("original_service,selected_service", [("kb", "skills"), ("skills", "kb")])
def test_stdio_service_mismatch_cannot_reuse_another_service_entry(
    tmp_path,
    fake_enrollment,
    original_service,
    selected_service,
):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/skills/mcp"
    _connect(config, service=original_service, name="chosen", transport="stdio")
    original = config.read_bytes()
    fake_enrollment[0].clear()

    with pytest.raises(client.EnrollmentError, match="--replace"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
            service=selected_service,
            name="chosen",
        )
    with pytest.raises(client.EnrollmentError, match="--replace"):
        _connect(config, service=selected_service, name="chosen", transport="stdio")
    assert fake_enrollment[0] == []
    assert config.read_bytes() == original
    _connect(config, service=selected_service, name="chosen", transport="stdio", replace=True)
    entry = json.loads(config.read_text())["mcpServers"]["chosen"]
    assert entry["args"][entry["args"].index("--service") + 1] == selected_service


def test_legacy_stdio_without_service_stays_knowledge(tmp_path, fake_enrollment, monkeypatch):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/skills/mcp"
    _connect(config, transport="stdio")
    document = json.loads(config.read_text())
    args = document["mcpServers"]["corporate-kb"]["args"]
    start = args.index("--service")
    del args[start : start + 2]
    _write(config, document)
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))

    client.proxy_client(
        server_url=SERVER,
        cert=tmp_path / "client.crt",
        key=tmp_path / "client.key",
        config=config,
    )
    assert proxies == [(SERVER + "/mcp", TOKEN, None)]
    with pytest.raises(client.EnrollmentError, match="--replace"):
        client.proxy_client(
            server_url=SERVER,
            cert=tmp_path / "client.crt",
            key=tmp_path / "client.key",
            config=config,
            name="corporate-kb",
            service="skills",
        )


@pytest.mark.parametrize(
    "service,expected_name", [("kb", "corporate-kb"), ("skills", "corporate-skills")]
)
def test_cli_service_selects_default_entry_name(tmp_path, fake_enrollment, service, expected_name):
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/skills/mcp"
    config = tmp_path / "settings.json"
    result = CliRunner().invoke(
        client.app,
        [
            "connect",
            "--server-url",
            SERVER,
            "--cert",
            "c.pem",
            "--key",
            "k.pem",
            "--config",
            str(config),
            "--service",
            service,
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"MCP entry '{expected_name}'" in result.output
    assert set(json.loads(config.read_text())["mcpServers"]) == {expected_name}
    assert TOKEN not in result.output


def test_invalid_service_is_rejected_before_enrollment(tmp_path, fake_enrollment):
    config = tmp_path / "settings.json"
    with pytest.raises(client.EnrollmentError, match="kb or skills"):
        _connect(config, service="anything")
    assert fake_enrollment[0] == []
    assert not config.exists()


def test_proxy_cli_preserves_service_and_keeps_stdout_for_mcp(
    tmp_path,
    fake_enrollment,
    monkeypatch,
):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = "/registry/skills"
    _connect(config, service="skills", transport="stdio")
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    result = CliRunner().invoke(
        client.app,
        [
            "proxy",
            "--server-url",
            SERVER,
            "--cert",
            "c.pem",
            "--key",
            "k.pem",
            "--config",
            str(config),
            "--service",
            "skills",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert proxies == [(SERVER + "/registry/skills", TOKEN, None)]


@pytest.mark.parametrize(
    "path", ["/skills.v2/mcp", "https://elsewhere/mcp", "/../bad", 3, {}, None]
)
def test_knowledge_ignores_unselected_optional_skills_endpoint(
    tmp_path,
    fake_enrollment,
    monkeypatch,
    path,
):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = path
    _connect(config)
    assert (
        json.loads(config.read_text())["mcpServers"]["corporate-kb"]["httpUrl"] == SERVER + "/mcp"
    )

    _connect(config, transport="stdio", replace=True)
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    client.proxy_client(
        server_url=SERVER,
        cert=tmp_path / "client.crt",
        key=tmp_path / "client.key",
        config=config,
    )
    assert proxies == [(SERVER + "/mcp", TOKEN, None)]


@pytest.mark.parametrize(
    "path", ["/skills.v2/mcp", "/registry/.skills/mcp-v2.0", "/registry/skills.../mcp"]
)
def test_skills_accepts_safe_dotted_paths_for_http_and_stdio(
    tmp_path,
    fake_enrollment,
    monkeypatch,
    path,
):
    config = tmp_path / "settings.json"
    fake_enrollment[1]["payload"]["skills_mcp_path"] = path
    _connect(config, service="skills")
    assert (
        json.loads(config.read_text())["mcpServers"]["corporate-skills"]["httpUrl"] == SERVER + path
    )

    _connect(config, service="skills", transport="stdio", replace=True)
    proxies = []
    monkeypatch.setattr(client, "_run_proxy", lambda *args: proxies.append(args))
    client.proxy_client(
        server_url=SERVER,
        cert=tmp_path / "client.crt",
        key=tmp_path / "client.key",
        config=config,
        service="skills",
    )
    assert proxies == [(SERVER + path, TOKEN, None)]
