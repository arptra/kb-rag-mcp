from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from corporate_kb.dev_debug.recording import SessionStore
from dev_console.controller import DevConsoleController


class FakeWorkflow:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.run: Callable[[dict[str, Any], str], int] = lambda _call, _goal: 0

    def __call__(self, store: SessionStore, session: Path, **options: Any) -> SimpleNamespace:
        call = {"store": store, "session": session, "thread": threading.current_thread(), **options}
        self.calls.append(call)
        return SimpleNamespace(run=lambda goal: self.run(call, goal))


@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DevConsoleController]:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: True)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n")
    (project / ".gitignore").write_text(".cache/\n")
    for args in (("init",), ("add", "."), ("commit", "-m", "Fixture")):
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Console Test",
                "-c",
                "user.email=console@example.invalid",
                *args,
            ],
            cwd=project,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    controller = DevConsoleController(
        project, workflow_factory=FakeWorkflow(), collection_interval=0.02
    )
    yield controller
    controller.close()


def create(console: DevConsoleController) -> str:
    return str(console.create_session("Failing scenario")["session"]["id"])


def wait_pending(console: DevConsoleController) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        request = console.overview()["pending_approval"]
        if isinstance(request, dict):
            return request
        threading.Event().wait(0.005)
    raise AssertionError("Workflow never requested approval")


def with_browser(
    console: DevConsoleController,
    action: Callable[[dict[str, Any]], None],
) -> None:
    def browse() -> None:
        try:
            action(wait_pending(console))
        except BaseException:
            state = console.overview()["repair"]
            if state and state["running"]:
                console.cancel_repair(state["session_id"])
            raise

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(browse)
        assert console.run_next_repair(timeout=0.1)
        future.result(timeout=3)


def test_recording_is_single_and_keeps_http_responsive(
    console: DevConsoleController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collecting, release = threading.Event(), threading.Event()

    def collect(_session: Path) -> dict[str, Any]:
        collecting.set()
        assert release.wait(3)
        return {}

    monkeypatch.setattr(console.store, "collect_once", collect)
    try:
        detail = console.start_recording(label="Observed failure", log_paths=["runtime.log"])
        session_id = detail["session"]["id"]
        assert collecting.wait(1)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(console.overview).result(timeout=1)["recording"]["running"]
        assert console.start_recording(session_id)["recording"]["running"]
        with pytest.raises(RuntimeError, match="Stop the active"):
            console.start_recording()
        assert console.session_detail(session_id)["session"]["extra_log_paths"] == [
            str(console.store.project_root / "runtime.log")
        ]
    finally:
        release.set()
    assert not console.stop_recording(session_id)["recording"]["running"]


def test_notes_bundles_and_artifacts_are_bounded_and_redacted(
    console: DevConsoleController,
) -> None:
    session_id = create(console)
    detail = console.add_note(session_id, "Observed error token=do-not-expose")
    assert "do-not-expose" not in json.dumps(detail)
    bundle = console.prepare_bundle(session_id)
    artifact = console.read_artifact(session_id, bundle["path"])
    assert isinstance(json.loads(artifact["content"]), dict)
    assert "do-not-expose" not in artifact["content"]
    assert bundle["path"] in {item["path"] for item in console.list_artifacts(session_id)}
    session = console.store.root / "sessions" / session_id
    (session / "bundle-999.json").write_text('{"api_key":"hidden","message":"safe"}')
    assert json.loads(console.read_artifact(session_id, "bundle-999.json")["content"]) == {
        "api_key": "[REDACTED]",
        "message": "safe",
    }
    (session / "private.txt").write_text("not an allowed artifact")
    for path in ("../app.py", "private.txt", str(session / "session.json")):
        with pytest.raises(ValueError):
            console.read_artifact(session_id, path)
    (session / "bundle-998.json").symlink_to(console.store.project_root / "app.py")
    with pytest.raises(ValueError, match="symlinks"):
        console.read_artifact(session_id, "bundle-998.json")
    (session / "bundle-997.json").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="bounded"):
        console.read_artifact(session_id, "bundle-997.json")


