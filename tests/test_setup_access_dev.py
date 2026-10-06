"""Hermetic runtime-bootstrap shell tests: fake interpreters, no downloads or installs."""

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
with open(os.environ["MOCK_LOG"], "a") as stream:
    stream.write(json.dumps({"executable": str(executable), "args": args,
        "uv_cache": os.environ.get("UV_CACHE_DIR"),
        "python_cache": os.environ.get("UV_PYTHON_INSTALL_DIR"),
        "pip_cache": os.environ.get("PIP_CACHE_DIR")}) + "\n")
def make_venv(destination):
    directory = Path(destination) / "bin"
    directory.mkdir(parents=True)
    shutil.copy2(os.environ["MOCK_SHIM"], directory / "python")
if args[:1] == ["-c"]:
    print(os.environ.get("MOCK_PROJECT_VERSION", "3.12")
        if executable.parent.parent.name == ".venv"
        else os.environ.get("MOCK_PYTHON_VERSION", "3.12"))
elif args[:2] == ["-m", "venv"]:
    if os.environ.get("MOCK_VENV_FAIL") == "1":
        sys.exit(1)
    make_venv(args[2])
elif args[:3] == ["-m", "pip", "--version"]:
    if os.environ.get("MOCK_NO_PIP") == "1" and not (executable.parent / "pip-ready").exists():
        sys.exit(1)
    print("mock pip")
elif args[:2] == ["-m", "ensurepip"]:
    (executable.parent / "pip-ready").touch()
elif args[:3] == ["-m", "pip", "install"]:
    if args[-1] == "uv":
        (executable.parent / "uv-ready").touch()
elif executable.name == "uv" or args[:2] == ["-m", "uv"]:
    if args[:2] == ["-m", "uv"]:
        ready = (executable.parent / "uv-ready").exists()
        if not ready and os.environ.get("MOCK_SYSTEM_UV") != "1":
            sys.exit(1)
        args = args[2:]
    if args[:1] == ["--version"]:
        print("uv mock")
    elif args[:1] == ["venv"]:
        make_venv(args[-1])
    elif args[:2] != ["pip", "install"]:
        sys.exit(2)
else:
    sys.exit(2)
