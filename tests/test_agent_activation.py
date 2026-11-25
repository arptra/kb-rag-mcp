from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from corporate_kb.agents import activation
from corporate_kb.agents.activation import (
    GigaCodeAgentController,
    GigaCodeDecisionError,
    GigaCodeRecoveryPending,
    accepts_decisions,
)
from corporate_kb.gigacode_runner import (
    GigaCodeAccessError,
    GigaCodeCancelled,
    GigaCodeIncompleteOutput,
    GigaCodeInvalidOutput,
    GigaCodePolicyStop,
    GigaCodeProcessCrashed,
    GigaCodeRequestFailed,
    GigaCodeTimedOut,
    GigaCodeTransientError,
)

_VERSION = "corporate-gigacode 26.11"
_URL = "https://login.example.test/device?code=private-device-secret"
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    }
]


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.probes = 0
        self.version = _VERSION
        self.available = True
        self.started = threading.Event()
        self.release = threading.Event()
        self.authenticate = False
        self.block = False
        self.error: Exception | None = None
        self.decision = {"content": "Answer [1]", "tool_calls": []}
        self.probe_payload = {"ready": True}
        self.active = self.max_active = 0
        self.lock = threading.Lock()

    def status(self, **kwargs: Any) -> dict[str, Any]:
        self.probes += 1
        return {"available": self.available, "version": self.version, "error": _URL}

    def run_json(self, **kwargs: Any) -> Any:
        assert kwargs["checkout"].is_dir()
        assert not list(kwargs["checkout"].iterdir())
        assert kwargs["decision_only"] is True
        assert kwargs["allow_edits"] is False
        assert "profile" not in kwargs
        with self.lock:
            self.calls.append(kwargs)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.authenticate:
                kwargs["authentication_url"](_URL)
            while self.block and not self.release.wait(0.005):
                if kwargs["cancel"].is_set():
                    raise GigaCodeCancelled("cancelled " + _URL)
            if self.authenticate:
                kwargs["authentication_complete"]()
            if self.error:
                raise self.error
            return SimpleNamespace(
                payload=self.probe_payload
                if "ready" in kwargs["schema"]["properties"]
                else self.decision,
            )
        finally:
            with self.lock:
                self.active -= 1


def wait_state(
    controller: GigaCodeAgentController, state: str, *, timeout: float = 3,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = controller.status()
        if status["state"] == state:
            return status
        time.sleep(0.005)
    pytest.fail(f"Expected {state}; got {controller.status()}")


def activated(settings_factory) -> tuple[GigaCodeAgentController, FakeRunner]:
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    wait_state(controller, "ready")
    runner.started.clear()
    return controller, runner


async def decide(controller: GigaCodeAgentController, **kwargs: Any) -> dict[str, Any]:
    return await controller.decide(
        messages=[{"role": "user", "content": "Read the document"}],
        tools=kwargs.pop("tools", _TOOLS),
        cancel=kwargs.pop("cancel", threading.Event()),
        max_input_chars=kwargs.pop("max_input_chars", 5000),
        **kwargs,
    )


def test_disabled_on_restart_does_not_probe_or_spawn(settings_factory):
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    assert controller.status()["state"] == "disabled"
    assert controller.status()["available"] is False
    assert runner.probes == 0
    assert not runner.calls
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(decide(controller))


def test_activation_does_not_require_separate_model_profile(settings_factory):
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    status = wait_state(controller, "ready")
    assert status["available"]
    assert len(runner.calls) == 1
    assert "profile" not in runner.calls[0]
    assert not runner.calls[0]["checkout"].exists()
    controller.stop()


@pytest.mark.parametrize("version", ["different-version", "unknown"])
def test_available_cli_does_not_require_pinned_version(settings_factory, version):
    runner = FakeRunner()
    runner.version = version
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    controller.stop()


def test_unavailable_cli_rejected_before_auth_launch(settings_factory):
    runner = FakeRunner()
    runner.available = False
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    status = wait_state(controller, "error")
    assert _URL not in json.dumps(status)
    assert not runner.calls


@pytest.mark.parametrize("failure", [
    {"crash_returncode": -9}, {"probe_timed_out": True}, {"unexpected_exit_code": 2},
])
def test_failed_version_check_recovers_before_first_model_request(
    settings_factory, monkeypatch, failure,
):
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0.01,))
    versions = iter([{"available": False, **failure}, {"available": True}])
    monkeypatch.setattr(runner, "status", lambda **_: next(versions))
    try:
        controller.enable()
        assert wait_state(controller, "ready")["available"]
        assert len(runner.calls) == 1
    finally:
        controller.stop()