def test_queue_never_launches_on_http_thread_and_requires_main_thread(
    console: DevConsoleController,
) -> None:
    session_id = create(console)
    fake = console._workflow_factory
    with ThreadPoolExecutor(max_workers=1) as executor:
        queued = executor.submit(console.queue_repair, session_id, "Find the cause", 2).result()
        assert queued["status"] == "queued" and fake.calls == []
        with pytest.raises(RuntimeError, match="main thread"):
            executor.submit(console.run_next_repair, 0.01).result()
    assert console.run_next_repair(0.1)
    assert fake.calls[0]["thread"] is threading.main_thread()
    assert fake.calls[0]["command"] == "gigacode"
    assert fake.calls[0]["max_rounds"] == 2
    assert not fake.calls[0]["cancelled"]()
    assert console.overview()["repair"]["status"] == "completed"
    assert not console.run_next_repair(0.01)


def test_non_tty_keeps_viewing_but_rejects_repair(
    console: DevConsoleController,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = create(console)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert console.overview()["interactive_available"] is False
    with pytest.raises(RuntimeError, match="interactive terminal"):
        console.queue_repair(session_id)
    assert console._workflow_factory.calls == []
    assert console.session_detail(session_id)["session"]["id"] == session_id


def test_one_project_repair_and_cancelled_queue_can_be_replaced(
    console: DevConsoleController,
) -> None:
    first, second = create(console), create(console)
    console.queue_repair(first)
    with pytest.raises(RuntimeError, match="already queued"):
        console.queue_repair(second)
    console.cancel_repair(first)
    console.queue_repair(second)
    assert console.run_next_repair(0.1)
    assert len(console._workflow_factory.calls) == 1
    assert console._workflow_factory.calls[0]["session"].name == second


def test_cancelled_queue_does_not_run_native_workflow(console: DevConsoleController) -> None:
    session_id = create(console)
    console.queue_repair(session_id)
    assert console.cancel_repair(session_id)["status"] == "cancelled"
    assert console.cancel_repair(session_id)["status"] == "cancelled"
    assert console.run_next_repair(0.1)
    assert console._workflow_factory.calls == []


def test_approval_uses_captured_plan_and_consumes_exact_request(
    console: DevConsoleController,
) -> None:
    session_id, other_id = create(console), create(console)
    plan = {"summary": "Captured approved candidate", "acceptance": "Repeat the failure"}
    answered: list[bool] = []

    def run(call: dict[str, Any], _goal: str) -> int:
        call["store"].update(call["session"], status="awaiting_plan_approval", proposed_plan=plan)
        # A changed file must not replace the proposal held by the workflow.
        (call["session"] / "plan.json").write_text('{"summary":"Changed afterwards"}')
        answered.append(call["ask"]("Allow this plan?"))
        return 0

    console._workflow_factory.run = run
    console.queue_repair(session_id)

    def approve(request: dict[str, Any]) -> None:
        assert request["kind"] == "plan" and request["plan"] == plan
        assert answered == []
        for wrong_session, wrong_id in ((other_id, request["id"]), (session_id, "old-request")):
            with pytest.raises(RuntimeError, match="stale"):
                console.approval(wrong_session, wrong_id, True)
        with pytest.raises(ValueError, match="boolean"):
            console.approval(session_id, request["id"], "true")  # type: ignore[arg-type]
        assert console.approval(session_id, request["id"], True)["approved"] is True
        with pytest.raises(RuntimeError, match="stale"):
            console.approval(session_id, request["id"], True)

    with_browser(console, approve)
    assert answered == [True]
    assert console.overview()["pending_approval"] is None
    with pytest.raises(RuntimeError, match="finished"):
        console.cancel_repair(session_id)


@pytest.mark.parametrize("approve", [False, True])
def test_check_approval_shows_exact_commands(console: DevConsoleController, approve: bool) -> None:
    session_id = create(console)
    commands = [{"check": "lint", "argv": ["fixed-python", "-m", "ruff", "check", "src"]}]
    answered: list[bool] = []

    def run(call: dict[str, Any], _goal: str) -> int:
        call["store"].update(
            call["session"], status="awaiting_check_approval", proposed_checks=commands
        )
        answered.append(call["ask"]("Run these checks?"))
        return 0

    console._workflow_factory.run = run
    console.queue_repair(session_id)

    def respond(request: dict[str, Any]) -> None:
        assert request["kind"] == "checks" and request["commands"] == commands
        console.approve_request(request["id"], approve)

    with_browser(console, respond)
    assert answered == [approve]


def test_cancel_pending_approval_is_denial_and_never_terminal_input(
    console: DevConsoleController,
) -> None:
    session_id = create(console)
    answered: list[bool] = []

    def run(call: dict[str, Any], _goal: str) -> int:
        call["store"].update(
            call["session"], status="awaiting_scenario_check", acceptance="Repeat it"
        )
        answered.append(call["ask"]("Did the original scenario pass?"))
        assert call["cancelled"]()
        return 0

    console._workflow_factory.run = run
    console.queue_repair(session_id)

    def cancel(request: dict[str, Any]) -> None:
        assert request["kind"] == "scenario" and request["acceptance"] == "Repeat it"
        assert console.cancel_repair(session_id)["cancellation_requested"]
        with pytest.raises(RuntimeError, match="stale"):
            console.approval(session_id, request["id"], True)

    with_browser(console, cancel)
    assert answered == [False]
    assert console.overview()["repair"]["status"] == "cancelled"


def test_cancel_during_native_only_sets_cooperative_flag(console: DevConsoleController) -> None:
    session_id = create(console)
    entered, release = threading.Event(), threading.Event()

    def run(call: dict[str, Any], _goal: str) -> int:
        entered.set()
        assert release.wait(3)
        assert call["cancelled"]()
        return 0

    console._workflow_factory.run = run
    console.queue_repair(session_id)

    def cancel() -> None:
        try:
            assert entered.wait(2)
            result = console.cancel_repair(session_id)
            assert result["running"] and result["status"] == "running"
        finally:
            release.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(cancel)
        assert console.run_next_repair(0.1)
        future.result(timeout=3)
    assert console.overview()["repair"]["status"] == "cancelled"


def test_launch_failure_is_visible_and_redacted(console: DevConsoleController) -> None:
    session_id = create(console)

    def failure(_call: dict[str, Any], _goal: str) -> int:
        raise RuntimeError("Launch failed token=never-display-this")

    console._workflow_factory.run = failure
    console.queue_repair(session_id)
    assert console.run_next_repair(0.1)
    detail = console.session_detail(session_id)
    assert detail["session"]["status"] == "needs_attention"
    assert detail["fix"]["status"] == "failed"
    assert "never-display-this" not in json.dumps(detail)
    assert detail["pending_approval"] is None


def test_close_wakes_pending_and_is_idempotent(console: DevConsoleController) -> None:
    session_id = create(console)
    answered: list[bool] = []

    def run(call: dict[str, Any], _goal: str) -> int:
        answered.append(call["ask"]("Continue?"))
        return 0

    console._workflow_factory.run = run
    console.queue_repair(session_id)
    with_browser(console, lambda _request: console.close())
    console.close()
    assert console.closed and answered == [False]
    assert console.overview()["pending_approval"] is None
    with pytest.raises(RuntimeError, match="closed"):
        console.create_session()


@pytest.mark.parametrize("max_rounds", [0, 11, True, "3"])
def test_repair_rounds_are_bounded(console: DevConsoleController, max_rounds: Any) -> None:
    with pytest.raises(ValueError, match=r"1\.\.10"):
        console.queue_repair(create(console), max_rounds=max_rounds)
