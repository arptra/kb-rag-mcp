from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import zipfile
from typing import Any

import httpx
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request

from skill_registry.delivery import (
    build_extension_zip,
    decode_selection,
    encode_selection,
    prepare_install,
)
from skill_registry.http import register_skills_routes
from skill_registry.mcp_server import create_skills_mcp_server


class MemoryRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.files = {
            "SKILL.md": b"---\nname: example\ndescription: Example\n---\nUntrusted instructions.\n",
            "templates/example.bin": b"\x00\xff\x01",
        }
        self.revision = "a" * 64
        self.name = "example"
        self.description = "Example"
        self.tamper = False
        self.installed: list[dict[str, Any]] = []
        self.search_parameters: dict[str, Any] = {}

    def get_release(self, skill_id: str, revision: str | None = None) -> dict[str, Any]:
        self.calls.append(("release", skill_id, revision or "published"))
        if skill_id == "missing":
            raise KeyError("secret-password-in-path")
        return {
            "skill_id": skill_id,
            "name": self.name,
            "description": "Example",
            "revision": revision or self.revision,
            "version": 1,
            "source_id": "source",
            "commit": "b" * 40,
            "created_at": "2026-01-01T00:00:00Z",
            "files": [
                {
                    "path": path, "size": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "executable": path.endswith(".sh"),
                }
                for path, raw in self.files.items()
            ],
        }

    def read_file(self, skill_id: str, revision: str, path: str) -> dict[str, Any]:
        self.calls.append(("file", skill_id, revision, path))
        raw = self.files[path]
        if self.tamper:
            raw += b" tampered"
        try:
            content = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            content = base64.b64encode(raw).decode()
            encoding = "base64"
        return {
            "path": path,
            "encoding": encoding,
            "content": content,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def list_skills(self, **kwargs: Any) -> dict[str, Any]:
        self.search_parameters = kwargs
        self.calls.append(("list",))
        return {
            "skills": [
                {
                    "id": "source:example",
                    "name": self.name,
                    "description": self.description,
                    "internal_details": "not-for-discovery",
                }
            ],
            "total": 1,
        }

    def check_updates(self, installed: list[dict[str, Any]]) -> dict[str, Any]:
        self.installed = installed
        return {
            "updates": [
                {"skill_id": item["skill_id"], "revision": self.revision} for item in installed
            ]
        }

    def list_sources(self) -> list[dict[str, Any]]:
        return [{"id": "source", "name": "Repository", "enabled": True}]

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return []

    def save_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def validate_source(self, _payload: dict[str, Any]) -> dict[str, Any]:
        raise ValueError("https://user:secret-password@git.example/repo")

    def queue_sync(self, source_id: str) -> dict[str, Any]:
        return {"id": "job", "source_id": source_id, "status": "queued"}


@pytest.mark.asyncio
async def test_skills_mcp_is_separate_compact_and_read_only() -> None:
    registry = MemoryRegistry()
    registry.description = "long description " * 2000
    async with Client(create_skills_mcp_server(registry)) as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == {
            "skills_search",
            "skills_get_release",
            "skills_check_updates",
            "skills_prepare_install",
        }
        assert all(tool.annotations and tool.annotations.readOnlyHint for tool in tools)
        result = await client.call_tool("skills_search", {"query": "example"})
        assert result.data["total"] == 1
        assert registry.calls == [("list",)]
        assert registry.search_parameters["published_only"] is True
        assert "Untrusted instructions" not in str(result.data)
        assert "internal_details" not in str(result.data)
        assert len(result.data["skills"][0]["description"]) == 320
        assert result.data["skills"][0]["description_truncated"] is True
        assert result.data["skills"][0]["skill_id"] == "source:example"
        await client.call_tool(
            "skills_check_updates",
            {"installed": [{"skill_id": "source:example", "revision": "b" * 64, "pinned": True}]},
        )
        assert registry.installed[0]["pinned"] is True
        release = await client.call_tool("skills_get_release", {"skill_id": "source:example"})
        assert release.data["revision"] == registry.revision
        with pytest.raises(ToolError, match="pinned revision"):
            await client.call_tool(
                "skills_get_release", {"skill_id": "source:example", "file_path": "SKILL.md"}
            )
        file = await client.call_tool(
            "skills_get_release",
            {"skill_id": "source:example", "revision": registry.revision, "file_path": "SKILL.md"},
        )
        assert file.data["untrusted_data"] is True
        assert file.data["revision"] == registry.revision
        prompts = await client.list_prompts()
        assert [prompt.name for prompt in prompts] == ["corporate_skills_sync"]
        prompt = await client.get_prompt("corporate_skills_sync", {"scope": "project"})
        text = str(prompt.messages)
        assert ".gigacode/skills" in text
        assert "untrusted package data" in text
        assert "when invoked" in text


def test_pinned_subset_archive_preserves_files_and_does_not_follow_latest() -> None:
    registry = MemoryRegistry()
    registry.files["scripts/check.sh"] = b"#!/bin/sh\nexit 0\n"
    prepared = prepare_install(registry, [{"skill_id": "source:example"}], "project")
    token = encode_selection(prepared["manifest"])
    selected, scope = decode_selection(token)
    assert selected[0].revision == "a" * 64
    assert scope == "project"
    registry.revision = "c" * 64
    content = build_extension_zip(registry, prepared["manifest"])
    assert content == build_extension_zip(registry, prepared["manifest"])
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert set(archive.namelist()) == {
            "gigacode-extension.json",
            "corporate-skills.lock.json",
            "README.md",
            "skills/example/SKILL.md",
            "skills/example/templates/example.bin",
            "skills/example/scripts/check.sh",
        }
        lock = json.loads(archive.read("corporate-skills.lock.json"))
        assert lock["skills"][0]["revision"] == "a" * 64
        extension = json.loads(archive.read("gigacode-extension.json"))
        assert "mcpServers" not in extension
        for path, raw in registry.files.items():
            assert archive.read("skills/example/" + path) == raw
        assert archive.getinfo("skills/example/scripts/check.sh").external_attr >> 16 == 0o100755
        assert archive.getinfo("skills/example/SKILL.md").external_attr >> 16 == 0o100644
    assert all(call[2] == "a" * 64 for call in registry.calls if call[0] == "file")


def test_delivery_rejects_name_collisions_paths_unpinned_urls_and_corruption() -> None:
    registry = MemoryRegistry()
    with pytest.raises(ValueError, match="conflicting"):
        prepare_install(registry, [{"skill_id": "one:example"}, {"skill_id": "two:example"}])
    prepared = prepare_install(registry, [{"skill_id": "source:example"}])
    registry.tamper = True
    with pytest.raises(ValueError, match="integrity"):
        build_extension_zip(registry, prepared["manifest"])
    for name in ["../bad", "bad/path", "bad\\path"]:
        registry.name = name
        with pytest.raises(ValueError, match="portable"):
            prepare_install(registry, [{"skill_id": "source:example"}])
    registry.name = "example"
    registry.files["../escape"] = b"escape"
    with pytest.raises(ValueError, match="file path"):
        prepare_install(registry, [{"skill_id": "source:example"}])
    selection = (
        base64.urlsafe_b64encode(
            json.dumps({"scope": "user", "skills": [{"skill_id": "source:example"}]}).encode()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(ValueError, match="selection"):
        decode_selection(selection)


def _dashboard_app(registry: MemoryRegistry) -> Any:
    server = FastMCP("test-dashboard")

    async def reader(request: Request) -> bool:
        return request.headers.get("authorization") in {"Bearer reader", "Bearer manager"}

    async def manager(request: Request) -> bool:
        return request.headers.get("authorization") == "Bearer manager"

    register_skills_routes(server, registry, reader_authorized=reader, manager_authorized=manager)
    return server.http_app(path="/mcp", stateless_http=True)


@pytest.mark.asyncio
async def test_dashboard_authorization_csrf_and_bounded_requests() -> None:
    app = _dashboard_app(MemoryRegistry())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        assert (await client.get("/admin/api/skills/registry")).status_code == 401
        assert (await client.post("/admin/api/skills/sync", json={})).status_code == 401
        client.headers["Authorization"] = "Bearer reader"
        status = await client.get("/admin/api/skills/status")
        assert status.status_code == 200
        assert status.json()["can_manage"] is False
        assert (await client.post("/admin/api/skills/sync", json={})).status_code == 403
        client.headers["Authorization"] = "Bearer manager"
        response = await client.post(
            "/admin/api/skills/sync", json={}, headers={"Origin": "https://evil.example"}
        )
        assert response.status_code == 403
        response = await client.post(
            "/admin/api/skills/sync", json={}, headers={"Origin": "https://testserver"}
        )
        assert response.status_code == 202
        response = await client.post(
            "/admin/api/skills/sources",
            content="x" * 65537,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        response = await client.post("/admin/api/skills/sources/validate", json={})
        assert response.status_code == 400
        assert "secret-password" not in response.text


@pytest.mark.asyncio
async def test_dashboard_onboarding_downloads_are_authenticated_and_pinned() -> None:
    registry = MemoryRegistry()
    app = _dashboard_app(registry)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers={"Authorization": "Bearer reader"},
    ) as client:
        connection = (await client.get("/admin/api/skills/connect?token=private")).json()
        assert connection["mcp_url"] == "https://testserver/skills/mcp"
        assert "private" not in str(connection)
        assert "Bearer reader" not in str(connection)
        response = await client.post(
            "/admin/api/skills/prepare-install",
            json={"skills": [{"skill_id": "source:example"}], "scope": "user"},
        )
        assert response.status_code == 200
        prepared = response.json()
        assert prepared["extension"]["automatic_client_updates"] is False
        original = copy.deepcopy(prepared["manifest"])
        registry.revision = "d" * 64
        bundle = await client.get(prepared["download_url"])
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert json.loads(archive.read("corporate-skills.lock.json")) == original
        skill = await client.get(connection["client_skill_url"])
        assert "name: corporate-skills-sync" in skill.text
        assert "attachment" in skill.headers["content-disposition"]
        del client.headers["Authorization"]
        assert (await client.get(prepared["download_url"])).status_code == 401
        assert (await client.get(connection["client_skill_url"])).status_code == 401
