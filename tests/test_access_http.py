from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from corporate_kb.access.http import ADMIN_COOKIE, USER_COOKIE, AccessControl
from corporate_kb.access.store import AccessStore
from corporate_kb.access.tls import (
    CertificateH11Protocol,
    _CertificateScopeApp,
    certificate_from_scope,
    identity_from_der,
)
from corporate_kb.mcp.http_server import create_http_app, tls_uvicorn_config
from corporate_kb.service import KnowledgeService

PASSWORD = "correct-bootstrap-password-123"


@pytest.fixture
def pki(tmp_path):
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Access test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certificates = {}
    for name in ("server", "client", "other", "untrusted", "expired"):
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2))
            .not_valid_after(
                now - timedelta(days=1) if name == "expired" else now + timedelta(days=2)
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.ExtendedKeyUsage(
                    [
                        ExtendedKeyUsageOID.SERVER_AUTH
                        if name == "server"
                        else ExtendedKeyUsageOID.CLIENT_AUTH,
                    ]
                ),
                False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
            )
        )
        if name == "server":
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                        x509.DNSName("localhost"),
                    ]
                ),
                False,
            )
        cert = builder.sign(key if name == "untrusted" else ca_key, hashes.SHA256())
        (tmp_path / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (tmp_path / f"{name}.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        certificates[name] = identity_from_der(cert.public_bytes(serialization.Encoding.DER))
    return tmp_path, certificates


@pytest.fixture
def secured(settings_factory, pki):
    directory, certificates = pki
    settings = settings_factory(
        access_enabled=True,
        access_client_certificate_mode="trusted_ca",
        access_db_path=directory / "access.sqlite3",
        access_client_ca_file=directory / "ca.pem",
        access_bootstrap_admin_password=PASSWORD,
        mcp_tls_cert_file=directory / "server.pem",
        mcp_tls_key_file=directory / "server.key",
        # Neither legacy credential may grant access in the registry mode.
        mcp_http_bearer_token="legacy-token-that-must-never-bypass-registry",
        admin_password="legacy-password-that-must-not-work",
    )
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "test.md").write_text("# Payments\n\nPayment limits.")
    service = KnowledgeService(settings)
    service.build_index(force=True)
    app = create_http_app(service, settings)
    return app, settings, AccessStore(settings.access_db_path), certificates


def test_secure_settings_fail_closed(settings_factory, pki):
    directory, _ = pki
    with pytest.raises(ValueError, match="CLIENT_CA"):
        settings_factory(access_enabled=True, access_client_certificate_mode="trusted_ca")
    with pytest.raises(ValueError, match="TLS_ENABLED"):
        settings_factory(
            access_enabled=True, mcp_tls_enabled=False, access_client_ca_file=directory / "ca.pem"
        )
    settings = settings_factory(
        access_enabled=True,
        access_client_ca_file=directory / "ca.pem",
        access_db_path=directory / "uninitialized.sqlite3",
    )
    with pytest.raises(ValueError):
        AccessControl(settings)


def test_tls_settings_use_verified_transport(secured, pki):
    _, settings, _, certificates = secured
    config = tls_uvicorn_config(settings)
    assert config["ssl_cert_reqs"] == ssl.CERT_OPTIONAL
    assert config["http"] is CertificateH11Protocol
    assert config["proxy_headers"] is False
    assert certificate_from_scope({"scheme": "https", "headers": []}) is None
    assert (
        certificate_from_scope(
            {
                "scheme": "https",
                "corporate_kb.verified_client_certificate": certificates["expired"],
            }
        )
        == certificates["expired"]
    )
    settings.access_client_ca_file = pki[0] / "missing.pem"
    with pytest.raises(ValueError, match="CA"):
        tls_uvicorn_config(settings)


@pytest.mark.asyncio
async def test_every_operational_route_requires_personal_auth(secured):
    app, _, _, _ = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client,
    ):
        checked = 0
        for route in app.routes:
            path = getattr(route, "path", "")
            if not path.startswith(("/admin/api/", "/api/v1/")):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                response = await client.request(
                    method,
                    path,
                    headers={
                        "authorization": "Bearer legacy-token-that-must-never-bypass-registry",
                        "x-kb-admin-password": "legacy-password-that-must-not-work",
                    },
                )
                assert response.status_code == 401, (method, path, response.text)
                checked += 1
        assert checked > 30
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/access-admin")).status_code == 200
        assert (await client.get("/access/api/users")).status_code == 401
        assert (await client.post("/mcp", json={})).status_code == 401
        denied = await client.post(
            "/auth/token",
            json={},
            headers={
                "x-client-cert": "fake certificate",
                "x-ssl-client-verify": "SUCCESS",
            },
        )
        assert denied.status_code == 403
        assert (await client.get("/auth/status")).json()["certificate_present"] is False


