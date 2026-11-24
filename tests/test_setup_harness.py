"""Hermetic Harness setup tests: fake Python, uv and Docker; no downloads or builds."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SHIM = r"""
import json, os, shutil, sys
from pathlib import Path
executable = Path(sys.argv[0])
args = sys.argv[1:]
environment_keys = (
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_NO_INDEX", "PIP_FIND_LINKS",
    "HTTPS_PROXY", "PIP_CERT", "REQUESTS_CA_BUNDLE", "UV_PROJECT_ENVIRONMENT",
    "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
)
with open(os.environ["MOCK_LOG"], "a") as stream:
    stream.write(json.dumps({"executable": str(executable), "args": args,
        "cwd": os.getcwd(), "environment": {
            key: os.environ.get(key) for key in environment_keys}}) + "\n")
if executable.name == "docker":
    if args[:1] not in (["info"], ["build"]):
        sys.exit(97)
elif executable.name == "uv":
    if args[:1] != ["sync"]:
        sys.exit(97)
    sys.exit(int(os.environ.get("MOCK_UV_EXIT", "0")))
elif args[:1] == ["-c"]:
    print(os.environ.get("MOCK_PROJECT_VERSION", "3.12")
        if executable.parent.parent.name == ".venv"
        else os.environ.get("MOCK_PYTHON_VERSION", "3.12"))
elif args[:2] == ["-m", "venv"]:
    directory = Path(args[2]) / "bin"
    directory.mkdir(parents=True)
    shutil.copy2(os.environ["MOCK_SHIM"], directory / "python")
elif args[:3] == ["-m", "pip", "--version"]:
    if os.environ.get("MOCK_NO_PIP") == "1" and not (executable.parent / "pip-ready").exists():
        sys.exit(1)
    print("mock pip")
elif args[:2] == ["-m", "ensurepip"]:
    (executable.parent / "pip-ready").touch()
elif args[:3] == ["-m", "pip", "install"]:
    sys.exit(int(os.environ.get("MOCK_PIP_EXIT", "0")))
else:
    sys.exit(97)
