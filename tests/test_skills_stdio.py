"""Exercise the local entry points through real MCP subprocess pipes."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import test_skill_registry as registry_fixtures
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from skill_registry import SkillsRegistry

repository = registry_fixtures.repository
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("launcher", ["module", "script"])
async def test_local_cli_discovers_and_downloads_published_skills(
    tmp_path: Path,
    repository: Path,
    launcher: str,
) -> None:
    registry = SkillsRegistry(tmp_path / "registry")
    source = registry_fixtures.add_source(registry, repository)
    assert registry.sync_now(source["id"])["status"] == "succeeded"
    command, arguments = (
        (sys.executable, ["-m", "skill_registry.mcp_server"])
        if launcher == "module"
        else ("bash", [str(ROOT / "scripts/start-skills-mcp.sh")])
    )
    transport = StdioTransport(
        command,
        arguments,
        cwd=str(tmp_path),
        env={
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "KB_SKILLS_REGISTRY_ENABLED": "true",
            "KB_SKILLS_REGISTRY_DIR": str(registry.root),
            "KB_ACCESS_ENABLED": "false",
        },
        log_file=tmp_path / "mcp-stderr.log",
    )
    async with Client(transport, timeout=15, init_timeout=15) as client:
        assert {tool.name for tool in await client.list_tools()} == {
            "skills_search",
            "skills_get_release",
            "skills_check_updates",
            "skills_prepare_install",
        }
        search = await client.call_tool("skills_search", {})
        assert search.data["total"] == 1
        skill_id = search.data["skills"][0]["skill_id"]
        prepared = await client.call_tool(
            "skills_prepare_install",
            {"skills": [{"skill_id": skill_id}]},
        )
        assert "manifest" in prepared.data
        release = (await client.call_tool("skills_get_release", {"skill_id": skill_id})).data
        for file in release["files"]:
            downloaded = await client.call_tool(
                "skills_get_release",
                {
                    "skill_id": skill_id,
                    "revision": release["revision"],
                    "file_path": file["path"],
                },
            )
            assert downloaded.data["encoding"] == "utf-8"
            assert hashlib.sha256(downloaded.data["content"].encode()).hexdigest() == file["sha256"]

        # A running stdio client sees new dashboard publications without restarting its server.
        registry_fixtures.write_skill(repository, body="Published update")
        registry_fixtures.commit(repository)
        assert registry.sync_now(source["id"])["status"] == "succeeded"
        latest = (await client.call_tool("skills_get_release", {"skill_id": skill_id})).data
        assert latest["revision"] != release["revision"]
        old = await client.call_tool(
            "skills_get_release",
            {
                "skill_id": skill_id,
                "revision": release["revision"],
                "file_path": "SKILL.md",
            },
        )
        assert "First" in old.data["content"]
        assert "Published update" not in old.data["content"]
        assert [prompt.name for prompt in await client.list_prompts()] == ["corporate_skills_sync"]
    assert not (registry.root / "scheduler.lock").exists()


def test_disabled_local_server_exits_without_protocol_noise(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "skill_registry.mcp_server"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "KB_SKILLS_REGISTRY_ENABLED": "false",
            "KB_SKILLS_REGISTRY_DIR": str(tmp_path / "disabled-registry"),
            "KB_ACCESS_ENABLED": "false",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Skills registry is disabled" in result.stderr
    assert not (tmp_path / "disabled-registry").exists()


@pytest.mark.parametrize("launcher", ["module", "script"])
def test_missing_local_registry_fails_without_creating_it(tmp_path: Path, launcher: str) -> None:
    missing = tmp_path / "missing" / "registry"
    command = (
        [sys.executable, "-m", "skill_registry.mcp_server"]
        if launcher == "module"
        else ["bash", str(ROOT / "scripts/start-skills-mcp.sh")]
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "KB_SKILLS_REGISTRY_ENABLED": "true",
            "KB_SKILLS_REGISTRY_DIR": str(missing),
            "KB_ACCESS_ENABLED": "false",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert "Skills registry was not found" in result.stderr
    assert "KB_SKILLS_REGISTRY_DIR" in result.stderr
    assert "Traceback" not in result.stderr
    assert not missing.parent.exists()


def test_read_only_registry_cannot_mutate_or_start_git(tmp_path: Path) -> None:
    writable = SkillsRegistry(tmp_path / "registry")
    before = writable.database_path.read_bytes()
    reader = SkillsRegistry(writable.root, read_only=True)
    assert reader.list_skills()["total"] == 0
    for operation in (
        lambda: reader.save_source({}),
        lambda: reader.delete_source("missing"),
        lambda: reader.validate_source({}),
        lambda: reader.publish("missing", "0" * 64),
        lambda: reader.queue_sync("missing"),
        reader.start,
    ):
        with pytest.raises(RuntimeError, match="Read-only Skills registry"):
            operation()
    with (
        reader._connect() as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute("CREATE TABLE unexpected(value)")
    assert writable.database_path.read_bytes() == before
    assert not (writable.root / "scheduler.lock").exists()
    assert not (writable.root / "checkouts").exists()


def test_read_only_registry_does_not_run_schema_migrations(tmp_path: Path) -> None:
    registry = SkillsRegistry(tmp_path / "registry")
    with sqlite3.connect(registry.database_path) as connection:
        connection.execute("ALTER TABLE sources DROP COLUMN publish_pending")
    reader = SkillsRegistry(registry.root, read_only=True)
    with reader._connect() as connection:
        assert "publish_pending" not in {
            row["name"] for row in connection.execute("PRAGMA table_info(sources)")
        }
    # Dashboard construction retains ownership of migrations.
    SkillsRegistry(registry.root)
    with reader._connect() as connection:
        assert "publish_pending" in {
            row["name"] for row in connection.execute("PRAGMA table_info(sources)")
        }