@pytest.mark.asyncio
async def test_enrollment_registry_reuse_revocation_and_mcp(secured):
    app, settings, store, certificates = secured
    verified_app = _CertificateScopeApp(app, certificates["client"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=verified_app),
            base_url="https://testserver",
        ) as client,
    ):
        issued = await client.post("/auth/token", json={})
        assert issued.status_code == 200, issued.text
        assert issued.headers["cache-control"] == "no-store"
        token = issued.json()["access_token"]
        user_id = issued.json()["user"]["id"]
        client.headers["authorization"] = f"Bearer {token}"
        again = await client.post("/auth/token", json={})
        assert again.json()["access_token"] == token
        assert len(store.list_tokens()["items"]) == 1
        assert (await client.get("/api/v1/stats")).status_code == 200
        assert (await client.get("/admin/api/catalog")).status_code == 200
        assert (await client.get("/access/api/users")).status_code == 401
        async with (
            streamable_http_client(
                "https://testserver/mcp",
                http_client=client,
            ) as (read, write, session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "kb_search" in names
            previous_session = session_id()
        store.revoke_user(user_id, actor="admin", reason="Offboarding")
        assert (await client.get("/api/v1/stats")).status_code == 401
        assert (await client.get("/admin/api/catalog")).status_code == 401
        headers = {"mcp-session-id": previous_session} if previous_session else {}
        assert (await client.post("/mcp", json={}, headers=headers)).status_code == 401
        assert (await client.post("/auth/token", json={})).status_code == 403
        # Unknown token cannot bypass a blocked certificate either.
        client.headers["authorization"] = "Bearer unknown-token"
        assert (await client.post("/auth/token", json={})).status_code == 403
        reloaded = AccessStore(settings.access_db_path)
        assert reloaded.verify_user_token(token) is None


@pytest.mark.asyncio
async def test_mcp_rejects_plaintext_and_mismatched_certificate(secured):
    app, _, store, certificates = secured
    issued = store.enroll(certificates["client"])
    async with app.router.lifespan_context(app):
        for base_url, transport_app in (
            ("http://testserver", app),
            ("https://testserver", _CertificateScopeApp(app, certificates["other"])),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=transport_app),
                base_url=base_url,
                headers={
                    "authorization": f"Bearer {issued.token}",
                    "x-forwarded-proto": "https",
                    "x-client-cert": certificates["client"].certificate_pem.replace("\n", " "),
                },
            ) as client:
                assert (await client.post("/mcp", json={})).status_code == 401
                assert (await client.get("/api/v1/stats")).status_code == 401
                assert (await client.get("/admin/api/catalog")).status_code == 401


@pytest.mark.asyncio
async def test_browser_cookie_csrf_and_logout(secured):
    app, _, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url="https://testserver",
        ) as client,
    ):
        response = await client.post("/auth/browser-session", json={})
        assert response.status_code == 200
        assert "access_token" not in response.json()
        cookie = response.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict"))
        token = client.cookies[USER_COOKIE]
        assert (await client.get("/auth/status")).json()["authenticated"] is True
        assert (await client.get("/admin/api/catalog")).status_code == 200
        assert (await client.post("/admin/api/indexes", json={})).status_code == 401
        assert (
            await client.post(
                "/admin/api/indexes",
                json={},
                headers={
                    "origin": "https://evil.invalid",
                },
            )
        ).status_code == 401
        # Auth succeeds and normal request validation rejects missing index name.
        assert (
            await client.post(
                "/admin/api/indexes",
                json={},
                headers={
                    "origin": "https://testserver",
                },
            )
        ).status_code == 400
        response = await client.post(
            "/auth/logout",
            json={},
            headers={
                "origin": "https://testserver",
            },
        )
        assert response.status_code == 200
        assert store.verify_user_token(token) is None
        assert (await client.get("/admin/api/catalog")).status_code == 401


