"""Exercise Docker boundaries without building images or starting containers."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness import sandbox
from agent_harness.contracts import Principal
from agent_harness.integrations_config import RepositoryConfig, SandboxConfig
from agent_harness.sandbox import SandboxBackend, SandboxError

COMMIT = "a" * 40
REPOSITORY = RepositoryConfig(id="service", name="Service", url="https://git.example/service.git")
CONFIG = SandboxConfig(enabled=True)
OWNER = Principal("alice")


class FakeDocker:
    """A harmless Python subprocess stands in for each Docker CLI invocation."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.original = asyncio.create_subprocess_exec
        self.calls: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.image_available = True
        self.clone_fails = False
        self.cleanup_fails = False
        monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/fake/docker")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", self.create)

    async def create(
        self, executable: str, *args: str, **kwargs: Any
    ) -> asyncio.subprocess.Process:
        if executable != "/fake/docker":
            return await self.original(executable, *args, **kwargs)
        self.calls.append({"args": list(args), "env": dict(kwargs.get("env", {}))})
        value: Any = "ok"
        exit_code = 0
        if args[:2] == ("image", "inspect"):
            exit_code = 0 if self.image_available else 1
        if args[:2] == ("rm", "-f") and not self.image_available:
            exit_code = 1  # A failed start may leave no container to remove.
        if args[:2] == ("volume", "rm") and self.cleanup_fails:
            exit_code = 1
        if args[0] == "run":
            if not self.image_available:
                exit_code = 1
            elif sandbox.PROBE_TOOLS_SCRIPT in args:
                value = {"python": "3.12", "git": True, "ssh": True, "sleep": True,
                         "java": False, "mvn": False, "gradle": False}
            elif sandbox.CLONE_SCRIPT in args:
                value = {"commit": COMMIT}
                exit_code = 1 if self.clone_fails else 0
            elif sandbox.READ_SCRIPT in args:
                value = {"text": "1: current implementation", "truncated": False}
            elif sandbox.SEARCH_SCRIPT in args:
                value = {
                    "matches": [{"path": "main.py", "line": 3, "text": "def handler():"}],
                    "limited": False,
                }
            elif "long-task" in args:
                self.entered.set()
                return await self.original(
                    sys.executable, "-c", "import time; time.sleep(60)", **kwargs
                )
            elif "large-output" in args:
                value = "x" * 100000
        value = json.dumps(value) if isinstance(value, dict) else value
        source = f"import sys; print({value!r}); sys.exit({exit_code})"
        return await self.original(sys.executable, "-c", source, **kwargs)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    return FakeDocker(monkeypatch)


async def invoke(
    backend: SandboxBackend,
    name: str = "clone_repository",
    *,
    run: str = "run-1",
    principal: Principal = OWNER,
    repository: RepositoryConfig = REPOSITORY,
    config: SandboxConfig = CONFIG,
    cancel: Event | None = None,
    **args: Any,
) -> list[Any]:
    return await backend.call(
        name,
        {"repository_id": repository.id, **args},
        repository=repository,
        config=config,
        principal=principal,
        run_id=run,
        cancel=cancel or Event(),
    )


async def test_clone_reused_bounded_networkless_tools_and_explicit_cleanup(
    docker: FakeDocker,
) -> None:
    backend = SandboxBackend()
    cloned = await invoke(backend)
    searched = await invoke(backend, "search_repository", query="handler")
    read = await invoke(backend, "read_repository", path="main.py", start_line=1, end_line=10)
    assert cloned[0].revision == searched[0].revision == read[0].revision == COMMIT
    assert searched[0].index_id == "repo:service"
    assert "main.py:3:" in searched[0].text
    volumes = [c["args"] for c in docker.calls if c["args"][:2] == ["volume", "create"]]
    assert len(volumes) == 1
    assert volumes[0][-1].startswith("harness-") and "run-1" not in volumes[0][-1]
    assert "type=tmpfs" in volumes[0] and "device=tmpfs" in volumes[0]
    assert "o=size=512m,uid=65532,gid=65532,mode=0700,nosuid,nodev" in volumes[0]
    commands = [c["args"] for c in docker.calls if c["args"][0] == "run"]
    assert sum(sandbox.CLONE_SCRIPT in command for command in commands) == 1
    keeper = next(command for command in commands if "--detach" in command)
    assert keeper[-1] == "7500" and keeper[keeper.index("--network") + 1] == "none"
    for command in commands:
        assert command[command.index("--pull") + 1] == "never"
        assert command[command.index("--user") + 1] == "65532:65532"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--security-opt") + 1] == "no-new-privileges"
        assert "--read-only" in command and "--pids-limit" in command
        assert "--cpus" in command and "--memory-swap" in command
        assert command[command.index("--log-driver") + 1] == "none"
        assert command[command.index("--mount") + 1].startswith("type=volume,source=harness-")
        assert "--privileged" not in command and "--volume" not in command
        network = command[command.index("--network") + 1]
        assert network == ("bridge" if sandbox.CLONE_SCRIPT in command else "none")
    await backend.close_run("run-1")
    assert docker.calls[-2]["args"] == ["rm", "-f", "-v", keeper[keeper.index("--name") + 1]]
    assert docker.calls[-1]["args"] == ["volume", "rm", "-f", volumes[0][-1]]
    count = len(docker.calls)
    await backend.close_run("run-1")
    assert len(docker.calls) == count