def test_auth_completion_does_not_bypass_readiness_validation(settings_factory):
    runner = FakeRunner()
    runner.block = runner.authenticate = True
    runner.probe_payload = {"ready": False}
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    wait_state(controller, "authentication_required")
    runner.calls[0]["authentication_complete"]()
    status = controller.status()
    assert status["state"] == "starting"
    assert not status["available"]
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(decide(controller))
    runner.release.set()
    assert not wait_state(controller, "error")["available"]
    assert not runner.calls[0]["checkout"].exists()
    runner.probe_payload = {"ready": True}
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    controller.stop()


def test_concurrent_enable_is_idempotent_and_auth_url_is_admin_status_only(
    settings_factory,
    caplog,
):
    runner = FakeRunner()
    runner.block = runner.authenticate = True
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(lambda _: controller.enable(), range(30)))
    status = wait_state(controller, "authentication_required")
    assert status["authentication_url"] == _URL
    assert len(runner.calls) == 1
    assert _URL not in json.dumps(status["events"])
    assert _URL not in caplog.text
    assert "progress" not in runner.calls[0]  # CLI progress may contain secrets.
    runner.release.set()
    assert wait_state(controller, "ready")["authentication_url"] is None
    controller.stop()


def test_disable_cancels_and_old_callbacks_cannot_restore_new_generation(settings_factory):
    runner = FakeRunner()
    runner.block = runner.authenticate = True
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    wait_state(controller, "authentication_required")
    old = runner.calls[0]
    assert controller.disable()["authentication_url"] is None
    assert old["cancel"].is_set()
    old["authentication_url"](_URL)
    old["authentication_complete"]()
    assert controller.status()["state"] == "disabled"
    controller.enable()
    runner.release.set()
    wait_state(controller, "ready")
    old["authentication_url"](_URL)
    old["authentication_complete"]()
    assert controller.status()["state"] == "ready"
    assert controller.status()["authentication_url"] is None
    assert runner.max_active == 1
    assert len(runner.calls) == 2
    controller.stop()


def test_failure_redacts_exception_and_can_be_retried(settings_factory, caplog):
    runner = FakeRunner()
    runner.error = RuntimeError("Bearer private-token " + _URL)
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    status = wait_state(controller, "error")
    assert "private" not in json.dumps(status)
    assert "private" not in caplog.text
    assert not runner.calls[0]["checkout"].exists()
    runner.error = None
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    controller.stop()


def test_invalid_auth_urls_are_ignored(settings_factory):
    runner = FakeRunner()
    runner.block = True
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    assert runner.started.wait(2)
    callback = runner.calls[0]["authentication_url"]
    for url in ["javascript:alert(1)", "https://user:password@example.test/auth", "https://x/\n"]:
        callback(url)
        assert controller.status()["authentication_url"] is None
    controller.stop()


def test_decision_schema_tools_and_context_are_enforced(settings_factory):
    controller, runner = activated(settings_factory)
    runner.decision = {
        "content": "",
        "tool_calls": [{"name": "read_document", "args": {"id": "1"}}],
    }
    result = asyncio.run(decide(controller))
    assert result == runner.decision
    assert "GIGACODE_AGENT_REQUEST" in runner.calls[-1]["prompt"]
    assert runner.calls[0]["checkout"] != runner.calls[1]["checkout"]
    assert all(not call["checkout"].exists() for call in runner.calls)
    with pytest.raises(ValueError, match="character limit"):
        asyncio.run(decide(controller, max_input_chars=20))
    assert len(runner.calls) == 2
    controller.stop()


