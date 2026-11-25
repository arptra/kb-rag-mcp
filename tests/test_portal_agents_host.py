"""Test feature gating, existing dashboard access, and isolation from old deployment data."""

import json
from collections.abc import Callable
from uuid import uuid4

import httpx
import pytest
from test_a2a import indexed

from corporate_kb.config import Settings
from corporate_kb.mcp.http_server import create_http_app


@pytest.mark.parametrize("enabled", [False, True])
async def test_agent_extension_uses_portal_lifespan_and_grants(
    settings_factory: Callable[..., Settings], enabled: bool,
) -> None:
    password, reader = "admin-integration-test", "reader-token-for-this-integration-test"
    service, settings = indexed(settings_factory, agents_enabled=enabled, a2a_enabled=False,
                                admin_password=password, mcp_http_bearer_token=reader)
    old_db = settings.agents_state_dir.parent / "harness" / "history.sqlite3"
    old_db.parent.mkdir(parents=True)
    old_db.write_bytes(b"existing deployment archive")
    app = create_http_app(service, settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                          base_url="http://localhost") as client,
    ):
        admin_headers = {"X-KB-Admin-Password": password}
        auth = {"Authorization": "Bearer " + reader}
        overview = (await client.get("/admin/api/overview", headers=admin_headers)).json()
        assert overview["agents"] == {"enabled": enabled, "url": "/agents/" if enabled else None}
        assert "harness" not in overview
        assert (await client.get("/harness/api/status", headers=auth)).status_code == 404
        path = "/agents/api/model"
        if not enabled:
            assert (await client.get(path, headers=auth)).status_code == 404
            assert not settings.agents_state_dir.exists()
        else:
            assert (settings.agents_state_dir / "checkpoints.sqlite3").is_file()
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers=auth)).status_code == 200
            assert (await client.post(path, headers=auth, json={})).status_code == 403
            assert (await client.get(path, headers=admin_headers)).status_code == 200
            history = await client.get("/agents/api/chats/" + str(uuid4()), headers=auth)
            assert history.status_code == 200 and history.json()["messages"] == []
            assert reader not in json.dumps(overview["agents"])
    assert old_db.read_bytes() == b"existing deployment archive"