"""


@pytest.fixture
def setup_project(tmp_path):
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("setup-access-dev.sh", "setup-pip.sh"):
        shutil.copy2(source / name, scripts / name)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    # Restrict PATH so the tests can never find the host's real uv or Python.
    for name in ("bash", "dirname"):
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
        ["/bin/bash", str(project / "scripts/setup-access-dev.sh"), *args],
        capture_output=True,
        text=True,
        timeout=5,
        env={**env, **(overrides or {})},
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return process, calls


def test_setup_script_has_valid_bash_syntax(setup_project):
    project = setup_project[0]
    subprocess.run(["/bin/bash", "-n", str(project / "scripts/setup-access-dev.sh")], check=True)


def test_available_python312_creates_private_venv_and_installs_runtime_only(setup_project):
    project, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "python3.12")
    process, calls = _run(setup_project)
    assert process.returncode == 0, process.stderr
    assert (project / ".venv/bin/python").is_file()
    installs = [call for call in calls if call["args"][:3] == ["-m", "pip", "install"]]
    assert len(installs) == 1
    assert installs[0]["args"] == ["-m", "pip", "install", "--upgrade", str(project)]
    assert installs[0]["executable"] == str(project / ".venv/bin/python")
    assert all(call["uv_cache"] == str(project / ".uv-cache") for call in calls)
    assert all(call["python_cache"] == str(project / ".uv-python") for call in calls)
    assert all(call["pip_cache"] == str(project / ".cache/pip") for call in calls)


def test_existing_python312_venv_without_pip_uses_ensurepip_then_runtime_install(setup_project):
    project, _, shim, _, _ = setup_project
    _executable(shim, project / ".venv/bin/python")
    process, calls = _run(setup_project, overrides={"MOCK_NO_PIP": "1"})
    assert process.returncode == 0, process.stderr
    assert ["-m", "ensurepip", "--upgrade"] in [call["args"] for call in calls]
    assert not any(call["args"][:2] == ["-m", "venv"] for call in calls)


@pytest.mark.parametrize("kind", ["wrong-version", "missing-python", "broken-python", "symlink"])
def test_existing_unusable_venv_is_never_overwritten(setup_project, tmp_path, kind):
    project, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "uv")
    venv = project / ".venv"
    overrides = {}
    if kind == "symlink":
        target = tmp_path / "other-venv"
        target.mkdir()
        venv.symlink_to(target, target_is_directory=True)
    else:
        venv.mkdir()
        (venv / "keep.txt").write_text("existing environment")
        if kind == "wrong-version":
            _executable(shim, venv / "bin/python")
            overrides["MOCK_PROJECT_VERSION"] = "3.13"
        elif kind == "broken-python":
            python = venv / "bin/python"
            python.parent.mkdir()
            python.write_text("#!/missing/copied/macos/interpreter\n")
            python.chmod(0o700)
    process, calls = _run(setup_project, overrides=overrides)
    assert process.returncode != 0
    assert "not overwritten" in process.stderr
    assert "Move it aside" in process.stderr
    assert not any("install" in call["args"] or "venv" in call["args"] for call in calls)
    if kind == "symlink":
        assert venv.is_symlink()
        assert list(venv.iterdir()) == []
    else:
        assert (venv / "keep.txt").read_text() == "existing environment"


def test_existing_uv_downloads_managed_python_and_installs_into_target_venv(setup_project):
    project, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "uv")
    process, calls = _run(setup_project)
    assert process.returncode == 0, process.stderr
    uv_calls = [call["args"] for call in calls if Path(call["executable"]).name == "uv"]
    assert uv_calls == [
        ["venv", "--python", "3.12", str(project / ".venv")],
        ["pip", "install", "--python", str(project / ".venv/bin/python"), str(project)],
    ]
    assert not (project / ".cache/access-bootstrap").exists()


def test_system_python_uv_module_is_reused_without_global_pip(setup_project):
    project, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "python3")
    process, calls = _run(setup_project, overrides={"MOCK_SYSTEM_UV": "1"})
    assert process.returncode == 0, process.stderr
    assert any(call["args"][:3] == ["-m", "uv", "venv"] for call in calls)
    assert not any(call["args"][:3] == ["-m", "pip", "install"] for call in calls)
    assert not (project / ".cache/access-bootstrap").exists()


def test_debian_system_python_bootstraps_uv_only_in_private_venv(setup_project):
    project, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "python3")
    process, calls = _run(setup_project)
    assert process.returncode == 0, process.stderr
    bootstrap = project / ".cache/access-bootstrap"
    pip_installs = [call for call in calls if call["args"][:3] == ["-m", "pip", "install"]]
    assert len(pip_installs) == 1
    assert pip_installs[0]["executable"] == str(bootstrap / "bin/python")
    assert pip_installs[0]["args"] == ["-m", "pip", "install", "--upgrade", "uv"]
    assert any(call["args"] == ["-m", "venv", str(bootstrap)] for call in calls)
    assert any(call["args"][:3] == ["-m", "uv", "venv"] for call in calls)
    assert (project / ".venv/bin/python").is_file()


def test_previously_created_bootstrap_uv_is_reused(setup_project):
    project, _, shim, _, _ = setup_project
    bootstrap_python = project / ".cache/access-bootstrap/bin/python"
    _executable(shim, bootstrap_python)
    (bootstrap_python.parent / "uv-ready").touch()
    process, calls = _run(setup_project)
    assert process.returncode == 0, process.stderr
    assert all(call["args"][:2] != ["-m", "pip"] for call in calls)
    assert any(call["args"][:3] == ["-m", "uv", "venv"] for call in calls)


def test_missing_debian_venv_prints_actionable_instructions_without_sudo(setup_project):
    _, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "python3")
    process, calls = _run(setup_project, overrides={"MOCK_VENV_FAIL": "1"})
    assert process.returncode != 0
    assert "sudo apt install python3-venv" in process.stderr
    assert not any(call["args"][:3] == ["-m", "pip", "install"] for call in calls)


def test_invalid_python_bin_override_does_not_silently_replace_it_with_uv(setup_project):
    _, binaries, shim, _, _ = setup_project
    _executable(shim, binaries / "uv")
    process, calls = _run(setup_project, overrides={"PYTHON_BIN": "/missing/python3.12"})
    assert process.returncode != 0
    assert "PYTHON_BIN" in process.stderr
    assert calls == []


def test_custom_python312_override_is_respected(setup_project):
    _, binaries, shim, _, _ = setup_project
    custom = binaries / "custom-python"
    _executable(shim, custom)
    process, calls = _run(setup_project, overrides={"PYTHON_BIN": str(custom)})
    assert process.returncode == 0, process.stderr
    assert any(call["executable"] == str(custom) and "venv" in call["args"] for call in calls)


def test_cache_symlink_is_rejected_without_installs(setup_project, tmp_path):
    project, _, _, _, _ = setup_project
    other = tmp_path / "other-cache"
    other.mkdir()
    (project / ".cache").symlink_to(other, target_is_directory=True)
    process, calls = _run(setup_project)
    assert process.returncode != 0
    assert "symlink" in process.stderr
    assert calls == []
    assert list(other.iterdir()) == []


@pytest.mark.parametrize("arg", ["--help", "--unexpected"])
def test_help_or_unknown_option_never_creates_files(setup_project, arg):
    project = setup_project[0]
    process, calls = _run(setup_project, arg)
    assert process.returncode == (0 if arg == "--help" else 2)
    assert calls == []
    assert not (project / ".venv").exists()
    assert not (project / ".cache").exists()
