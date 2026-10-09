"""Browser certificate-based MCP configuration export without a local helper."""

from __future__ import annotations

import sqlite3
import ssl
import time

import httpx
import pytest
import test_access_http as http_fixtures
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_access_proxy import _https_server

from corporate_kb.access.http import ADMIN_COOKIE, USER_COOKIE
from corporate_kb.access.store import AccessStore
from corporate_kb.access.tls import _CertificateScopeApp
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

pki = http_fixtures.pki
secured = http_fixtures.secured
CONFIG_COOKIE = "__Host-kb-mcp-config"
ORIGIN = "https://testserver"
ENDPOINT = "/auth/mcp-config"


def _exported_token(
    response: httpx.Response,
    origin: str = ORIGIN,
    path: str = "/mcp",
    *,
    skills_path: str | None = "/skills/mcp",
) -> str:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"config", "expires_at", "user"}
    assert isinstance(payload["expires_at"], int)
    assert payload["expires_at"] > time.time()
    servers = payload["config"]["mcpServers"]
    assert set(servers) == (
        {"corporate-kb", "corporate-skills"} if skills_path is not None else {"corporate-kb"}
    )
    entry = servers["corporate-kb"]
    assert entry["httpUrl"] == origin + path
    assert set(entry) == {"httpUrl", "headers"}
    assert set(entry["headers"]) == {"Authorization"}
    scheme, token = entry["headers"]["Authorization"].split(" ", 1)
    assert scheme == "Bearer"
    assert token
    if skills_path is not None:
        assert servers["corporate-skills"] == {
            "httpUrl": origin + skills_path,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    return token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("base_url", "verified", "headers"),
    [
        (ORIGIN, False, {"origin": ORIGIN}),
        (ORIGIN, True, {}),
        (ORIGIN, True, {"origin": "https://other.example.test"}),
        (ORIGIN, True, {"origin": "null"}),
        (ORIGIN, True, {"origin": ORIGIN + "/"}),
        ("http://testserver", True, {"origin": "http://testserver"}),
        (
            "http://testserver",
            True,
            {"origin": ORIGIN, "x-forwarded-proto": "https"},
        ),
        (
            ORIGIN,
            False,
            {
                "origin": ORIGIN,
                "x-client-cert": "fake certificate",
                "x-ssl-client-verify": "SUCCESS",
                "x-forwarded-client-cert": "fake certificate",
            },
        ),
    ],
)
async def test_config_export_requires_https_verified_certificate_and_same_origin(
    secured, base_url, verified, headers
):
    app, _, store, certificates = secured
    transport_app = _CertificateScopeApp(app, certificates["client"]) if verified else app
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=transport_app), base_url=base_url
        ) as client,
    ):
        response = await client.post(ENDPOINT, headers=headers, json={})
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"
        assert "config" not in response.json()
        assert CONFIG_COOKIE not in response.cookies
        assert store.list_tokens()["items"] == []


@pytest.mark.asyncio
async def test_user_bearer_and_admin_cookie_cannot_replace_personal_certificate(secured):
    app, _, store, certificates = secured
    issued = store.enroll(certificates["client"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"origin": ORIGIN}
        ) as client,
    ):
        bearer = await client.post(
            ENDPOINT, json={}, headers={"Authorization": f"Bearer {issued.token}"}
        )
        assert bearer.status_code == 403
        login = await client.post(
            "/access/api/login",
            json={"username": "admin", "password": http_fixtures.PASSWORD},
        )
        assert login.status_code == 200
        assert ADMIN_COOKIE in client.cookies
        denied = await client.post(ENDPOINT, json={})
        assert denied.status_code == 403
        assert len(store.list_tokens()["items"]) == 1


@pytest.mark.asyncio
async def test_export_uses_separate_secure_cookie_and_survives_browser_logout(secured):
    app, settings, store, certificates = secured
    verified = _CertificateScopeApp(app, certificates["client"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=verified),
            base_url=ORIGIN,
            headers={"origin": ORIGIN},
        ) as client,
    ):
        browser = await client.post("/auth/browser-session", json={})
        assert browser.status_code == 200
        browser_token = client.cookies[USER_COOKIE]

        response = await client.post(
            ENDPOINT,
            json={},
            # Neither dashboard cookie nor supplied Bearer is the export credential.
            headers={"Authorization": f"Bearer {browser_token}"},
        )
        token = _exported_token(response)
        assert token != browser_token
        assert browser_token not in response.text
        assert client.cookies[USER_COOKIE] == browser_token
        assert client.cookies[CONFIG_COOKIE] == token
        assert USER_COOKIE not in response.cookies
        cookie = response.headers["set-cookie"].lower()
        assert all(flag in cookie for flag in ("httponly", "secure", "samesite=strict", "path=/"))
        assert "domain=" not in cookie

        repeated = await client.post(ENDPOINT, json={})
        assert _exported_token(repeated) == token
        assert len(store.list_tokens()["items"]) == 2
        assert AccessStore(settings.access_db_path).verify_user_token(token) is not None
        assert token.encode() not in settings.access_db_path.read_bytes()

        logged_out = await client.post("/auth/logout", json={})
        assert logged_out.status_code == 200
        assert USER_COOKIE not in client.cookies
        assert client.cookies[CONFIG_COOKIE] == token
        assert store.verify_user_token(browser_token) is None
        assert store.verify_user_token(token) is not None
        assert (await client.get("/auth/status")).json()["authenticated"] is False
        assert (await client.get("/admin/api/catalog")).status_code == 401
        assert _exported_token(await client.post(ENDPOINT, json={})) == token


