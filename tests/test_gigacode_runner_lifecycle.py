"""Real child processes exercise cancellation, terminal draining and stream cleanup."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

import corporate_kb.gigacode_runner as module
from corporate_kb.gigacode_runner import (
    GigaCodeCancelled,
    GigaCodeInvalidOutput,
    GigaCodePolicyStop,
    GigaCodeRunner,
    GigaCodeTimedOut,
)


def launcher(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "gigacode-lifecycle"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json,os,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n print('lifecycle-test'); raise SystemExit(0)\n" + body,
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def terminal(**extra: object) -> str:
    return (
        "print("
        + repr(
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": {"content": "answer", "tool_calls": []},
                    **extra,
                }
            )
        )
        + ", flush=True)\n"
    )


def execute(settings_factory, tmp_path: Path, body: str, **kwargs):
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(launcher(tmp_path, body))))
    return runner.run_json(
        checkout=tmp_path,
        prompt="question",
        schema={},
        decision_only=True,
        **kwargs,
    )


def test_large_prompt_can_be_cancelled_while_cli_does_not_read_stdin(settings_factory, tmp_path):
    cancel = threading.Event()
    cancelled: list[float] = []

    def auth(_url: str) -> None:
        cancelled.append(time.monotonic())
        cancel.set()

    executable = launcher(
        tmp_path,
        "print('Open browser https://login.example.test/device', flush=True)\ntime.sleep(20)\n",
    )
    runner = GigaCodeRunner(settings_factory(gigacode_command=str(executable)))
    try:
        with pytest.raises(GigaCodeCancelled):
            runner.run_json(
                checkout=tmp_path,
                prompt="x" * 2_000_000,
                schema={},
                cancel=cancel,
                decision_only=True,
                authentication_url=auth,
            )
        assert cancelled and time.monotonic() - cancelled[0] < 3
    finally:
        cancel.set()


def test_stalled_stdin_obeys_the_same_supervisor_timeout(settings_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_SUPERVISOR_GRACE_SECONDS", 0)
    executable = launcher(tmp_path, "time.sleep(20)\n")
    settings = settings_factory(gigacode_command=str(executable)).model_copy(
        update={"gigacode_timeout_seconds": 0.15}
    )
    started = time.monotonic()
    with pytest.raises(GigaCodeTimedOut):
        GigaCodeRunner(settings).run_json(
            checkout=tmp_path,
            prompt="x" * 2_000_000,
            schema={},
            decision_only=True,
        )
    assert time.monotonic() - started < 3


def test_terminal_answer_is_returned_when_cli_stays_alive(settings_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_RESULT_DRAIN_SECONDS", 0.15)
    started = time.monotonic()
    result = execute(
        settings_factory, tmp_path, "sys.stdin.read()\n" + terminal() + "time.sleep(20)\n"
    )
    assert result.payload["content"] == "answer"
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_completed_parent_does_not_leave_a_child_holding_pipes(
    settings_factory, tmp_path, monkeypatch
):
    monkeypatch.setattr(module, "_RESULT_DRAIN_SECONDS", 0.15)
    child_script = (
        "import signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGINT,signal.SIG_IGN)\n"
        "def finish(*args):\n Path('child-stopped').write_text('yes');raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM,finish)\n"
        "Path('child-started').write_text('yes')\ntime.sleep(20)\n"
    )
    body = (
        "sys.stdin.read()\n"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}])\n"
        "while not Path('child-started').exists():time.sleep(0.01)\n" + terminal()
    )
    started = time.monotonic()
    result = execute(settings_factory, tmp_path, body)
    assert result.payload["content"] == "answer"
    assert (tmp_path / "child-stopped").exists()
    assert time.monotonic() - started < 4


@pytest.mark.parametrize(
    "suffix,pattern",
    [
        ("time.sleep(0.03)\nraise SystemExit(7)\n", "exited with code 7"),
        (terminal(is_error=True, error="late terminal failure"), "late terminal failure"),
        (
            "print(json.dumps({'type':'tool_use','name':'read_file'}),flush=True)\n",
            "native tool event",
        ),
    ],
)
def test_terminal_answer_does_not_hide_later_failure_or_native_tool(
    settings_factory,
    tmp_path,
    suffix,
    pattern,
):
    with pytest.raises(RuntimeError, match=pattern):
        execute(settings_factory, tmp_path, "sys.stdin.read()\n" + terminal() + suffix)


@pytest.mark.parametrize(
    "body",
    [
        "sys.stdin.read()\n",
        "sys.stdin.read()\n" + terminal(result="not JSON"),
        "sys.stdin.read()\n"
        + terminal(subtype="error_max_turns", is_error=True)
        + "raise SystemExit(1)\n",
        "sys.stdin.read()\n" + terminal(stop_reason="max_tokens"),
    ],
)
def test_missing_unusable_and_truncated_results_have_a_recoverable_type(
    settings_factory, tmp_path, body
):
    with pytest.raises(GigaCodeInvalidOutput):
        execute(settings_factory, tmp_path, body)


@pytest.mark.parametrize("event", [
    {"type": "result", "finish_reason": "blacklist", "result": {"tool_calls": []}},
    {"type": "assistant",
     "message": {"content": [{"type": "refusal", "refusal": "upstream-body"}]}},
    {"type": "stream_event", "event": {"type": "message_delta",
                                     "delta": {"stop_reason": "refusal"}}},
    {"type": "stream_event", "event": {"type": "error", "error": {
        "code": "invalid_request", "innererror": {"code": "ResponsibleAIPolicyViolation"},
    }}},
    {"type": "result", "is_error": True,
     "error": {"code": "content_filter", "message": "upstream-body"}},
])
def test_native_policy_stop_discards_other_results_and_keeps_event_private(
    settings_factory, tmp_path, event,
):
    body = "sys.stdin.read()\n" + terminal() + f"print({json.dumps(event)!r}, flush=True)\n"
    progress: list[str] = []
    with pytest.raises(GigaCodePolicyStop) as stopped:
        execute(settings_factory, tmp_path, body + terminal(), progress=progress.append)
    assert "upstream-body" not in str(stopped.value) + "".join(progress)


def test_result_payload_can_describe_filters_and_refusals(settings_factory, tmp_path):
    content = {"finish_reason": "content_filter", "refusal": "Example from the documentation"}
    result = execute(settings_factory, tmp_path, "sys.stdin.read()\n" + terminal(result=content))
    assert result.payload == content


@pytest.mark.parametrize("blocked", [False, True])
def test_native_overload_can_recover_inside_cli_but_subsequent_denial_is_terminal(
    settings_factory, tmp_path, blocked: bool,
):
    events = [{"type": "error", "error": {"code": "rate_limit_exceeded"}}]
    if blocked:
        events.append({"type": "assistant", "error": "blacklist", "message": {}})
    body = "sys.stdin.read()\n" + "".join(
        f"print({json.dumps(event)!r}, flush=True)\n" for event in events) + terminal()
    if blocked:
        with pytest.raises(GigaCodePolicyStop):
            execute(settings_factory, tmp_path, body)
    else:
        assert execute(settings_factory, tmp_path, body).payload["content"] == "answer"


def test_error_event_is_not_a_completed_authentication(settings_factory, tmp_path):
    from corporate_kb.gigacode_runner import GigaCodeTransientError

    callbacks = []
    body = ("sys.stdin.read()\n"
            "print('Open browser https://login.example.test/device', flush=True)\n"
            "print(json.dumps({'type':'assistant','error':'rate_limit','message':{}}))\n"
            "raise SystemExit(1)\n")
    with pytest.raises(GigaCodeTransientError):
        execute(settings_factory, tmp_path, body, authentication_url=lambda _: callbacks.append(1),
                authentication_complete=lambda: callbacks.append(2))
    assert callbacks == [1]


@pytest.mark.parametrize(("error", "expected"), [
    ({"code": "blacklist"}, GigaCodePolicyStop),
    ({"code": 429}, module.GigaCodeTransientError),
])
def test_native_error_on_stderr_preserves_its_type(settings_factory, tmp_path, error, expected):
    message = json.dumps({"type": "error", "error": error})
    body = (f"sys.stdin.read()\nprint({message!r}, file=sys.stderr, flush=True)\n"
            "raise SystemExit(1)\n")
    with pytest.raises(expected):
        execute(settings_factory, tmp_path, body)


def test_truncated_native_message_invalidates_complete_looking_cli_result(
    settings_factory, tmp_path,
):
    event = {"type": "stream_event", "event": {"type": "message_delta",
                                               "delta": {"stop_reason": "max_tokens"}}}
    body = "sys.stdin.read()\n" + f"print({json.dumps(event)!r}, flush=True)\n" + terminal()
    with pytest.raises(GigaCodeInvalidOutput):
        execute(settings_factory, tmp_path, body)


def test_unbounded_output_is_rejected_without_truncating_into_an_answer(
    settings_factory,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(module, "_STREAM_LINE_LIMIT", 1024)
    with pytest.raises(GigaCodeInvalidOutput, match="size limit"):
        execute(
            settings_factory,
            tmp_path,
            "sys.stdin.read()\nprint('x'*10000,flush=True)\ntime.sleep(20)\n",
        )


def test_new_auth_urls_do_not_extend_the_deadline_forever(settings_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_SUPERVISOR_GRACE_SECONDS", 0)
    executable = launcher(
        tmp_path,
        "sys.stdin.read()\n"
        "for i in range(100):\n"
        " print('Open browser https://login.example.test/device?attempt='+str(i),flush=True)\n"
        " time.sleep(0.04)\n",
    )
    settings = settings_factory(gigacode_command=str(executable)).model_copy(
        update={"gigacode_timeout_seconds": 0.15, "gigacode_auth_timeout_seconds": 0.15}
    )
    started = time.monotonic()
    with pytest.raises(GigaCodeTimedOut):
        GigaCodeRunner(settings).run_json(checkout=tmp_path, prompt="question", schema={})
    assert time.monotonic() - started < 3
