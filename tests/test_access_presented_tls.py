"""Live TLS contracts for explicitly accepting proof of an untrusted client identity.

These clients use temporary PEM keys, not a browser or a corporate key store. The
tests establish transport/enrollment behavior without claiming browser integration.
"""

from __future__ import annotations

import ipaddress
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import test_access_http as http_fixtures
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_access_mcp_config import _exported_token
from test_access_proxy import _https_server

from corporate_kb.access.http import MCP_CONFIG_COOKIE
from corporate_kb.access.store import AccessStore
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

pki = http_fixtures.pki
secured = http_fixtures.secured
ENDPOINT = "/auth/mcp-config"


def _write_pair(directory: Path, name: str, certificate, key) -> tuple[Path, Path]:
    certificate_path = directory / f"{name}.pem"
    key_path = directory / f"{name}.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def _leaf(directory: Path, name: str, *, authority=None, server=False, age="valid"):
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    issuer, signer = (subject, key) if authority is None else (authority[0].subject, authority[1])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=1) if age == "future" else now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1) if age == "expired" else now + timedelta(days=3))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH if server else ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), False
        )
    )
    if server:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            False,
        )
    return _write_pair(directory, name, builder.sign(signer, hashes.SHA256()), key)


@pytest.fixture
def extra_pki(pki):
    """An independent CA plus genuinely self-signed and invalid client leaves."""
    directory, _ = pki
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unrelated test CA")])
    authority = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256())
    )
    authority_path = directory / "unrelated-ca.pem"
    authority_path.write_bytes(authority.public_bytes(serialization.Encoding.PEM))
    issuer = (authority, key)
    return {
        "ca": authority_path,
        "unrelated": _leaf(directory, "unrelated-client", authority=issuer),
        "selfsigned": _leaf(directory, "selfsigned-client"),
        "expired": _leaf(directory, "expired-external-client", authority=issuer, age="expired"),
        "future": _leaf(directory, "future-external-client", authority=issuer, age="future"),
        "wrong_eku": _leaf(directory, "wrong-purpose-client", authority=issuer, server=True),
        "server_a": _leaf(directory, "rotation-server-a", authority=issuer, server=True),
        "server_b": _leaf(directory, "rotation-server-b", authority=issuer, server=True),
    }


def _app(settings):
    service = KnowledgeService(settings)
    service.load_read_index()
    return create_http_app(service, settings)


@pytest.fixture
def presented(secured):
    _, original, store, certificates = secured
    settings = original.model_copy(
        update={"access_client_certificate_mode": "presented", "access_client_ca_file": None}
    )
    return _app(settings), settings, store, certificates


def _context(authority: Path, pair: tuple[Path, Path] | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(authority))
    if pair is not None:
        context.load_cert_chain(str(pair[0]), str(pair[1]))
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["unrelated", "selfsigned"])
async def test_presented_external_identity_can_export_and_use_mcp(
    presented, pki, extra_pki, identity
):
    app, settings, store, _ = presented
    directory, _ = pki
    pair = extra_pki[identity]
    certificate = x509.load_pem_x509_certificate(pair[0].read_bytes())
    async with _https_server(app, settings) as origin:
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem", pair), base_url=origin, timeout=5
        ) as browser:
            assert (await browser.get("/connect")).status_code == 200
            status = await browser.get("/auth/status")
            assert status.status_code == 200
            assert status.json()["certificate_present"] is True
            response = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            token = _exported_token(response, origin=origin)
            assert response.json()["user"]["fingerprint"] == certificate.fingerprint(
                hashes.SHA256()
            ).hex()
            repeated = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            assert _exported_token(repeated, origin=origin) == token
            assert MCP_CONFIG_COOKIE in browser.cookies

        # A stock MCP client subsequently needs HTTPS server trust and the bearer,
        # not access to the personal client key used during enrollment.
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem"),
            base_url=origin,
            timeout=5,
            headers={"authorization": f"Bearer {token}"},
        ) as client:
            assert (await client.get("/api/v1/stats")).status_code == 200
            async with (
                streamable_http_client(origin + "/mcp", http_client=client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert "kb_search" in {tool.name for tool in (await session.list_tools()).tools}
                stats = await session.call_tool("kb_stats", {})
                assert not stats.isError
        assert store.list_users()["total"] == 1
        assert store.list_tokens()["total"] == 1


@pytest.mark.asyncio
async def test_presented_no_certificate_and_spoofed_headers_cannot_enroll(presented, pki):
    app, settings, store, certificates = presented
    directory, _ = pki
    async with _https_server(app, settings) as origin:
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem"), base_url=origin, timeout=5
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/connect")).status_code == 200
            assert (await client.get("/auth/status")).json()["certificate_present"] is False
            for headers in (
                {"origin": origin},
                {
                    "origin": origin,
                    "x-client-cert": (
                        certificates["client"].certificate_pem.replace("\n", " ").strip()
                    ),
                    "x-ssl-client-verify": "SUCCESS",
                    "x-forwarded-client-cert": certificates["client"].fingerprint,
                    "x-forwarded-proto": "https",
                },
            ):
                response = await client.post(ENDPOINT, json={}, headers=headers)
                assert response.status_code == 403
                assert "config" not in response.json()
        assert store.list_users()["total"] == 0
        assert store.list_tokens()["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["expired", "future", "wrong_eku"])
async def test_presented_dates_and_purpose_do_not_block_cn_enrollment(
    presented, pki, extra_pki, identity
):
    app, settings, store, _ = presented
    directory, _ = pki
    async with _https_server(app, settings) as origin:
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem", extra_pki[identity]),
            base_url=origin,
            timeout=5,
        ) as client:
            assert (await client.get("/connect")).status_code == 200
            assert (await client.get("/auth/status")).json()["certificate_present"] is True
            response = await client.post(ENDPOINT, json={}, headers={"origin": origin})
            assert response.status_code == 200
            token = _exported_token(response, origin)
            assert store.verify_user_token(token) is not None
        assert store.list_tokens()["total"] == 1


def test_certificate_without_matching_private_key_cannot_configure_tls_client(pki, extra_pki):
    """The public PEM is insufficient; stdlib TLS rejects missing/mismatched keys.

    This is client setup validation, not a forged CertificateVerify handshake test.
    """
    directory, _ = pki
    certificate, _ = extra_pki["unrelated"]
    context = _context(directory / "ca.pem")
    with pytest.raises(ssl.SSLError):
        context.load_cert_chain(str(certificate), str(directory / "client.key"))
    with pytest.raises(ssl.SSLError):
        context.load_cert_chain(str(certificate))


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["unrelated", "selfsigned"])
async def test_trusted_ca_mode_still_rejects_external_identities(secured, pki, extra_pki, identity):
    _, original, store, _ = secured
    settings = original.model_copy(update={"access_client_certificate_mode": "trusted_ca"})
    directory, _ = pki
    async with _https_server(_app(settings), settings) as origin:
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem", extra_pki[identity]),
            base_url=origin,
            timeout=5,
        ) as client:
            with pytest.raises(httpx.TransportError):
                await client.post(ENDPOINT, json={}, headers={"origin": origin})
        assert store.list_tokens()["total"] == 0