async def test_credentials_only_in_clone_environment_and_git_never_runs_scripts(
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPO_SECRET", "test-private-value")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    repository = REPOSITORY.model_copy(update={"credential_env": "REPO_SECRET"})
    backend = SandboxBackend()
    await invoke(backend, repository=repository)
    await invoke(backend, "run_in_sandbox", repository=repository, command=["python", "-V"])
    clone = next(c for c in docker.calls if sandbox.CLONE_SCRIPT in c["args"])
    assert clone["env"]["HARNESS_GIT_TOKEN"] == "test-private-value"
    for call in docker.calls:
        assert "test-private-value" not in " ".join(call["args"])
        assert "UNRELATED_SECRET" not in call["env"]
        if call is not clone:
            assert "HARNESS_GIT_TOKEN" not in call["env"]
    assert "protocol.file.allow=never" in sandbox.CLONE_SCRIPT
    assert "core.hooksPath=/dev/null" in sandbox.CLONE_SCRIPT
    assert "http.followRedirects=false" in sandbox.CLONE_SCRIPT
    assert '"GIT_CONFIG_GLOBAL": "/dev/null"' in sandbox.CLONE_SCRIPT
    assert '"--no-recurse-submodules"' in sandbox.CLONE_SCRIPT
    assert '"GIT_LFS_SKIP_SMUDGE": "1"' in sandbox.CLONE_SCRIPT
    assert '"checkout", "--detach"' in sandbox.CLONE_SCRIPT
    await backend.close_run("run-1")


async def test_owner_and_resource_permissions_checked_before_docker(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    await invoke(backend)
    count = len(docker.calls)
    with pytest.raises(PermissionError):
        await invoke(backend, principal=Principal("bob"))
    private = REPOSITORY.model_copy(update={"allowed_owners": ["alice"]})
    with pytest.raises(PermissionError):
        await invoke(backend, run="other", principal=Principal("bob"), repository=private)
    assert len(docker.calls) == count
    changed = REPOSITORY.model_copy(update={"ref": "different"})
    with pytest.raises(SandboxError, match="changed"):
        await invoke(backend, repository=changed)
    await backend.close_run("run-1")


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", ".git/config", "a/../../b", "a\\b", "a\0b"]
)
async def test_invalid_paths_do_not_launch_docker(docker: FakeDocker, path: str) -> None:
    with pytest.raises(ValueError):
        await invoke(SandboxBackend(), "read_repository", path=path)
    assert docker.calls == []


@pytest.mark.parametrize("command", [[], "echo hello", ["-option"], ["echo", "a\0b"], [1]])
async def test_invalid_commands_do_not_launch_docker(docker: FakeDocker, command: Any) -> None:
    with pytest.raises(ValueError):
        await invoke(SandboxBackend(), "run_in_sandbox", command=command)
    assert docker.calls == []


async def test_missing_docker_image_and_credentials_fail_closed(
    docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = SandboxBackend()
    docker.image_available = False
    status = await backend.probe(CONFIG)
    assert status["available"] is False
    with pytest.raises(SandboxError):
        await invoke(backend)
    assert not any(c["args"][0] in {"pull", "build"} for c in docker.calls)
    assert any(c["args"][:2] == ["volume", "rm"] for c in docker.calls)
    docker.image_available = True
    repository = REPOSITORY.model_copy(update={"credential_env": "UNSET_REPO_CREDENTIAL"})
    monkeypatch.delenv("UNSET_REPO_CREDENTIAL", raising=False)
    count = len(docker.calls)
    with pytest.raises(SandboxError, match="credential"):
        await invoke(backend, run="missing-credential", repository=repository)
    assert len(docker.calls) == count
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    assert (await backend.probe(CONFIG))["available"] is False


async def test_clone_failure_removes_workspace_and_can_retry(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    docker.clone_fails = True
    with pytest.raises(SandboxError, match="clone failed"):
        await invoke(backend)
    assert docker.calls[-1]["args"][:2] == ["volume", "rm"]
    docker.clone_fails = False
    result = await invoke(backend)
    assert result[0].revision == COMMIT
    await backend.close_run("run-1")


async def test_probe_can_check_readiness_before_enabling_execution(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    config = SandboxConfig(enabled=False)
    status = await backend.probe(config)
    assert status["available"] is True and status["enabled"] is False
    assert "выполнение выключено" in status["reason"]
    assert [c["args"][0] for c in docker.calls] == ["version", "image", "run"]
    probe = docker.calls[-1]["args"]
    assert probe[probe.index("--network") + 1] == "none"
    assert probe[probe.index("--pull") + 1] == "never"
    assert probe[probe.index("--entrypoint") + 1] == "python"
    with pytest.raises(SandboxError, match="disabled"):
        await invoke(backend, config=config)
    assert len(docker.calls) == 3


async def test_cancellation_force_removes_container_and_volume_and_releases_capacity(
    docker: FakeDocker,
) -> None:
    backend, cancel = SandboxBackend(), Event()
    task = asyncio.create_task(
        invoke(backend, "run_in_sandbox", command=["long-task"], cancel=cancel)
    )
    await asyncio.wait_for(docker.entered.wait(), 2)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert len([c for c in docker.calls if c["args"][:2] == ["rm", "-f"]]) == 2
    assert docker.calls[-1]["args"][:2] == ["volume", "rm"]
    assert "run-1" not in backend._runs
    assert (await invoke(backend, run="next"))[0].revision == COMMIT
    await backend.close_run("next")


async def test_close_run_cancels_active_command_without_deadlock(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    task = asyncio.create_task(invoke(backend, "run_in_sandbox", command=["long-task"]))
    await asyncio.wait_for(docker.entered.wait(), 2)
    await asyncio.wait_for(backend.close_run("run-1"), 2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert docker.calls[-1]["args"][:2] == ["volume", "rm"]


async def test_timeout_and_bounded_output_mark_workspace_as_modified(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    result = await invoke(backend, "run_in_sandbox", command=["large-output"])
    assert len(result[0].text) <= CONFIG.max_output_chars
    assert "truncated" in result[0].text.lower()
    assert result[0].revision == COMMIT + "+workspace"
    later = await invoke(backend, "read_repository", path="main.py")
    assert later[0].revision == COMMIT + "+workspace"
    # Bypass the production minimum only to test the timeout quickly.
    short = CONFIG.model_copy(update={"timeout_seconds": 0.1})
    with pytest.raises(TimeoutError):
        await invoke(backend, "run_in_sandbox", command=["long-task"], config=short)
    assert docker.calls[-1]["args"][:2] == ["volume", "rm"]


async def test_failed_volume_cleanup_is_retryable(docker: FakeDocker) -> None:
    backend = SandboxBackend()
    await invoke(backend)
    docker.cleanup_fails = True
    with pytest.raises(SandboxError, match="cleanup"):
        await backend.close_run("run-1")
    assert "run-1" in backend._runs
    docker.cleanup_fails = False
    await backend.close_run("run-1")
    assert "run-1" not in backend._runs


def trusted_script(script: str, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # Only the fixed server script executes locally in this test; checkout code never does.
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(root), *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )


def test_read_and_search_do_not_follow_symlinks_or_read_special_files(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("needle private outside", encoding="utf-8")
    (repository / "linked.txt").symlink_to(outside)
    (repository / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    (repository / ".git").mkdir()
    (repository / ".git" / "config").write_text("needle hidden", encoding="utf-8")
    (repository / "main.py").write_text("one\nneedle visible\nthree", encoding="utf-8")
    os.mkfifo(repository / "pipe")
    for path in ("linked.txt", "linked-dir/outside.txt", "../outside.txt", ".git/config", "pipe"):
        output = trusted_script(sandbox.READ_SCRIPT, repository, path, "1", "160", "1000")
        assert output.returncode == 2, path
    read = trusted_script(sandbox.READ_SCRIPT, repository, "main.py", "2", "2", "1000")
    assert json.loads(read.stdout)["text"] == "2: needle visible"
    search = trusted_script(sandbox.SEARCH_SCRIPT, repository, "needle", "1000")
    assert json.loads(search.stdout)["matches"] == [
        {"path": "main.py", "line": 2, "text": "needle visible"}
    ]


def test_scripts_reject_replaced_repository_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.symlink_to(tmp_path, target_is_directory=True)
    output = trusted_script(sandbox.READ_SCRIPT, repository, "file", "1", "2", "1000")
    assert output.returncode == 2
    output = trusted_script(sandbox.SEARCH_SCRIPT, repository, "needle", "1000")
    assert output.returncode == 2
