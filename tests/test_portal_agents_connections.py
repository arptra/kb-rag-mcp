"""Verify published connections through the portal and the agent's actual tool loop."""

import asyncio
import json
import stat
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import FastMCP
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from test_a2a import indexed
from test_portal_agents_engine import Model, call

from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.mcp.servers import McpServerDefinition, McpServerRegistry
from portal_agents import connections, http

ADMIN = {"X-KB-Admin-Password": "separate-test-admin-password"}
READER = {"Authorization": "Bearer portal-test-reader-of-sufficient-length"}
ROOT = "/agents/api/connections"


@pytest.fixture
def portal(settings_factory):
    return indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        mcp_http_bearer_token=READER["Authorization"].removeprefix("Bearer "),
        admin_password=ADMIN["X-KB-Admin-Password"], agents_model_base_url="https://model.test/v1",
    )


@asynccontextmanager
async def browser(portal, factory=httpx.AsyncClient):
    app = create_http_app(*portal)
    async with (
        app.router.lifespan_context(app),
        factory(transport=httpx.ASGITransport(app=app), base_url="https://portal.test") as client,
    ):
        yield client


async def test_admin_workspace_and_sources_remain_scoped_and_persist_across_restart(
    portal, monkeypatch,
):
    calls = []

    async def docker(*args, timeout):
        calls.append(args)
        return 0, "java:21\njava:21\n<none>:<none>\npython:3.12\n"

    monkeypatch.setattr(connections, "_docker", docker)
    check = AsyncMock()
    monkeypatch.setattr(connections, "_check_workspace", check)
    workspace = {"name": "Java проверка", "image": "java:21"}
    deploy = Ed25519PrivateKey.generate()
    host = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode()
    git = {
        "kind": "git", "url": "ssh://git@source.test/org/project",
        "secret": deploy.private_bytes(serialization.Encoding.PEM,
                                       serialization.PrivateFormat.OpenSSH,
                                       serialization.NoEncryption()).decode(),
        "known_hosts": "source.test " + host,
    }
    maven = {"kind": "maven", "url": "https://artifacts.test/maven", "username": "reader",
             "secret": "test-repository-password"}
    async with browser(portal) as client:
        for headers in ({}, READER, {"X-Forwarded-User": "admin"}):
            assert (await client.get(ROOT, headers=headers)).status_code == 403
            assert (await client.get(ROOT + "/images", headers=headers)).status_code == 403
            assert (await client.put(ROOT + "/workspace", headers=headers,
                                     json=workspace)).status_code == 403
        assert calls == []
        assert (await client.get(ROOT + "/images", headers=ADMIN)).json() == {
            "images": ["java:21", "python:3.12"],
        }
        assert calls == [("image", "ls", "--format", "{{.Repository}}:{{.Tag}}")]
        path = ROOT + "/workspace"
        foreign = {**ADMIN, "Origin": "https://foreign.test"}
        assert (await client.put(path, headers=foreign, json=workspace)).status_code == 403
        assert (await client.put(path, headers=ADMIN, json={
            **workspace, "image": "not-installed:latest",
        })).status_code == 422
        assert (await client.put(path, headers=ADMIN, json=workspace)).status_code == 200
        check.assert_awaited_once_with("java:21")
        check.side_effect = HTTPException(422, "test image missing shell")
        assert (await client.put(path, headers=ADMIN, json={
            "name": "bad", "image": "python:3.12",
        })).status_code == 422
        for invalid in ({"secret": "broken private key"}, {"known_hosts": "source.test invalid"},
                        {"known_hosts": "other.test " + host}, {"url": "ssh://git@source.test/a;b"}):
            rejected = await client.put(ROOT + "/sources/app", headers=ADMIN,
                                        json={**git, **invalid})
            assert rejected.status_code == 422
            assert "PRIVATE KEY" not in rejected.text
        for key, data in [("app", git), ("dependencies", maven)]:
            path = ROOT + "/sources/" + key
            assert (await client.put(path, headers=ADMIN, json=data)).status_code == 200
            edit = {k: v for k, v in data.items() if k not in {"secret", "known_hosts"}}
            assert (await client.put(path, headers=ADMIN, json=edit)).status_code == 200
            changed = {**edit, "url": data["url"].replace(".test", ".new.test")}
            assert (await client.put(path, headers=ADMIN, json=changed)).status_code == 422
    async with browser(portal) as client:
        saved = await client.get(ROOT, headers=ADMIN)
        assert saved.json()["workspace"] == workspace
        assert {s["id"] for s in saved.json()["sources"] if s["has_secret"]} == {
            "app", "dependencies",
        }
        private = [git["secret"], git["known_hosts"], maven["secret"]]
        assert all(v not in saved.text for v in private)
        database = portal[1].agents_state_dir / "connections.sqlite3"
        assert stat.S_IMODE(database.stat().st_mode) == 0o600

        def choose(messages, names):
            if isinstance(messages[-1], HumanMessage):
                exposed = {"fetch_git_snapshot", "fetch_maven_artifact", "run_in_workspace"}
                assert exposed <= set(names)
                return call("list_artifact_sources")
            assert isinstance(messages[-1], ToolMessage)
            assert sorted(json.loads(messages[-1].text), key=lambda row: row["id"]) == [
                {"id": "app", "kind": "git"}, {"id": "dependencies", "kind": "maven"},
            ]
            return AIMessage(content="Two sources available.")

        @asynccontextmanager
        async def model(*args, **kwargs):
            yield Model(respond=choose)

        monkeypatch.setattr(http, "create_model", model)
        response = await client.post("/agents/api/chat", headers=READER, json={
            "chat": str(uuid4()), "question": "What artifacts can I inspect?",
        })
        assert json.loads(response.text.splitlines()[-1])["text"] == "Two sources available."
        for key in ("app", "dependencies"):
            result = await client.request("DELETE", ROOT + "/sources/" + key, headers=ADMIN,
                                          json={})
            assert result.status_code == 200
        assert (await client.get(ROOT, headers=ADMIN)).json()["sources"] == []


