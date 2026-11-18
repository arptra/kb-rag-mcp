"""Enrollment remains reachable independently of existing access credentials."""

from __future__ import annotations

import re

import httpx
import pytest
import test_access_http as http_fixtures

from corporate_kb.access.http import MCP_CONFIG_COOKIE, USER_COOKIE
from corporate_kb.access.tls import _CertificateScopeApp

pki = http_fixtures.pki
secured = http_fixtures.secured
ORIGIN = "https://testserver"


async def _assert_public_page(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    for path in ("/connect", "/connect/"):
        page = await client.get(path, headers=headers)
        assert page.status_code == 200, page.text
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-store"
        assets = re.findall(r'(?:src|href)="(/admin/assets/[^\"]+)"', page.text)
        assert any(asset.endswith(".js") for asset in assets)
        assert any(asset.endswith(".css") for asset in assets)
        for path in assets:
            asset = await client.get(path, headers=headers)
            assert asset.status_code == 200, path
            assert asset.content
            assert "text/html" not in asset.headers["content-type"]


@pytest.mark.asyncio
async def test_connect_and_assets_ignore_expired_unknown_and_revoked_credentials(
    secured, monkeypatch
):
    app, _settings, store, certificates = secured
    browser_session = store.enroll(certificates["client"], purpose="browser")
    revoked_mcp = store.enroll(certificates["client"])
    store.revoke_token(revoked_mcp.token_id, actor="test")
    assert browser_session.expires_at is not None
    monkeypatch.setattr(
        "corporate_kb.access.store._now", lambda: browser_session.expires_at + 1
    )

    cases = (
        {},
        {"cookie": f"{USER_COOKIE}=unknown-session"},
        {"cookie": f"{USER_COOKIE}={browser_session.token}"},
        {"cookie": f"{MCP_CONFIG_COOKIE}=unknown-config-token"},
        {"cookie": f"{MCP_CONFIG_COOKIE}={revoked_mcp.token}"},
        {"authorization": "Bearer unknown-token"},
        {"authorization": f"Bearer {revoked_mcp.token}"},
        {
            "cookie": (
                f"{USER_COOKIE}={browser_session.token}; "
                f"{MCP_CONFIG_COOKIE}={revoked_mcp.token}"
            ),
            "authorization": f"Bearer {revoked_mcp.token}",
        },
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client,
    ):
        for headers in cases:
            await _assert_public_page(client, headers)
            status = await client.get("/auth/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.headers["cache-control"] == "no-store"
            assert status.json()["authenticated"] is False
            assert status.json()["certificate_present"] is False
            # Public enrollment assets must not accidentally expose protected APIs.
            assert (await client.get("/api/v1/stats", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_revoked_user_can_open_connect_but_cannot_export_credentials(secured):
    app, _settings, store, certificates = secured
    issued = store.enroll(certificates["client"])
    store.revoke_user(issued.user["id"], actor="test")
    headers = {"cookie": f"{MCP_CONFIG_COOKIE}={issued.token}"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_CertificateScopeApp(app, certificates["client"])
            ),
            base_url=ORIGIN,
        ) as client,
    ):
        await _assert_public_page(client, headers)
        status = await client.get("/auth/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["authenticated"] is False
        assert status.json()["certificate_present"] is True
        exported = await client.post(
            "/auth/mcp-config", json={}, headers={**headers, "origin": ORIGIN}
        )
        assert exported.status_code == 403
        assert "config" not in exported.json()
        assert MCP_CONFIG_COOKIE not in exported.cookies
        assert store.list_tokens()["total"] == 1