@pytest.mark.asyncio
async def test_separate_admin_sessions_csrf_and_access_management(secured):
    app, _, store, certificates = secured
    issued = store.enroll(certificates["client"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client,
    ):
        assert (
            await client.post(
                "/access/api/login",
                json={
                    "username": "admin",
                    "password": "incorrect",
                },
            )
        ).status_code == 401
        response = await client.post(
            "/access/api/login",
            json={
                "username": "admin",
                "password": PASSWORD,
            },
        )
        assert response.status_code == 200, response.text
        csrf = response.json()["csrf_token"]
        admin_id = response.json()["admin"]["id"]
        assert ADMIN_COOKIE in client.cookies
        assert (await client.get("/access/api/session")).json()["csrf_token"] == csrf
        assert (await client.get("/access/api/users")).json()["total"] == 1
        assert (await client.get("/admin/api/catalog")).status_code == 401
        # User bearer and old admin password cannot substitute for CSRF/admin roles.
        assert (
            await client.post(
                f"/access/api/users/{issued.user['id']}/revoke",
                json={
                    "reason": "Offboarding",
                },
            )
        ).status_code == 401
        client.headers["x-csrf-token"] = csrf
        client.headers["origin"] = "https://testserver"
        assert (
            await client.post(
                f"/access/api/users/{issued.user['id']}/revoke",
                json={
                    "reason": "Offboarding",
                },
            )
        ).status_code == 200
        assert store.verify_user_token(issued.token) is None
        assert (
            await client.post(f"/access/api/users/{issued.user['id']}/restore", json={})
        ).status_code == 200
        assert store.verify_user_token(issued.token) is None
        assert (
            await client.post(f"/access/api/admins/{admin_id}/deactivate", json={})
        ).status_code in (400, 403)
        added = await client.post(
            "/access/api/admins",
            json={
                "username": "second",
                "password": "another-secure-password-123",
            },
        )
        assert added.status_code == 201, added.text
        second = added.json()["admin"]
        assert (
            await client.post(f"/access/api/admins/{second['id']}/deactivate", json={})
        ).status_code == 200
        events = await client.get("/access/api/events")
        assert events.status_code == 200
        assert PASSWORD not in events.text and issued.token not in events.text
        assert (await client.post("/access/api/logout", json={})).status_code == 200
        assert (await client.get("/access/api/users")).status_code == 401


@pytest.mark.asyncio
async def test_admin_revoked_while_body_is_read_cannot_create_another_admin(secured):
    app, _, store, _ = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        ) as client,
    ):
        login = await client.post(
            "/access/api/login", json={"username": "admin", "password": PASSWORD}
        )
        assert login.status_code == 200
        token = client.cookies[ADMIN_COOKIE]

        async def delayed_body():
            # Route authorization has already succeeded; invalidate its session
            # before allowing the body to complete and the mutation to begin.
            store.logout_admin(token)
            yield b'{"username":"intruder","password":"another-secure-password-123"}'

        response = await client.post(
            "/access/api/admins",
            content=delayed_body(),
            headers={
                "content-type": "application/json",
                "origin": "https://testserver",
                "x-csrf-token": login.json()["csrf_token"],
            },
        )
        assert response.status_code == 403, response.text
        assert [admin["username"] for admin in store.list_admins()["items"]] == ["admin"]


@pytest.mark.asyncio
async def test_actual_tls_enrollment_rejects_untrusted_expired_and_missing_cert(secured, pki):
    app, settings, _, _ = secured
    directory, _ = pki
    configuration = tls_uvicorn_config(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(10)
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="critical", lifespan="on", **configuration)
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if task.done():
                await task
                pytest.fail("TLS server exited before startup")
            if time.monotonic() > deadline:
                pytest.fail("TLS server failed to start")
            await asyncio.sleep(0.01)
        base = f"https://127.0.0.1:{port}"
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        async with httpx.AsyncClient(verify=context, base_url=base, timeout=3) as client:
            assert (await client.post("/auth/token", json={})).status_code == 403
            assert (
                await client.post(
                    "/access/api/login",
                    json={
                        "username": "admin",
                        "password": PASSWORD,
                    },
                )
            ).status_code == 200
        for name in ("untrusted", "expired"):
            context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
            context.load_cert_chain(str(directory / f"{name}.pem"), str(directory / f"{name}.key"))
            async with httpx.AsyncClient(verify=context, base_url=base, timeout=3) as client:
                with pytest.raises(httpx.TransportError):
                    await client.post("/auth/token", json={})
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        context.load_cert_chain(str(directory / "client.pem"), str(directory / "client.key"))
        async with httpx.AsyncClient(verify=context, base_url=base, timeout=3) as client:
            enrolled = await client.post("/auth/token", json={})
            assert enrolled.status_code == 200, enrolled.text
            assert enrolled.json()["user"]["subject"] == "CN=client"
            token = enrolled.json()["access_token"]
        # The enrolled opaque bearer is sufficient for stock HTTPS MCP clients.
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        async with httpx.AsyncClient(verify=context, base_url=base, timeout=3) as client:
            assert (
                await client.get(
                    "/api/v1/stats",
                    headers={
                        "authorization": f"Bearer {token}",
                    },
                )
            ).status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        sock.close()
