"""Real ASGI routing, discovery isolation and existing access-control integration."""

import asyncio
from datetime import timedelta

import httpx
import pytest
import test_access_http as access_fixtures
import test_skill_registry as registry_fixtures
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError

from corporate_kb.config import Settings
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.service import KnowledgeService

pki = access_fixtures.pki
secured = access_fixtures.secured
repository = registry_fixtures.repository

TOKEN = "skills-integration-token-with-more-than-32-characters"
PASSWORD = "skills-integration-admin-password"


def make_app(settings_factory, **overrides):
    settings = settings_factory(mcp_tls_enabled=False, **overrides)
    settings.knowledge_dir.mkdir(parents=True)
    (settings.knowledge_dir / "test.md").write_text("# Example\n\nTest knowledge.")
    service = KnowledgeService(settings)
    service.build_index(force=True)
    return create_http_app(service, settings)


async def discovered(client, path):
    async with (
        streamable_http_client(str(client.base_url).rstrip("/") + path, http_client=client)
        as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=5))
        as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        return {tool.name for tool in tools.tools}


@pytest.mark.asyncio
async def test_skills_and_knowledge_have_independent_mcp_discovery(settings_factory):
    app = make_app(settings_factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        as client,
    ):
        knowledge = await discovered(client, "/mcp")
        skills = await discovered(client, "/skills/mcp")
        assert skills == {
            "skills_search", "skills_get_release", "skills_check_updates", "skills_prepare_install"
        }
        assert "kb_search" in knowledge
        assert knowledge.isdisjoint(skills)
        status = await client.get("/admin/api/skills/status")
        assert status.status_code == 200
        assert status.json()["can_manage"] is True
        assert (await client.get("/admin/api/skills/registry")).json()["skills"] == []
        connection = (await client.get("/admin/api/skills/connect")).json()
        assert connection["mcp_url"] == "http://testserver/skills/mcp"
        assert connection["extension_auto_update"] == "unverified"
        assert "Bearer" not in str(connection.get("mcp_config", {}))


@pytest.mark.asyncio
async def test_skills_management_does_not_accept_reader_token(settings_factory):
    app = make_app(settings_factory, mcp_http_bearer_token=TOKEN, admin_password=PASSWORD)
    source = {
        "name": "Team skills", "git_url": "https://example.invalid/team.git",
        "skills_path": "skills", "enabled": False, "interval_minutes": 0,
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        as client,
    ):
        assert (await client.post("/skills/mcp", json={})).status_code == 401
        assert (await client.get("/admin/api/skills/status")).status_code == 401
        assert (await client.post("/admin/api/skills/sources", json=source)).status_code == 401
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert (await client.get("/admin/api/skills/status")).json()["can_manage"] is False
        assert (await client.post("/admin/api/skills/sources", json=source)).status_code == 403
        assert "skills_search" in await discovered(client, "/skills/mcp")
        client.headers["X-KB-Admin-Password"] = PASSWORD
        saved = await client.post("/admin/api/skills/sources", json=source)
        assert saved.status_code in {200, 201}, saved.text
        blocked = await client.post(
            "/admin/api/skills/sources", json=source, headers={"Origin": "https://evil.invalid"}
        )
        assert blocked.status_code in {400, 403}


@pytest.mark.asyncio
async def test_skills_uses_personal_token_and_separate_admin_session(secured):
    app, _, store, certificates = secured
    issued = store.enroll(certificates["client"])
    source = {
        "name": "Corporate skills", "git_url": "https://example.invalid/skills.git",
        "enabled": False, "interval_minutes": 0,
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")
        as client,
    ):
        client.headers["Authorization"] = f"Bearer {issued.token}"
        assert "skills_search" in await discovered(client, "/skills/mcp")
        assert (await client.get("/admin/api/skills/status")).json()["can_manage"] is False
        assert (await client.post("/admin/api/skills/sources", json=source)).status_code == 403
        login = await client.post(
            "/access/api/login",
            json={"username": "admin", "password": access_fixtures.PASSWORD},
        )
        assert login.status_code == 200
        assert (await client.get("/admin/api/skills/status")).json()["can_manage"] is True
        # Being an admin does not bypass the existing CSRF policy.
        assert (await client.post("/admin/api/skills/sources", json=source)).status_code == 403
        saved = await client.post(
            "/admin/api/skills/sources", json=source,
            headers={"X-CSRF-Token": login.json()["csrf_token"], "Origin": "https://testserver"},
        )
        assert saved.status_code in {200, 201}, saved.text