@pytest.mark.parametrize(
    "decision,tools",
    [
        ({"content": "", "tool_calls": [{"name": "shell", "args": {}}]}, _TOOLS),
        ({"content": "", "tool_calls": [{"name": "read_document", "args": {"id": 1}}]}, _TOOLS),
        ({"content": "", "tool_calls": [{"name": "read_document", "args": {"id": "1"}}]}, []),
        ({"content": "private-result", "tool_calls": [], "extra": "private"}, _TOOLS),
        ({"content": "   ", "tool_calls": []}, _TOOLS),
    ],
)
def test_invalid_model_output_never_reaches_runtime(settings_factory, decision, tools):
    controller, runner = activated(settings_factory)
    runner.decision = decision
    with pytest.raises(ValueError, match="invalid tool decision") as exc:
        asyncio.run(decide(controller, tools=tools))
    assert "private" not in str(exc.value)
    assert "private" not in json.dumps(controller.status())
    assert controller.status()["available"]
    runner.decision = {"content": "Corrected answer", "tool_calls": []}
    assert asyncio.run(decide(controller))["content"] == "Corrected answer"
    controller.stop()


@pytest.mark.parametrize(("error", "reason"), [
    (GigaCodeInvalidOutput(_URL), "format"), (GigaCodeIncompleteOutput(_URL), "length"),
])
def test_controller_preserves_the_model_correction_reason(settings_factory, error, reason, caplog):
    controller, runner = activated(settings_factory)
    runner.error = error
    with pytest.raises(GigaCodeDecisionError) as caught:
        asyncio.run(decide(controller))
    assert caught.value.reason == reason and _URL not in str(caught.value)
    assert _URL not in caplog.text and "phase=decision" in caplog.text
    assert controller.status()["available"] and controller.status()["state"] == "ready"
    runner.error = None
    assert asyncio.run(decide(controller)) == runner.decision
    controller.stop()


@pytest.mark.parametrize("failure,cause", [
    (GigaCodeTimedOut(_URL), "timeout"),
    (GigaCodeRequestFailed(reason="unexpected-exit", returncode=2), "unexpected-exit"),
])
def test_interrupted_decision_rechecks_process_before_resuming(
    settings_factory, monkeypatch, caplog, failure, cause,
):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0.1,))
    runner.error = failure
    try:
        with pytest.raises(type(failure)) as caught:
            asyncio.run(decide(controller))
        assert controller.status()["state"] == "recovering"
        assert f"phase=decision cause={cause}" in caplog.text
        assert _URL not in str(caught.value) and _URL not in caplog.text
        runner.error = None
        assert wait_state(controller, "ready")["available"]
        assert asyncio.run(decide(controller)) == runner.decision
    finally:
        controller.stop()


@pytest.mark.parametrize("error", [GigaCodeInvalidOutput(_URL), GigaCodeTimedOut(_URL),
                                   GigaCodeTransientError()])
def test_incomplete_reply_during_pending_authentication_requires_reauthorization(
    settings_factory, monkeypatch, error,
):
    controller, runner = activated(settings_factory)

    def pending_auth(**kwargs):
        kwargs["authentication_url"](_URL)
        raise error

    monkeypatch.setattr(runner, "run_json", pending_auth)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(decide(controller))
    assert _URL not in str(caught.value)
    assert controller.status()["state"] == "error"
    assert not controller.status()["available"]
    controller.stop()


def test_provider_denies_account_and_controller_reports_access_issue(settings_factory):
    controller, runner = activated(settings_factory)
    runner.error = GigaCodeAccessError(status=403)
    with pytest.raises(GigaCodeAccessError) as failure:
        asyncio.run(decide(controller))
    assert failure.value.status == 403
    status = controller.status()
    assert status["state"] == "error" and "доступ" in status["message"]
    controller.stop()


def test_reauthentication_during_real_decision(settings_factory):
    controller, runner = activated(settings_factory)
    runner.block = runner.authenticate = True

    async def run():
        operation = asyncio.create_task(decide(controller))
        await asyncio.to_thread(runner.started.wait, 2)
        await asyncio.to_thread(wait_state, controller, "authentication_required")
        assert controller.status()["authentication_url"] == _URL
        runner.release.set()
        assert await operation == runner.decision

    asyncio.run(run())
    assert controller.status()["state"] == "ready"
    assert controller.status()["authentication_url"] is None
    controller.stop()


