import asyncio
import os
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from portal_agents import workspace


async def test_untouched_and_revoked_workspaces_never_start_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = AsyncMock()
    monkeypatch.setattr(workspace, "_docker", docker)
    grant = AsyncMock(return_value=False)
    room = workspace.OfflineWorkspace("configured-image", grant)
    tool = room.tool()
    await room.close()
    docker.assert_not_awaited()
    with pytest.raises(PermissionError):
        await tool.ainvoke({"script": "pwd"})
    await room.close()
    docker.assert_not_awaited()


async def test_revocation_before_another_call_prevents_script_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = AsyncMock(return_value=(0, "ok"))
    monkeypatch.setattr(workspace, "_docker", docker)
    grant = AsyncMock(return_value=True)
    room = workspace.OfflineWorkspace("configured-image", grant)
    tool = room.tool()
    assert await tool.ainvoke({"script": "pwd"}) == "exit_code=0\nok"
    grant.return_value = False
    try:
        with pytest.raises(PermissionError):
            await tool.ainvoke({"script": "echo blocked"})
    finally:
        await room.close()
    await room.close()
    assert [call.args[0] for call in docker.await_args_list] == ["run", "exec", "rm"]


async def test_revocation_while_docker_starts_prevents_the_first_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(workspace, "_docker", docker)
    room = workspace.OfflineWorkspace("configured-image", AsyncMock(side_effect=[True, False]))
    try:
        with pytest.raises(PermissionError):
            await room.tool().ainvoke({"script": "echo blocked"})
    finally:
        await room.close()
    assert [call.args[0] for call in docker.await_args_list] == ["run", "rm"]


async def test_failed_docker_start_can_be_retried_in_the_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker = AsyncMock(side_effect=[(1, "failed"), (1, "missing"), (0, "ok"), (0, "ok"), (0, "")])
    monkeypatch.setattr(workspace, "_docker", docker)
    room = workspace.OfflineWorkspace("configured-image", AsyncMock(return_value=True))
    tool = room.tool()
    try:
        assert "не запустилась" in await tool.ainvoke({"script": "echo ok"})
        assert await tool.ainvoke({"script": "echo ok"}) == "exit_code=0\nok"
    finally:
        await room.close()
    assert [call.args[0] for call in docker.await_args_list] == ["run", "rm", "run", "exec", "rm"]


async def test_real_offline_workspaces_reuse_their_own_files_and_remove_containers() -> None:
    # The test never pulls an image or builds one. CI can supply its cached image.
    image = os.environ.get("PORTAL_AGENTS_TEST_IMAGE", "alpine:3.20")
    if not shutil.which("docker"):
        pytest.skip("Docker CLI unavailable")
    try:
        available = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker daemon unavailable")
    if available.returncode:
        pytest.skip("No local Docker daemon or cached test image")

    grant = AsyncMock(return_value=True)
    one = workspace.OfflineWorkspace(image, grant)
    two = workspace.OfflineWorkspace(image, grant)
    assert one.name != two.name
    first, second = one.tool(), two.tool()
    try:
        assert await first.ainvoke({"script": "echo only-first > note"}) == "exit_code=0\n"
        assert await first.ainvoke({"script": "cat note"}) == "exit_code=0\nonly-first\n"
        assert await second.ainvoke({"script": "test ! -e note && echo only-second > note"}) == (
            "exit_code=0\n"
        )
        assert await first.ainvoke({"script": "cat note"}) == "exit_code=0\nonly-first\n"
        assert await second.ainvoke({"script": "cat note"}) == "exit_code=0\nonly-second\n"
        info = await workspace._docker("inspect", "--format",
                                       "{{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}}",
                                       one.name)
        assert info == (0, "none true\n")
    finally:
        await asyncio.gather(one.close(), two.close())
    for room in (one, two):
        code, output = await workspace._docker("container", "inspect", room.name)
        assert code != 0 and "No such" in output