@pytest.mark.asyncio
async def test_skills_mount_is_configurable(settings_factory):
    app = make_app(settings_factory, skills_mcp_path="/registry/protocol")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        as client,
    ):
        assert "skills_search" in await discovered(client, "/registry/protocol")
        assert (await client.post("/skills/mcp", json={})).status_code == 404


@pytest.mark.asyncio
async def test_skills_can_be_disabled(settings_factory):
    app = make_app(settings_factory, skills_registry_enabled=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        as client,
    ):
        assert "kb_search" in await discovered(client, "/mcp")
        assert (await client.post("/skills/mcp", json={})).status_code == 404
        assert (await client.get("/admin/api/skills/status")).status_code == 404


@pytest.mark.asyncio
async def test_partial_scan_diagnostics_reach_preview_source_jobs_and_mcp(
    settings_factory, repository,
):
    invalid = repository / "skills/broken"
    invalid.mkdir()
    secret = "example-secret-that-must-not-appear-in-diagnostics"
    (invalid / "SKILL.md").write_text(
        f"---\nname: broken\ndescription: [unterminated\nprivate-value: {secret}\n---\n"
    )
    registry_fixtures.commit(repository)
    app = make_app(settings_factory)
    payload = {
        "name": "Mixed source", "git_url": repository.as_uri(), "interval_minutes": 0,
    }
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        as client,
    ):
        preview = await client.post("/admin/api/skills/sources/validate", json=payload)
        assert preview.status_code == 200
        assert len(preview.json()["skills"]) == 1
        assert preview.json()["skipped"] == 1
        assert preview.json()["warnings"][0]["path"] == "broken/SKILL.md"
        assert secret not in preview.text
        saved = await client.post("/admin/api/skills/sources", json=payload)
        source_id = saved.json()["source"]["id"]
        queued = await client.post("/admin/api/skills/sync", json={"source_id": source_id})
        job_id = queued.json()["jobs"][0]["id"]
        async with asyncio.timeout(10):
            while True:
                response = await client.get("/admin/api/skills/jobs", params={"job_id": job_id})
                job = response.json()["job"]
                if job["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.02)
        assert job["status"] == "succeeded_with_warnings"
        assert job["result"]["valid"] == 1
        assert job["result"]["skipped"] == 1
        assert secret not in response.text
        source_response = await client.get("/admin/api/skills/sources")
        source = source_response.json()["sources"][0]
        assert source["last_warnings"] == job["result"]["warnings"]
        assert source["last_error"] is None
        assert secret not in source_response.text
        async with (
            streamable_http_client("http://testserver/skills/mcp", http_client=client)
            as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.call_tool("skills_search", {})
            assert not listed.isError
            assert listed.structuredContent["total"] == 1
            skill = listed.structuredContent["skills"][0]
            file = await session.call_tool("skills_get_release", {
                "skill_id": skill["skill_id"], "revision": skill["published_revision"],
                "file_path": "SKILL.md",
            })
            assert not file.isError
            assert "First" in file.structuredContent["content"]


@pytest.mark.parametrize("path", ["/mcp", "/admin/mcp", "/skills/../mcp", "/skills/mcp/"])
def test_skills_mount_rejects_ambiguous_paths(path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, skills_mcp_path=path)


def test_skills_mount_cannot_shadow_knowledge():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_http_path="/skills/knowledge")