@pytest.mark.parametrize("cancel_mode", ["external", "asyncio", "disable"])
def test_cancellation_stops_running_model(settings_factory, cancel_mode):
    controller, runner = activated(settings_factory)
    runner.block = True
    external = threading.Event()

    async def run():
        operation = asyncio.create_task(decide(controller, cancel=external))
        assert await asyncio.to_thread(runner.started.wait, 2)
        if cancel_mode == "external":
            external.set()
        elif cancel_mode == "disable":
            controller.disable()
        else:
            operation.cancel()
        with pytest.raises((GigaCodeCancelled, asyncio.CancelledError)):
            await operation

    asyncio.run(run())
    assert runner.active == 0
    assert controller.status()["state"] == ("disabled" if cancel_mode == "disable" else "ready")
    controller.stop()


def test_queued_model_calls_do_not_spawn_parallel_cli(settings_factory):
    controller, runner = activated(settings_factory)
    runner.block = True

    async def run():
        first = asyncio.create_task(decide(controller))
        assert await asyncio.to_thread(runner.started.wait, 2)
        second = asyncio.create_task(decide(controller))
        await asyncio.sleep(0.06)
        assert len(runner.calls) == 2  # Activation plus first decision.
        controller.disable()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(outcome, GigaCodeCancelled) for outcome in outcomes)

    asyncio.run(run())
    assert runner.max_active == 1
    assert len(runner.calls) == 2
    controller.stop()


@pytest.mark.parametrize("async_cancel", [False, True])
def test_cancel_pending_authentication_does_not_claim_ready(settings_factory, async_cancel):
    controller, runner = activated(settings_factory)
    runner.block = runner.authenticate = True
    external = threading.Event()

    async def run():
        operation = asyncio.create_task(decide(controller, cancel=external))
        await asyncio.to_thread(wait_state, controller, "authentication_required")
        if async_cancel:
            operation.cancel()
        else:
            external.set()
        with pytest.raises((GigaCodeCancelled, asyncio.CancelledError)):
            await operation

    asyncio.run(run())
    status = controller.status()
    assert status["state"] == "error"
    assert status["available"] is False
    assert status["authentication_url"] is None
    runner.block = runner.authenticate = False
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    controller.stop()


def test_event_history_is_bounded_and_snapshot_is_detached(settings_factory):
    controller = GigaCodeAgentController(settings_factory(), runner=FakeRunner())
    for _ in range(60):
        controller.enable()
        wait_state(controller, "ready")
        controller.disable()
    status = controller.status()
    assert len(status["events"]) == 50
    status["events"][0]["message"] = "mutated"
    assert "mutated" not in json.dumps(controller.status())
    controller.stop()


def test_crashing_readiness_probe_keeps_recovering_with_capped_delay(settings_factory, monkeypatch):
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0.001, 0.002))
    runner = FakeRunner()
    run = runner.run_json
    failures = iter([GigaCodeProcessCrashed(-9) for _ in range(4)])

    def eventually_ready(**kwargs):
        runner.error = next(failures, None)
        return run(**kwargs)

    monkeypatch.setattr(runner, "run_json", eventually_ready)
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    try:
        controller.enable()
        ready = wait_state(controller, "ready")
        assert len(runner.calls) == runner.probes == 5
        assert runner.max_active == 1
        assert any(e["state"] == "recovering" for e in ready["events"])
        assert asyncio.run(decide(controller)) == runner.decision
    finally:
        controller.stop()


def test_malformed_readiness_reply_gets_one_corrected_probe(settings_factory, monkeypatch):
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0,))
    runner = FakeRunner()
    runner.error = GigaCodeInvalidOutput(_URL)
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    try:
        controller.enable()
        snapshot = wait_state(controller, "error")
        assert not snapshot["available"] and _URL not in json.dumps(snapshot)
        assert len(runner.calls) == 2
        assert runner.calls[0]["prompt"] != runner.calls[1]["prompt"]
    finally:
        controller.stop()


@pytest.mark.parametrize("probe_error", [
    GigaCodeIncompleteOutput("length"), GigaCodeInvalidOutput("bad JSON"), GigaCodeRequestFailed(),
])
def test_process_restart_survives_failed_readiness_replies(
    settings_factory, monkeypatch, probe_error,
):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0.001,))
    run = runner.run_json
    failures = iter([GigaCodeProcessCrashed(-9), probe_error, probe_error])

    def respond(**kwargs):
        runner.error = next(failures, None)
        return run(**kwargs)

    monkeypatch.setattr(runner, "run_json", respond)
    try:
        with pytest.raises(GigaCodeProcessCrashed):
            asyncio.run(decide(controller))
        assert wait_state(controller, "ready")["available"]
        assert runner.probes == 4  # Initial activation and three shared recovery checks.
        assert asyncio.run(decide(controller)) == runner.decision
    finally:
        controller.stop()