async def test_mcp_discovery_publication_live_calls_revocation_and_token_binding(
    portal, monkeypatch,
):
    remote = FastMCP("deployment metadata")
    token = "test-secret-for-one-MCP"
    observed = []
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    @remote.tool(annotations={"readOnlyHint": True})
    async def current_version(wait: bool = False) -> dict[str, str]:
        calls.append(wait)
        if wait:
            entered.set()
            await release.wait()
        return {"release": "r27"}

    @remote.tool(annotations={"destructiveHint": True})
    def deploy() -> str:
        raise AssertionError("Unpublished writes should be unreachable")

    mcp_app = remote.http_app(path="/mcp", json_response=True, stateless_http=True)

    async def protected(scope, receive, send):
        headers = dict(scope["headers"])
        observed.append((headers[b"host"], headers.get(b"authorization")))
        if observed[-1] != (b"release.test", ("Bearer " + token).encode()):
            await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
        else:
            await mcp_app(scope, receive, send)

    original_client = httpx.AsyncClient

    def remote_client(**options):
        assert options["follow_redirects"] is False
        return original_client(**options, transport=httpx.ASGITransport(app=protected))

    monkeypatch.setattr(httpx, "AsyncClient", remote_client)

    @asynccontextmanager
    async def model(*args, **kwargs):
        def decide(messages, names):
            last = messages[-1]
            if isinstance(last, HumanMessage):
                assert "call_environment_tool" in names
                return call("call_environment_tool", server_id=server_id,
                            tool_name="current_version", arguments={"wait": last.text == "Wait"})
            assert isinstance(last, ToolMessage) and "r27" in last.text
            return AIMessage(content="Release r27")
        yield Model(respond=decide)

    monkeypatch.setattr(http, "create_model", model)
    original_url = "https://release.test/mcp"
    async with mcp_app.router.lifespan_context(mcp_app), browser(portal, original_client) as client:
        response = await client.post(ROOT + "/servers", headers=ADMIN,
                                     json={"name": "Releases", "url": original_url})
        server_id = response.json()["id"]
        server = ROOT + "/servers/" + server_id
        discover = server + "/discover"
        blocked = await client.post(discover, headers=READER, json={"token": token})
        assert blocked.status_code == 403
        assert observed == []
        assert (await client.post(discover, headers=ADMIN, json={})).status_code == 502
        result = await client.post(discover, headers=ADMIN, json={"token": token})
        assert result.json() == {"tools": ["current_version"]}
        assert (await client.get(ROOT, headers=ADMIN)).json()["servers"][0]["has_token"] is False
        publication = {"tools": ["current_version"], "token": token}
        assert (await client.put(server, headers=ADMIN, json=publication)).status_code == 200
        assert (await client.post(discover, headers=ADMIN, json={})).json() == result.json()
        saved = await client.get(ROOT, headers=ADMIN)
        assert saved.json()["servers"][0]["has_token"] and token not in saved.text
        chat = str(uuid4())
        path = "/agents/api/chat"

        async def question(text):
            response = await client.post(
                path, headers=READER, json={"chat": chat, "question": text},
            )
            response.raise_for_status()
            return [json.loads(line) for line in response.text.splitlines()]

        assert (await question("Lookup"))[-1]["text"] == "Release r27"
        assert calls == [False]
        pending = asyncio.create_task(question("Wait"))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
            assert (await client.put(server, headers=ADMIN, json={"tools": []})).status_code == 200
        finally:
            release.set()
        assert (await pending)[-1]["event"] == "error"
        history = await client.get("/agents/api/chats/" + chat, headers=READER)
        assert history.json()["messages"] == []
        assert (await client.put(server, headers=ADMIN, json={
            "tools": ["current_version"],
        })).status_code == 200
        assert (await client.post(discover, headers=ADMIN, json={})).json() == result.json()

    # A registry can be reconfigured outside this page. An existing server ID at
    # a different URL must not inherit its previous tool grants or credentials.
    registry = McpServerRegistry(portal[1].mcp_servers_path)
    registry.delete(server_id)
    registry.add(McpServerDefinition(id=server_id, name="Replacement", url="https://other.test/mcp"))
    async with browser(portal, original_client) as client:
        changed = (await client.get(ROOT, headers=ADMIN)).json()["servers"][0]
        assert not changed["has_token"] and not changed["tools"]
        assert (await client.post(discover, headers=ADMIN, json={})).status_code == 502
        assert observed[-1] == (b"other.test", None)