@pytest.mark.asyncio
async def test_exported_config_is_usable_for_mcp_and_api_without_client_certificate(secured):
    app, _, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
        ) as browser,
    ):
        response = await browser.post(ENDPOINT, headers={"origin": ORIGIN}, json={})
        token = _exported_token(response)
        headers = response.json()["config"]["mcpServers"]["corporate-kb"]["headers"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers=headers
        ) as mcp_client:
            stats = await mcp_client.get("/api/v1/stats")
            assert stats.status_code == 200
            assert stats.json()["document_count"] == 1
            assert (await mcp_client.get("/access/api/users")).status_code == 401
            async with (
                streamable_http_client(ORIGIN + "/mcp", http_client=mcp_client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert "kb_search" in {tool.name for tool in (await session.list_tools()).tools}
                result = await session.call_tool("kb_stats", {})
                assert not result.isError
                assert result.structuredContent["document_count"] == 1
            skills_entry = response.json()["config"]["mcpServers"]["corporate-skills"]
            assert skills_entry["headers"] == headers
            async with (
                streamable_http_client(skills_entry["httpUrl"], http_client=mcp_client)
                as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert names == {
                    "skills_search", "skills_get_release", "skills_check_updates",
                    "skills_prepare_install",
                }
                assert "kb_search" not in names
        assert store.verify_user_token(token) is not None


@pytest.mark.asyncio
async def test_export_url_uses_configured_path_and_ignores_forwarding_headers(secured):
    app, settings, _, certificates = secured
    settings.mcp_http_path = "/custom/mcp"
    settings.skills_mcp_path = "/custom-skills/protocol"
    base = "https://testserver:8443"
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=base,
        ) as client,
    ):
        response = await client.post(
            ENDPOINT,
            json={},
            headers={
                "origin": base,
                "x-forwarded-host": "attacker.example.test",
                "x-forwarded-proto": "http",
                "forwarded": 'host="attacker.example.test";proto=http',
            },
        )
        _exported_token(
            response, origin=base, path="/custom/mcp", skills_path="/custom-skills/protocol"
        )
        assert "attacker.example.test" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_enrollment_and_export_advertise_only_enabled_configured_skills_server(
    secured, enabled
):
    _, settings, store, certificates = secured
    settings.skills_registry_enabled = enabled
    settings.skills_mcp_path = "/separate-skills/protocol"
    app = create_http_app(KnowledgeService(settings), settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
            headers={"origin": ORIGIN},
        ) as browser,
    ):
        enrollment = await browser.post("/auth/token", json={})
        assert enrollment.status_code == 200
        assert enrollment.json()["mcp_path"] == "/mcp"
        if enabled:
            assert enrollment.json()["skills_mcp_path"] == "/separate-skills/protocol"
        else:
            assert "skills_mcp_path" not in enrollment.json()
        exported = await browser.post(ENDPOINT, json={})
        token = _exported_token(
            exported, skills_path="/separate-skills/protocol" if enabled else None
        )
        assert store.verify_user_token(token) is not None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
            headers={"Authorization": f"Bearer {token}"},
        ) as mcp_client:
            if enabled:
                async with (
                    streamable_http_client(
                        ORIGIN + "/separate-skills/protocol", http_client=mcp_client
                    ) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    assert "skills_search" in {
                        tool.name for tool in (await session.list_tools()).tools
                    }
            else:
                absent = await mcp_client.post("/separate-skills/protocol", json={})
                assert absent.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_export_requires_post(secured, method):
    app, _, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
        ) as client,
    ):
        response = await client.request(method, ENDPOINT, headers={"origin": ORIGIN})
        assert response.status_code == 405
        assert store.list_tokens()["items"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "content_type"),
    [("{}", "text/plain"), ("[]", "application/json"), ("invalid", "application/json")],
)
async def test_export_rejects_invalid_body_without_issuing_token(secured, content, content_type):
    app, _, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
        ) as client,
    ):
        response = await client.post(
            ENDPOINT, content=content, headers={"origin": ORIGIN, "content-type": content_type}
        )
        assert response.status_code == 400
        assert store.list_tokens()["items"] == []