async def test_one_shared_restart_serves_queued_and_new_decisions(settings_factory, monkeypatch,
                                                                 caplog):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0,))
    run = runner.run_json
    crash_started, recovery_started, release = (threading.Event() for _ in range(3))
    crash = threading.Event()
    slot = threading.Lock()
    invocations = []

    def scripted(**kwargs):
        assert slot.acquire(blocking=False)
        try:
            is_probe = "ready" in kwargs["schema"]["properties"]
            invocations.append("probe" if is_probe else "decision")
            if len(invocations) == 1:
                crash_started.set()
                assert crash.wait(3)
                raise GigaCodeProcessCrashed(-9)
            if is_probe:
                recovery_started.set()
                assert release.wait(3)
            return run(**kwargs)
        finally:
            slot.release()

    monkeypatch.setattr(runner, "run_json", scripted)
    try:
        first = asyncio.create_task(decide(controller))
        assert await asyncio.to_thread(crash_started.wait, 2)
        queued = asyncio.create_task(decide(controller))
        await asyncio.sleep(0.02)
        crash.set()
        with pytest.raises(GigaCodeProcessCrashed):
            await first
        assert "phase=decision cause=process-crash exit_code=-9 signal=9" in caplog.text
        assert await asyncio.to_thread(recovery_started.wait, 2)
        snapshot = controller.status()
        assert not snapshot["available"] and accepts_decisions(snapshot)
        newcomers = [asyncio.create_task(decide(controller)) for _ in range(2)]
        await asyncio.sleep(0.02)
        assert invocations == ["decision", "probe"]
        release.set()
        assert await asyncio.gather(queued, *newcomers) == [runner.decision] * 3
        assert invocations == ["decision", "probe", "decision", "decision", "decision"]
        assert controller.status()["available"]
    finally:
        release.set()
        crash.set()
        controller.stop()


async def test_disable_and_cancel_during_restart_backoff(settings_factory, monkeypatch):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (60,))
    runner.error = GigaCodeProcessCrashed(1)
    try:
        with pytest.raises(GigaCodeProcessCrashed):
            await decide(controller)
        waiter = asyncio.create_task(decide(controller))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert controller.status()["state"] == "recovering"
        second = asyncio.create_task(decide(controller))
        await asyncio.sleep(0.02)
        controller.disable()
        with pytest.raises(GigaCodeCancelled):
            await second
        assert not accepts_decisions(controller.status())
        with pytest.raises(RuntimeError, match="not ready"):
            await decide(controller)
        assert len(runner.calls) == 2
    finally:
        controller.stop()


async def test_expired_wait_leaves_background_restart_running(settings_factory, monkeypatch):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0.2,))
    monkeypatch.setattr(controller, "_recovery_wait", 0.02)
    runner.error = GigaCodeProcessCrashed()
    try:
        with pytest.raises(GigaCodeProcessCrashed):
            await decide(controller)
        with pytest.raises(GigaCodeRecoveryPending):
            await decide(controller)
        assert controller.status()["state"] == "recovering"
        runner.error = None
        assert (await asyncio.to_thread(wait_state, controller, "ready"))["available"]
        assert await decide(controller) == runner.decision
    finally:
        controller.stop()


@pytest.mark.parametrize("end", [GigaCodeAccessError(status=401), GigaCodePolicyStop()])
def test_recovery_probe_respects_provider_stop(settings_factory, monkeypatch, end):
    controller, runner = activated(settings_factory)
    monkeypatch.setattr(activation, "_RESTART_DELAYS", (0,))
    failures = iter([GigaCodeProcessCrashed(), end])
    run = runner.run_json

    def stopped(**kwargs):
        runner.error = next(failures)
        return run(**kwargs)

    monkeypatch.setattr(runner, "run_json", stopped)
    try:
        with pytest.raises(GigaCodeProcessCrashed):
            asyncio.run(decide(controller))
        assert not accepts_decisions(wait_state(controller, "error"))
        assert len(runner.calls) == 3
    finally:
        controller.stop()