@pytest.mark.asyncio
async def test_presented_other_certificate_gets_new_identity_and_revocation_is_sticky(
    presented, pki, extra_pki
):
    app, settings, store, _ = presented
    directory, _ = pki
    async with _https_server(app, settings) as origin:
        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem", extra_pki["unrelated"]),
            base_url=origin,
            timeout=5,
        ) as first_client:
            response = await first_client.post(ENDPOINT, json={}, headers={"origin": origin})
            first = _exported_token(response, origin=origin)
            first_user = response.json()["user"]

        async with httpx.AsyncClient(
            verify=_context(directory / "ca.pem", extra_pki["selfsigned"]),
            base_url=origin,
            timeout=5,
            headers={"origin": origin},
        ) as second_client:
            # A bearer presented alongside a different proven client identity is invalid.
            response = await second_client.get(
                "/api/v1/stats", headers={"authorization": f"Bearer {first}"}
            )
            assert response.status_code == 401
            response = await second_client.post(
                ENDPOINT, json={}, headers={"cookie": f"{MCP_CONFIG_COOKIE}={first}"}
            )
            second = _exported_token(response, origin=origin)
            second_user = response.json()["user"]
            assert second != first
            assert second_user["id"] != first_user["id"]
            assert second_user["fingerprint"] != first_user["fingerprint"]
            assert store.verify_user_token(first) is not None
            store.revoke_user(second_user["id"], actor="test-admin")
            assert AccessStore(settings.access_db_path).verify_user_token(second) is None
            for cookie in (second, "unknown-token"):
                response = await second_client.post(
                    ENDPOINT, json={}, headers={"cookie": f"{MCP_CONFIG_COOKIE}={cookie}"}
                )
                assert response.status_code == 403
            response = await second_client.get(
                "/api/v1/stats", headers={"authorization": f"Bearer {second}"}
            )
            assert response.status_code == 401
        assert store.list_users()["total"] == 2
        assert store.list_tokens()["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trusted_ca", "presented"])
async def test_server_certificate_rotation_preserves_client_identity_and_bearer(
    secured, pki, extra_pki, mode
):
    _, original, store, _ = secured
    directory, _ = pki
    first_certificate, first_key = extra_pki["server_a"]
    settings = original.model_copy(
        update={
            "access_client_certificate_mode": mode,
            "mcp_tls_cert_file": first_certificate,
            "mcp_tls_key_file": first_key,
        }
    )
    client_pair = (directory / "client.pem", directory / "client.key")
    async with (
        _https_server(_app(settings), settings) as origin,
        httpx.AsyncClient(
            verify=_context(extra_pki["ca"], client_pair), base_url=origin, timeout=5
        ) as client,
    ):
        response = await client.post(ENDPOINT, json={}, headers={"origin": origin})
        token = _exported_token(response, origin=origin)
        user = response.json()["user"]

    second_certificate, second_key = extra_pki["server_b"]
    assert second_certificate.read_bytes() != first_certificate.read_bytes()
    assert second_key.read_bytes() != first_key.read_bytes()
    rotated = settings.model_copy(
        update={"mcp_tls_cert_file": second_certificate, "mcp_tls_key_file": second_key}
    )
    assert rotated.access_db_path == settings.access_db_path
    async with _https_server(_app(rotated), rotated) as origin:
        async with httpx.AsyncClient(
            verify=_context(extra_pki["ca"]), base_url=origin, timeout=5
        ) as token_client:
            response = await token_client.get(
                "/api/v1/stats", headers={"authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
        async with httpx.AsyncClient(
            verify=_context(extra_pki["ca"], client_pair), base_url=origin, timeout=5
        ) as enrolled:
            response = await enrolled.post(
                ENDPOINT,
                json={},
                headers={"origin": origin, "cookie": f"{MCP_CONFIG_COOKIE}={token}"},
            )
            assert _exported_token(response, origin=origin) == token
            assert response.json()["user"]["id"] == user["id"]
            assert response.json()["user"]["fingerprint"] == user["fingerprint"]
        assert store.list_users()["total"] == 1
        assert store.list_tokens()["total"] == 1