"""


@pytest.fixture
def setup_harness_project(tmp_path):
    project = tmp_path / "project with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("setup-harness.sh", "setup-pip.sh"):
        shutil.copy2(source / name, scripts / name)
    (project / "pyproject.toml").write_text("[project]\nname = 'test-harness'\n")
    (project / "uv.lock").write_text("version = 1\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Never discover the host's real Python, uv, pip or Docker.
    for name in ("bash", "dirname", "cat"):
        resolved = shutil.which(name)
        assert resolved is not None
        (binaries / name).symlink_to(resolved)
    shim = tmp_path / "interpreter-shim"
    shim.write_text(f"#!{sys.executable}\n" + _SHIM)
    shim.chmod(0o700)
    log = tmp_path / "calls.jsonl"
    env = {
        "PATH": str(binaries),
        "MOCK_LOG": str(log),
        "MOCK_SHIM": str(shim),
    }
    return project, binaries, shim, log, env


def _executable(shim: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shim, destination)


def _run(fixture, *args: str, overrides: dict[str, str] | None = None):
    project, _binaries, _shim, log, env = fixture
    process = subprocess.run(
        ["/bin/bash", str(project / "scripts/setup-harness.sh"), *args],
        capture_output=True,
        text=True,
        timeout=5,
        env={**env, **(overrides or {})},
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return process, calls


def _pip_installs(calls):
    return [call for call in calls if call["args"][:3] == ["-m", "pip", "install"]]


def _tool_calls(calls, name):
    return [call for call in calls if Path(call["executable"]).name == name]


def test_auto_without_uv_installs_runtime_only_through_pip(setup_harness_project):
    project, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    process, calls = _run(setup_harness_project, "--deps-only")
    assert process.returncode == 0, process.stderr
    assert (project / ".venv/bin/python").is_file()
    installs = _pip_installs(calls)
    assert len(installs) == 1
    assert installs[0]["executable"] == str(project / ".venv/bin/python")
    assert installs[0]["args"] == ["-m", "pip", "install", "--upgrade", f"{project}[agents]"]
    assert not _tool_calls(calls, "uv")
    assert not _tool_calls(calls, "docker")


@pytest.mark.parametrize("local_uv", [False, True])
def test_explicit_pip_ignores_uv_and_preserves_network_configuration(
    setup_harness_project, local_uv
):
    project, binaries, shim, _, _ = setup_harness_project
    _executable(shim, project / ".venv/bin/python")
    _executable(shim, binaries / "uv")
    if local_uv:
        _executable(shim, project / ".venv/bin/uv")
    configuration = {
        "PIP_INDEX_URL": "https://packages.example.invalid/simple",
        "PIP_EXTRA_INDEX_URL": "https://extra.example.invalid/simple",
        "HTTPS_PROXY": "http://proxy.example.invalid:8080",
        "PIP_CERT": "/configured/corporate-ca.pem",
        "REQUESTS_CA_BUNDLE": "/configured/request-ca.pem",
    }
    process, calls = _run(
        setup_harness_project,
        "--installer",
        "pip",
        "--deps-only",
        overrides=configuration,
    )
    assert process.returncode == 0, process.stderr
    assert not _tool_calls(calls, "uv")
    installs = _pip_installs(calls)
    assert len(installs) == 1
    assert installs[0]["args"] == ["-m", "pip", "install", "--upgrade", f"{project}[agents]"]
    assert all(installs[0]["environment"][key] == value for key, value in configuration.items())


def test_pip_failure_stops_before_docker_builds(setup_harness_project):
    _, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    _executable(shim, binaries / "docker")
    process, calls = _run(setup_harness_project, overrides={"MOCK_PIP_EXIT": "7"})
    assert process.returncode == 7
    assert len(_pip_installs(calls)) == 1
    assert [call["args"] for call in _tool_calls(calls, "docker")] == [["info"]]


def test_deps_only_never_calls_available_docker(setup_harness_project):
    _, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    _executable(shim, binaries / "docker")
    process, calls = _run(setup_harness_project, "--deps-only")
    assert process.returncode == 0, process.stderr
    assert not _tool_calls(calls, "docker")


def test_normal_setup_builds_both_images_after_install(setup_harness_project):
    project, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    _executable(shim, binaries / "docker")
    process, calls = _run(setup_harness_project)
    assert process.returncode == 0, process.stderr
    docker_calls = _tool_calls(calls, "docker")
    assert [call["args"] for call in docker_calls] == [
        ["info"],
        [
            "build",
            "--tag",
            "harness-sandbox:local",
            "--file",
            "src/agent_harness/sandbox.Dockerfile",
            "src/agent_harness",
        ],
        [
            "build",
            "--tag",
            "harness-sandbox-java21:local",
            "--file",
            "src/agent_harness/sandbox.java21.Dockerfile",
            "src/agent_harness",
        ],
    ]
    install_index = calls.index(_pip_installs(calls)[0])
    assert all(calls.index(call) > install_index for call in docker_calls[1:])
    assert all(call["cwd"] == str(project) for call in docker_calls)


def test_existing_venv_without_pip_runs_ensurepip(setup_harness_project):
    project, _, shim, _, _ = setup_harness_project
    _executable(shim, project / ".venv/bin/python")
    process, calls = _run(setup_harness_project, "--deps-only", overrides={"MOCK_NO_PIP": "1"})
    assert process.returncode == 0, process.stderr
    arguments = [call["args"] for call in calls]
    assert ["-m", "ensurepip", "--upgrade"] in arguments
    assert arguments.index(["-m", "ensurepip", "--upgrade"]) < calls.index(_pip_installs(calls)[0])
    assert not any(args[:2] == ["-m", "venv"] for args in arguments)


def test_wrong_existing_python_is_not_overwritten(setup_harness_project):
    project, _, shim, _, _ = setup_harness_project
    python = project / ".venv/bin/python"
    _executable(shim, python)
    sentinel = project / ".venv/keep.txt"
    sentinel.write_text("existing environment")
    original_python = python.read_bytes()
    process, calls = _run(
        setup_harness_project, "--deps-only", overrides={"MOCK_PROJECT_VERSION": "3.13"}
    )
    assert process.returncode != 0
    assert "Python 3.13" in process.stderr
    assert "Python 3.12" in process.stderr
    assert sentinel.read_text() == "existing environment"
    assert python.read_bytes() == original_python
    assert not any("install" in call["args"] or "venv" in call["args"] for call in calls)


def test_wrong_bootstrap_python_does_not_create_venv(setup_harness_project):
    project, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    process, calls = _run(
        setup_harness_project, "--deps-only", overrides={"MOCK_PYTHON_VERSION": "3.11"}
    )
    assert process.returncode != 0
    assert "Python 3.12" in process.stderr
    assert not (project / ".venv").exists()
    assert not any("install" in call["args"] or "venv" in call["args"] for call in calls)


@pytest.mark.parametrize("installer", ["auto", "uv"])
@pytest.mark.parametrize("local_uv", [False, True])
def test_available_uv_syncs_locked_dependencies_into_checkout(
    setup_harness_project, installer, local_uv
):
    project, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "uv")
    expected_uv = binaries / "uv"
    if local_uv:
        _executable(shim, project / ".venv/bin/python")
        expected_uv = project / ".venv/bin/uv"
        _executable(shim, expected_uv)
    process, calls = _run(
        setup_harness_project,
        "--installer",
        installer,
        "--deps-only",
        overrides={"VIRTUAL_ENV": "/other/venv", "UV_PROJECT_ENVIRONMENT": "/other/venv"},
    )
    assert process.returncode == 0, process.stderr
    uv_calls = _tool_calls(calls, "uv")
    assert len(uv_calls) == 1
    call = uv_calls[0]
    assert call["executable"] == str(expected_uv)
    assert call["args"] == [
        "sync",
        "--project",
        str(project),
        "--python",
        "3.12",
        "--locked",
        "--inexact",
        "--no-dev",
        "--extra",
        "agents",
    ]
    assert call["environment"]["UV_PROJECT_ENVIRONMENT"] == str(project / ".venv")
    assert call["environment"]["UV_CACHE_DIR"] == str(project / ".uv-cache")
    assert call["environment"]["UV_PYTHON_INSTALL_DIR"] == str(project / ".uv-python")
    assert not _pip_installs(calls)


def test_explicit_uv_missing_reports_pip_alternative_without_bootstrap(setup_harness_project):
    _, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "python3.12")
    process, calls = _run(setup_harness_project, "--installer", "uv", "--deps-only")
    assert process.returncode != 0
    assert "uv не установлен" in process.stderr
    assert "--installer pip" in process.stderr
    assert calls == []


def test_pip_works_without_lock_and_preserves_offline_configuration(setup_harness_project):
    project, binaries, shim, _, _ = setup_harness_project
    (project / "uv.lock").unlink()
    _executable(shim, binaries / "python3.12")
    configuration = {"PIP_NO_INDEX": "1", "PIP_FIND_LINKS": "/configured/wheelhouse"}
    process, calls = _run(
        setup_harness_project,
        "--installer",
        "pip",
        "--deps-only",
        overrides=configuration,
    )
    assert process.returncode == 0, process.stderr
    installs = _pip_installs(calls)
    assert len(installs) == 1
    assert all(installs[0]["environment"][key] == value for key, value in configuration.items())


@pytest.mark.parametrize("installer", ["auto", "uv"])
def test_uv_failure_does_not_fall_back_to_pip(setup_harness_project, installer):
    _, binaries, shim, _, _ = setup_harness_project
    _executable(shim, binaries / "uv")
    _executable(shim, binaries / "python3.12")
    process, calls = _run(
        setup_harness_project,
        "--installer",
        installer,
        "--deps-only",
        overrides={"MOCK_UV_EXIT": "8"},
    )
    assert process.returncode == 8
    assert len(_tool_calls(calls, "uv")) == 1
    assert not _pip_installs(calls)


def test_uv_requires_lock_before_sync(setup_harness_project):
    project, binaries, shim, _, _ = setup_harness_project
    (project / "uv.lock").unlink()
    _executable(shim, binaries / "uv")
    process, calls = _run(setup_harness_project, "--installer", "uv", "--deps-only")
    assert process.returncode != 0
    assert "uv.lock" in process.stderr
    assert "--installer pip" in process.stderr
    assert calls == []
