"""CN enrollment uses the same defaults locally and on a VM; strict CA mode is opt-in."""

from __future__ import annotations

import pytest
import test_access_http as http_fixtures

from corporate_kb.access import local_dev
from corporate_kb.config import Settings
from corporate_kb.mcp.http_server import tls_uvicorn_config

pki = http_fixtures.pki


def test_cn_enrollment_is_default_and_strict_ca_is_explicit(settings_factory):
    assert settings_factory().access_client_certificate_mode == "presented"
    with pytest.raises(ValueError, match="CLIENT_CA"):
        settings_factory(access_enabled=True, access_client_certificate_mode="trusted_ca")
    settings = settings_factory(access_enabled=True)
    assert settings.access_client_ca_file is None
    with pytest.raises(ValueError, match="TLS_ENABLED"):
        settings_factory(
            access_enabled=True,
            access_client_certificate_mode="presented",
            mcp_tls_enabled=False,
        )
    with pytest.raises(ValueError):
        settings_factory(access_client_certificate_mode="trust_everything_typo")


def test_presented_does_not_wrap_protocol_in_stdlib_ssl(settings_factory, pki):
    directory, _ = pki
    settings = settings_factory(
        access_enabled=True,
        access_client_certificate_mode="presented",
        access_client_ca_file=directory / "intentionally-nonexistent-ca.pem",
        mcp_tls_cert_file=directory / "server.pem",
        mcp_tls_key_file=directory / "server.key",
    )
    config = tls_uvicorn_config(settings)
    assert config is not None
    assert config["proxy_headers"] is False
    assert config["ws"] == "none"
    assert not any(name.startswith("ssl_") for name in config)
    settings.mcp_tls_cert_file = directory / "missing-server.pem"
    with pytest.raises(ValueError, match="TLS certificate was not found"):
        tls_uvicorn_config(settings)


def test_local_policy_is_explicit_and_does_not_change_production(tmp_path, monkeypatch):
    monkeypatch.setenv("KB_ACCESS_CLIENT_CERTIFICATE_MODE", "trusted_ca")
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "isolated-state"
    settings = local_dev.prepare_local_environment(project, state)
    assert settings.access_client_certificate_mode == "presented"
    assert Settings(_env_file=None).access_client_certificate_mode == "trusted_ca"
    strict = local_dev.prepare_local_environment(
        project, state, client_certificate_mode="trusted_ca"
    )
    assert strict.access_client_certificate_mode == "trusted_ca"
    assert strict.access_db_path == settings.access_db_path
    assert strict.mcp_tls_cert_file == settings.mcp_tls_cert_file
