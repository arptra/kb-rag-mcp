"""The same CN/token workflow with development and separately configured VM TLS files."""

from __future__ import annotations

import ssl
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import test_access_http as http_fixtures
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_access_mcp_config import _exported_token
from test_access_proxy import _https_server

from corporate_kb.access.http import MCP_CONFIG_COOKIE
from corporate_kb.access.local_dev import prepare_local_environment
from corporate_kb.access.store import AccessStore
from corporate_kb.config import Settings
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

pki = http_fixtures.pki
secured = http_fixtures.secured
ENDPOINT = "/auth/mcp-config"


def _client(directory, label, names):
    """Each leaf has an independent signing key and issuer, unrelated to server CA."""
    now = datetime.now(UTC)
    client_key = ec.generate_private_key(ec.SECP256R1())
    issuer_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Client test")]
        + [x509.NameAttribute(NameOID.COMMON_NAME, value) for value in names]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label + " CA")]))
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(issuer_key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{label}.pem", directory / f"{label}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _context(authority, pair=None):
    context = ssl.create_default_context(cafile=str(authority))
    if pair:
        context.load_cert_chain(*map(str, pair))
    return context


@pytest.fixture(params=["localhost", "vm"])
def cn_server(request, secured, tmp_path, pki):
    if request.param == "localhost":
        project = tmp_path / "local-project"
        project.mkdir()
        settings = prepare_local_environment(project, tmp_path / "isolated-local")
        authority = settings.access_client_ca_file
    else:
        _, previous, _, _ = secured
        # An ordinary VM deployment supplies server cert/key and no client CA or mode.
        settings = Settings(
            _env_file=None,
            **previous.model_dump(
                exclude={"access_client_certificate_mode", "access_client_ca_file"}
            ),
        )
        authority = pki[0] / "ca.pem"
        assert settings.access_client_ca_file is None
    assert settings.access_client_certificate_mode == "presented"
    service = KnowledgeService(settings)
    service.build_index(force=True)
    return (
        create_http_app(service, settings),
        settings,
        AccessStore(settings.access_db_path),
        authority,
    )


@pytest.mark.asyncio
async def test_same_cn_reissued_key_reuses_account_and_token_in_both_launch_modes(
    cn_server, tmp_path
):
    app, settings, store, authority = cn_server
    first = _client(tmp_path, "first", ["alice"])
    reissued = _client(tmp_path, "reissued", ["alice"])
    other = _client(tmp_path, "other", ["bob"])
    async with _https_server(app, settings) as origin:
        async with httpx.AsyncClient(verify=_context(authority, first), base_url=origin) as browser:
            status = (await browser.get("/auth/status")).json()
            assert status["certificate_present"] is True
            assert status["certificate_mode"] == "presented"
            response = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            token = _exported_token(response, origin)
            original = response.json()["user"]
            assert original["common_name"] == "alice"
            dashboard = await browser.post(
                "/auth/browser-session", json={}, headers={"origin": origin}
            )
            assert dashboard.status_code == 200
            assert (await browser.get("/auth/status")).json()["user"]["common_name"] == "alice"

        async with httpx.AsyncClient(
            verify=_context(authority, reissued), base_url=origin
        ) as browser:
            response = await browser.post(
                ENDPOINT,
                json={},
                headers={"origin": origin, "cookie": f"{MCP_CONFIG_COOKIE}={token}"},
            )
            assert _exported_token(response, origin) == token
            assert response.json()["user"]["id"] == original["id"]
            assert response.json()["user"]["fingerprint"] != original["fingerprint"]
            assert store.list_users()["total"] == 1
            assert (
                await browser.get("/api/v1/stats", headers={"authorization": f"Bearer {token}"})
            ).status_code == 200

        # An old cert is not invalidated by updating the account's last certificate metadata.
        for pair in (first, None):
            async with httpx.AsyncClient(
                verify=_context(authority, pair), base_url=origin
            ) as client:
                headers = {"authorization": f"Bearer {token}"}
                assert (await client.get("/api/v1/stats", headers=headers)).status_code == 200
                assert (await client.get("/access/api/users", headers=headers)).status_code == 401

        async with httpx.AsyncClient(verify=_context(authority, other), base_url=origin) as client:
            assert (
                await client.get(
                    "/api/v1/stats",
                    headers={
                        "authorization": f"Bearer {token}",
                        "x-client-cn": "alice",
                    },
                )
            ).status_code == 401

        store.revoke_user(original["id"], actor="test-admin")
        async with httpx.AsyncClient(
            verify=_context(authority, reissued), base_url=origin
        ) as browser:
            response = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            assert response.status_code == 403
            assert (
                await browser.get("/api/v1/stats", headers={"authorization": f"Bearer {token}"})
            ).status_code == 401
        assert store.list_users()["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [[], ["alice", "bob"]])
async def test_missing_or_ambiguous_cn_never_receives_token(cn_server, tmp_path, names):
    app, settings, store, authority = cn_server
    pair = _client(tmp_path, "invalid-cn", names)
    async with (
        _https_server(app, settings) as origin,
        httpx.AsyncClient(verify=_context(authority, pair), base_url=origin) as browser,
    ):
        assert (await browser.get("/connect")).status_code == 200
        assert (await browser.get("/auth/status")).json()["certificate_present"] is False
        assert (
            await browser.post(ENDPOINT, json={}, headers={"origin": origin})
        ).status_code == 403
    assert store.list_users()["total"] == 0
    assert store.list_tokens()["total"] == 0