@pytest.mark.asyncio
async def test_config_cookie_is_reused_only_for_same_certificate(secured):
    app, _, store, certificates = secured
    first = store.enroll(certificates["client"])
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["other"])),
            base_url=ORIGIN,
            cookies={CONFIG_COOKIE: first.token},
        ) as client,
    ):
        response = await client.post(ENDPOINT, json={}, headers={"origin": ORIGIN})
        token = _exported_token(response)
        assert token != first.token
        assert response.json()["user"]["id"] != first.user["id"]
        assert response.json()["user"]["subject"] == "CN=other"
        assert store.verify_user_token(token)["subject"] == "CN=other"
        assert store.verify_user_token(first.token)["subject"] == "CN=client"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidation", ["expiry", "token_revocation"])
async def test_active_certificate_can_replace_expired_or_revoked_export_token(
    secured, invalidation
):
    app, settings, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
            headers={"origin": ORIGIN},
        ) as client,
    ):
        first = _exported_token(await client.post(ENDPOINT, json={}))
        if invalidation == "expiry":
            with sqlite3.connect(settings.access_db_path) as database:
                database.execute("UPDATE user_tokens SET expires_at = ?", (int(time.time()) - 1,))
        else:
            token_id = store.list_tokens()["items"][0]["id"]
            store.revoke_token(token_id, actor="test-admin")
        assert store.verify_user_token(first) is None
        second = _exported_token(await client.post(ENDPOINT, json={}))
        assert second != first
        assert store.verify_user_token(second) is not None
        assert len(store.list_tokens()["items"]) == 2


@pytest.mark.asyncio
async def test_revoked_personal_certificate_cannot_export_even_with_unknown_cookie(secured):
    app, _, store, certificates = secured
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_CertificateScopeApp(app, certificates["client"])),
            base_url=ORIGIN,
            headers={"origin": ORIGIN},
        ) as client,
    ):
        first = _exported_token(await client.post(ENDPOINT, json={}))
        user = store.verify_user_token(first)
        store.revoke_user(user["id"], actor="test-admin")
        for cookie in (first, "unknown-token"):
            response = await client.post(
                ENDPOINT, json={}, headers={"cookie": f"{CONFIG_COOKIE}={cookie}"}
            )
            assert response.status_code == 403
            assert "config" not in response.json()
            assert CONFIG_COOKIE not in response.cookies
        assert len(store.list_tokens()["items"]) == 1


@pytest.mark.asyncio
async def test_actual_tls_export_requires_trusted_certificate_and_produces_usable_config(
    secured, pki
):
    app, settings, _, _ = secured
    directory, _ = pki
    async with _https_server(app, settings) as origin:
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        async with httpx.AsyncClient(verify=context, base_url=origin, timeout=3) as anonymous:
            denied = await anonymous.post(ENDPOINT, json={}, headers={"origin": origin})
            assert denied.status_code == 403

        for name in ("untrusted", "expired"):
            context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
            context.load_cert_chain(str(directory / f"{name}.pem"), str(directory / f"{name}.key"))
            async with httpx.AsyncClient(verify=context, base_url=origin, timeout=3) as invalid:
                with pytest.raises(httpx.TransportError):
                    await invalid.post(ENDPOINT, json={}, headers={"origin": origin})

        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        context.load_cert_chain(str(directory / "client.pem"), str(directory / "client.key"))
        async with httpx.AsyncClient(verify=context, base_url=origin, timeout=3) as browser:
            response = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            token = _exported_token(response, origin=origin)
            repeated = await browser.post(ENDPOINT, json={}, headers={"origin": origin})
            assert _exported_token(repeated, origin=origin) == token

        # JSON configuration is sufficient: MCP/API client has only server trust and Bearer.
        context = ssl.create_default_context(cafile=str(directory / "ca.pem"))
        async with httpx.AsyncClient(
            verify=context,
            base_url=origin,
            timeout=3,
            headers={"authorization": f"Bearer {token}"},
        ) as mcp_client:
            assert (await mcp_client.get("/api/v1/stats")).status_code == 200
            async with (
                streamable_http_client(origin + "/mcp", http_client=mcp_client) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert "kb_search" in {tool.name for tool in (await session.list_tools()).tools}
