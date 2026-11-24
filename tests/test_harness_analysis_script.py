"""Agent tool chain for writing and executing a per-run analysis program."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_harness_integrations import ToolModel, configuration, hub, wait_finished
from test_harness_sandbox import OWNER, FakeDocker, invoke

from agent_harness import sandbox
from agent_harness.config import HarnessSettings
from agent_harness.contracts import RunRequest
from agent_harness.integrations_config import IntegrationSettings
from agent_harness.runtime import HarnessRuntime
from agent_harness.sandbox import SandboxBackend

COUNT_PAYMENTS = """import csv
from collections import Counter
with open("payments.csv", newline="", encoding="utf-8") as file:
    states = Counter(row["state"] for row in csv.DictReader(file))
print("pending =", states["pending"])
"""


async def test_script_output_reaches_next_model_turn(tmp_path: Path, monkeypatch: Any) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    (repo / "payments.csv").write_text("state\npaid\npending\npending\n", encoding="utf-8")

    class FixtureDocker(FakeDocker):
        # Run the trusted writer and one fixed, known test program in a temporary directory.
        # Production sends both operations to Docker. All other Docker operations are stubbed.
        async def create(
            self, executable: str, *args: str, **kwargs: Any
        ) -> asyncio.subprocess.Process:
            if executable == "/fake/docker" and sandbox.WRITE_ANALYSIS_SCRIPT in args:
                assert COUNT_PAYMENTS not in args and "--interactive" in args
                assert args[args.index("--network") + 1] == "none"
                assert "HARNESS_GIT_TOKEN" not in kwargs["env"]
                assert args[-2:] == ("/workspace", "Count.py")
                return await self.original(
                    sys.executable, "-I", "-S", "-c", sandbox.WRITE_ANALYSIS_SCRIPT,
                    str(workspace), "Count.py", **kwargs,
                )
            if executable == "/fake/docker" and args[-2:] == ("-I", "/workspace/analysis/Count.py"):
                assert args[args.index("--network") + 1] == "none"
                assert args[args.index("--workdir") + 1] == "/workspace/repo"
                path = workspace / "analysis" / "Count.py"
                assert path.read_text(encoding="utf-8") == COUNT_PAYMENTS
                return await self.original(sys.executable, "-I", str(path), cwd=repo, **kwargs)
            return await super().create(executable, *args, **kwargs)

    class AnalysisModel(ToolModel):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            context = json.loads(kwargs["messages"][-1]["content"])
            if len(self.requests) == 1:
                call = {"name": "write_analysis_script", "args": {
                    "repository_id": "orders", "filename": "Count.py", "source": COUNT_PAYMENTS,
                }}
            elif len(self.requests) == 2:
                receipt = next(e for e in context["evidence"] if e["kind"] == "tool_metadata")
                call = {"name": "run_in_sandbox", "args": json.loads(
                    receipt["text"].split("Call run_in_sandbox with ", 1)[1],
                )}
            else:
                result = next(e for e in context["evidence"] if "pending = 2" in e["text"])
                return {"content": f"Ожидающих платежей: 2 [{result['id']}].", "tool_calls": []}
            return {"tool_calls": [call]}

    docker = FixtureDocker(monkeypatch)
    integrations = hub(tmp_path)
    integrations._sandbox = SandboxBackend()
    model = AnalysisModel()
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path),
                             model=model, tools=integrations)
    await runtime.start()
    try:
        run = await runtime.submit(OWNER, RunRequest(
            question="Посчитай скриптом ожидающие платежи в payments.csv",
            mode="investigate", index_ids=["repo:orders"],
        ))
        finished = await wait_finished(runtime, run["id"])
        assert finished["status"] == "completed", finished["warnings"]
        assert finished["answer"].startswith("Ожидающих платежей: 2")
        assert finished["metrics"]["tool_calls"] == 2
        assert any(c["args"][:2] == ["volume", "rm"] for c in docker.calls)
        source = next(e for e in finished["evidence"] if e["kind"] == "source")
        assert source["tool_origin"]["tool"] == "run_in_sandbox"
    finally:
        await runtime.stop()


@pytest.mark.parametrize(("filename", "source"), [
    ("../evil.py", "123"), ("/workspace/repo/main.py", "123"), ("payload.sh", "123"),
    ("Blank.py", " "), ("TooLarge.java", "я" * 13000),
])
async def test_invalid_script_never_launches_docker(
    filename: str, source: str, monkeypatch: Any
) -> None:
    docker = FakeDocker(monkeypatch)
    with pytest.raises(ValueError):
        await invoke(SandboxBackend(), "write_analysis_script", filename=filename, source=source)
    assert not docker.calls


def test_writer_replaces_links_without_touching_targets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scratch = workspace / "analysis"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "Count.py"
    target.write_text("original", encoding="utf-8")
    args = [sys.executable, "-I", "-S", "-c", sandbox.WRITE_ANALYSIS_SCRIPT,
            str(workspace), "Count.py"]
    scratch.symlink_to(outside, target_is_directory=True)
    assert subprocess.run(args, input=b"body", capture_output=True, timeout=3).returncode != 0
    scratch.unlink()
    scratch.mkdir()
    dest = scratch / "Count.py"
    dest.symlink_to(target)
    subprocess.run(args, input=b"body", capture_output=True, timeout=3, check=True)
    assert not dest.is_symlink() and dest.read_bytes() == b"body"
    dest.unlink()
    os.link(target, dest)
    subprocess.run(args, input=b"revised", capture_output=True, timeout=3, check=True)
    assert dest.read_bytes() == b"revised"
    assert target.read_bytes() == b"original"
    assert list(scratch.iterdir()) == [dest]


async def test_resource_enums_match_selected_type_and_access(tmp_path: Path) -> None:
    integrations = hub(tmp_path)
    config = configuration()
    jira = config["connections"][0]
    config["connections"].extend([
        {**jira, "id": "not-selected"}, {**jira, "id": "private", "allowed_owners": ["bob"]},
        {**jira, "kind": "a2a", "id": "agent", "allowed_tools": []},
    ])
    integrations.store.save(IntegrationSettings.model_validate(config))
    tools = await integrations.describe(
        OWNER, ["repo:orders", "connection:jira", "connection:private", "connection:agent"],
    )
    for tool in tools:
        name = tool["function"]["name"]
        props = tool["function"]["parameters"]["properties"]
        if "repository_id" in props:
            assert props["repository_id"]["enum"] == ["orders"]
        else:
            assert props["connection_id"]["enum"] == (["agent"] if name == "a2a_ask" else ["jira"])