@pytest.mark.parametrize("failure", [GigaCodeRequestFailed(), RuntimeError(_URL)])
def test_one_failed_decision_leaves_controller_ready(settings_factory, failure):
    controller, runner = activated(settings_factory)
    try:
        runner.error = failure
        with pytest.raises(type(failure)) as caught:
            asyncio.run(decide(controller))
        assert _URL not in str(caught.value)
        assert controller.status()["available"]
        runner.error = None
        assert asyncio.run(decide(controller)) == runner.decision
        assert runner.probes == 1
    finally:
        controller.stop()


def test_real_cli_activation_authentication_and_decision_without_profile(
    settings_factory, tmp_path,
):
    executable = tmp_path / "fake-gigacode"
    capture, authorized = tmp_path / "calls.jsonl", tmp_path / "authorized"
    executable.write_text(
        f"#!{sys.executable} -IS\n"
        "import json,os,sys,time\n"
        "from pathlib import Path\n"
        f"version = {_VERSION!r}\n"
        "if '--version' in sys.argv:\n"
        " print(version); raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        f"with Path({str(capture)!r}).open('a') as capture:\n"
        " capture.write(json.dumps({'cwd':os.getcwd(),'args':sys.argv[1:]})+'\\n')\n"
        f"print(json.dumps({{'type':'auth','url':{_URL!r}}}),flush=True)\n"
        f"while not Path({str(authorized)!r}).exists(): time.sleep(0.005)\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'test'}),flush=True)\n"
        "payload = {'content':'Verified answer [1]','tool_calls':[]} "
        "if 'GIGACODE_AGENT_REQUEST' in prompt else {'ready':True}\n"
        "print(json.dumps({'type':'result','is_error':False,'result':payload}),flush=True)\n"
    )
    executable.chmod(0o755)
    settings = settings_factory(gigacode_command=str(executable), gigacode_max_session_turns=7)
    controller = GigaCodeAgentController(settings)
    controller.enable()
    try:
        # The real runner starts two --version processes before the auth CLI.
        # Its five-second version deadline can exceed the fake-runner test wait.
        waiting = wait_state(controller, "authentication_required", timeout=15)
        assert waiting["authentication_url"] == _URL
        assert not waiting["available"]
        authorized.touch()
        assert wait_state(controller, "ready")["authentication_url"] is None
        assert asyncio.run(decide(controller)) == {
            "content": "Verified answer [1]", "tool_calls": [],
        }
    finally:
        controller.stop()
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]["cwd"] != calls[1]["cwd"]
    for call in calls:
        assert not Path(call["cwd"]).exists()
        assert call["args"] == [
            "--output-format", "stream-json", "--exclude-tools",
            "shell,write,edit,agent,web_fetch,web_search", "--max-session-turns", "7",
        ]


@pytest.mark.parametrize("outcome", ["cancel", "error"])
def test_authorization_subprocess_and_temporary_checkout_are_cleaned(
    settings_factory, tmp_path, outcome,
):
    executable, stopped = tmp_path / "fake-waiting-cli", tmp_path / "stopped"
    checkout = tmp_path / "checkout.txt"
    executable.write_text(
        f"#!{sys.executable} -IS\n"
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f" print({_VERSION!r}); raise SystemExit(0)\n"
        "def stop(*args):\n"
        f" Path({str(stopped)!r}).write_text('stopped')\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT,stop)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "sys.stdin.read()\n"
        f"Path({str(checkout)!r}).write_text(os.getcwd())\n"
        f"print(json.dumps({{'type':'auth','url':{_URL!r}}}),flush=True)\n"
        f"if {outcome!r} == 'error': raise SystemExit(7)\n"
        "time.sleep(15)\n"
    )
    executable.chmod(0o755)
    controller = GigaCodeAgentController(settings_factory(gigacode_command=str(executable)))
    controller.enable()
    if outcome == "cancel":
        wait_state(controller, "authentication_required")
        controller.stop()
        assert stopped.read_text() == "stopped"
        assert controller.status()["state"] == "disabled"
    else:
        assert not wait_state(controller, "error")["available"]
        controller.stop()
    assert not Path(checkout.read_text()).exists()
    assert controller.status()["authentication_url"] is None
