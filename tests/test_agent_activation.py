from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from corporate_kb.agents.activation import GigaCodeAgentController
from corporate_kb.gigacode_runner import (
    GigaCodeCancelled,
    GigaCodeModelProfile,
    GigaCodeRunner,
)

_VERSION = "verified-test-launcher 1.0"
_URL = "https://login.example.test/device?code=private-device-secret"
_PROFILE = GigaCodeModelProfile(
    "test-no-tools",
    _VERSION,
    (),
    "Test launcher has no tools, MCP, hooks or ambient context",
)
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
        self.active = self.max_active = 0
        self.lock = threading.Lock()

    def status(self, **kwargs: Any) -> dict[str, Any]:
        self.probes += 1
        return {"available": self.available, "version": self.version, "error": _URL}

    def run_model_json(self, **kwargs: Any) -> Any:
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
                payload={"ready": True}
                if "ready" in kwargs["schema"]["properties"]
                else self.decision,
            )
        finally:
            with self.lock:
                self.active -= 1


def wait_state(controller: GigaCodeAgentController, state: str) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = controller.status()
        if status["state"] == state:
            return status
        time.sleep(0.005)
    pytest.fail(f"Expected {state}; got {controller.status()}")


def activated(settings_factory) -> tuple[GigaCodeAgentController, FakeRunner]:
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
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
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
    assert controller.status()["state"] == "disabled"
    assert controller.status()["available"] is False
    assert runner.probes == 0
    assert not runner.calls
    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(decide(controller))


def test_profile_is_required_before_cli_is_probed(settings_factory):
    runner = FakeRunner()
    controller = GigaCodeAgentController(settings_factory(), runner=runner)
    controller.enable()
    status = wait_state(controller, "error")
    assert "KB_HARNESS_MODEL_PROFILE_PATH" in status["message"]
    assert not status["available"]
    assert runner.probes == 0
    assert not runner.calls


@pytest.mark.parametrize("version,available", [("different", True), (_VERSION, False)])
def test_bad_cli_rejected_before_auth_launch(settings_factory, version, available):
    runner = FakeRunner()
    runner.version, runner.available = version, available
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
    controller.enable()
    status = wait_state(controller, "error")
    assert _URL not in json.dumps(status)
    assert not runner.calls


def test_load_verified_profile_from_server_file(settings_factory, tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "name": _PROFILE.name,
                "cli_version": _VERSION,
                "arguments": [],
                "isolation_notes": _PROFILE.isolation_notes,
            }
        )
    )
    runner = FakeRunner()
    controller = GigaCodeAgentController(
        settings_factory(harness_model_profile_path=path),
        runner=runner,
    )
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    assert runner.calls[0]["profile"] == _PROFILE
    controller.stop()


def test_concurrent_enable_is_idempotent_and_auth_url_is_admin_status_only(
    settings_factory,
    caplog,
):
    runner = FakeRunner()
    runner.block = runner.authenticate = True
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
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
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
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
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
    controller.enable()
    status = wait_state(controller, "error")
    assert "private" not in json.dumps(status)
    assert "private" not in caplog.text
    runner.error = None
    controller.enable()
    assert wait_state(controller, "ready")["available"]
    controller.stop()


def test_invalid_auth_urls_are_ignored(settings_factory):
    runner = FakeRunner()
    runner.block = True
    controller = GigaCodeAgentController(settings_factory(), runner=runner, profile=_PROFILE)
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
    ],
)
def test_invalid_model_output_never_reaches_runtime(settings_factory, decision, tools):
    controller, runner = activated(settings_factory)
    runner.decision = decision
    with pytest.raises(RuntimeError, match="decision failed") as exc:
        asyncio.run(decide(controller, tools=tools))
    assert "private" not in str(exc.value)
    assert "private" not in json.dumps(controller.status())
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
    controller = GigaCodeAgentController(settings_factory(), runner=FakeRunner(), profile=_PROFILE)
    for _ in range(60):
        controller.enable()
        wait_state(controller, "ready")
        controller.disable()
    status = controller.status()
    assert len(status["events"]) == 50
    status["events"][0]["message"] = "mutated"
    assert "mutated" not in json.dumps(controller.status())
    controller.stop()


def test_real_runner_forwards_model_auth_callbacks(settings_factory, tmp_path):
    executable = tmp_path / "fake-gigacode"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"version = {_VERSION!r}\n"
        "if '--version' in sys.argv:\n"
        " print(version); raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type': 'auth', 'url': {_URL!r}}}), flush=True)\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'test'}), flush=True)\n"
        "print(json.dumps({'type':'result','is_error':False,"
        "'result':{'ready':True}}), flush=True)\n"
    )
    executable.chmod(0o755)
    urls, completed = [], []
    result = GigaCodeRunner(settings_factory(gigacode_command=str(executable))).run_model_json(
        prompt='Return {"ready":true}',
        schema={"type": "object"},
        profile=_PROFILE,
        authentication_url=urls.append,
        authentication_complete=lambda: completed.append(True),
    )
    assert result.payload == {"ready": True}
    assert urls == [_URL]
    assert completed == [True]


def test_disable_stops_real_authorization_subprocess(settings_factory, tmp_path):
    executable, stopped = tmp_path / "fake-waiting-cli", tmp_path / "stopped"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json,signal,sys,time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f" print({_VERSION!r}); raise SystemExit(0)\n"
        "def stop(*args):\n"
        f" Path({str(stopped)!r}).write_text('stopped')\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGINT,stop)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "sys.stdin.read()\n"
        f"print(json.dumps({{'type':'auth','url':{_URL!r}}}),flush=True)\n"
        "time.sleep(15)\n"
    )
    executable.chmod(0o755)
    controller = GigaCodeAgentController(
        settings_factory(gigacode_command=str(executable)), profile=_PROFILE,
    )
    controller.enable()
    wait_state(controller, "authentication_required")
    controller.stop()
    assert stopped.read_text() == "stopped"
    assert controller.status()["state"] == "disabled"
    assert controller.status()["authentication_url"] is None
