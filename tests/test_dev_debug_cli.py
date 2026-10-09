"""CLI recording flows use local processes and never invoke a model or server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from corporate_kb.dev_debug import cli, workflow
from corporate_kb.dev_debug.recording import SessionStore


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    for args in (
        ("init", "--quiet"),
        ("config", "user.name", "Dev Test"),
        ("config", "user.email", "dev@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / ".gitignore").write_text(".cache/\n*.log\n")
    (root / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "Initial",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return root


def test_run_creates_session_captures_stderr_and_sanitizes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = cli.main(
        [
            "--project",
            str(project),
            "run",
            "--label",
            "clone test",
            "--",
            sys.executable,
            "-c",
            "import os,sys; print('ready'); print('ERROR password=cli-hidden',file=sys.stderr); "
            "print('capture=' + os.environ['KB_DEV_DEBUG_CAPTURE'])",
        ]
    )
    assert result == 0
    output = capsys.readouterr().out
    assert "ready" in output
    assert "capture=1" in output
    assert "ERROR password=[REDACTED]" in output
    assert "cli-hidden" not in output
    store = SessionStore(project)
    session = store.latest()
    assert session is not None
    state = store.load(session)
    assert state["label"] == "clone test"
    assert state["last_process"]["returncode"] == 0
    persisted = (session / "events.jsonl").read_text()
    assert "ready" in persisted and "ERROR password=" in persisted
    assert "cli-hidden" not in persisted


def test_note_bundle_and_status_preserve_user_observation(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SessionStore(project)
    session = store.create("source import")
    assert (
        cli.main(
            [
                "--project",
                str(project),
                "note",
                "Clone fails on branch main; password=hidden-note",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["--project", str(project), "bundle", "--note", "Expected skill list"]) == 0
    bundle_path = Path(capsys.readouterr().out.strip())
    bundle = json.loads(bundle_path.read_text())
    assert bundle["note"] == "Expected skill list"
    assert "Clone fails on branch main" in bundle["session_notes"][0]["text"]
    assert "hidden-note" not in bundle_path.read_text()
    store.update(session, status="awaiting_plan_approval", message="Inspect proposed fix")
    assert cli.main(["--project", str(project), "status"]) == 0
    output = capsys.readouterr().out
    assert "awaiting_plan_approval" in output
    assert "Inspect proposed fix" in output
    assert "hidden-note" not in output


def test_custom_log_relative_to_project_persists_into_bundle(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    custom = project / "custom.log"
    custom.write_text("ERROR custom repository clone\n")
    assert (
        cli.main(
            [
                "--project",
                str(project),
                "run",
                "--log",
                "custom.log",
                "--",
                sys.executable,
                "-c",
                "print('foreground done')",
            ]
        )
        == 0
    )
    capsys.readouterr()
    store = SessionStore(project)
    session = store.latest()
    assert session is not None
    assert store.load(session)["extra_log_paths"] == [str(custom)]
    assert cli.main(["--project", str(project), "bundle"]) == 0
    bundle_path = Path(capsys.readouterr().out.strip())
    assert "custom repository clone" in bundle_path.read_text()
    assert str(custom) in store.load(session)["log_offsets"]


def test_fix_without_tty_refuses_before_model_invocation(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SessionStore(project)
    store.create("clone failure")

    def forbidden(*args: object, **kwargs: object) -> int:
        pytest.fail("Noninteractive fix must not launch GigaCode")

    monkeypatch.setattr(workflow, "run_interactive", forbidden)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["--project", str(project), "fix", "--goal", "Fix clone"]) == 2
    assert "interactive terminal" in capsys.readouterr().err


def test_invalid_session_path_cannot_create_artifacts_outside_store(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SessionStore(project)
    store.create()
    assert (
        cli.main(
            [
                "--project",
                str(project),
                "note",
                "--session",
                str(project),
                "must not write",
            ]
        )
        == 2
    )
    assert "directly below" in capsys.readouterr().err
    assert not (project / "events.jsonl").exists()
    assert not (project / ".lock").exists()


def test_watch_interrupt_preserves_repair_status_and_records_stop(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SessionStore(project)
    session = store.create()
    store.update(session, status="awaiting_plan_approval")

    def interrupted(self: SessionStore, *args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(SessionStore, "collect_once", interrupted)
    assert cli.main(["--project", str(project), "watch"]) == 130
    assert store.load(session)["status"] == "awaiting_plan_approval"
    assert "watch_stopped" in (session / "events.jsonl").read_text()
    assert "артефакты сохранены" in capsys.readouterr().out
