"""Explicit commands are observed without leaking secrets or leaving workers running."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from corporate_kb.dev_debug import process as runner
from corporate_kb.dev_debug.recording import SessionStore


def _recording(tmp_path: Path) -> tuple[SessionStore, Path]:
    store = SessionStore(tmp_path)
    return store, store.create("process test")


def _script(tmp_path: Path, source: str) -> list[str]:
    path = tmp_path / "test-command.py"
    path.write_text(source, encoding="utf-8")
    return [sys.executable, "-u", str(path)]


def test_logged_process_sanitizes_split_secrets_pem_controls_and_partial_eof(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, session = _recording(tmp_path)
    argv = _script(
        tmp_path,
        """import os, sys, time
parts = [
    b'Author', b'ization: Bearer split-header-value\\n',
    b'pass\\x1b[31mword=split-password-value\\x1b[0m\\n',
    b'-----BE', b'GIN PRIVATE KEY-----\\n', b'short-private-key-body\\n',
    b'-----END PRIVATE KEY-----\\n',
    b'https://name:credential-value@git.example/repo?token=query-value\\n',
    b'\\x1b]0;evil title\\x07fatal: missing ref main\\n',
    b'token=partial-eof-value',
]
for part in parts:
    os.write(1, part)
    time.sleep(0.005)
""",
    )
    result = runner.run_logged(store, session, argv, timeout=3)
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert result["argv"] == argv
    output = capsys.readouterr().out
    assert result["output_sha256"] == hashlib.sha256(output.encode()).hexdigest()
    assert result["output_tail"] == output
    persisted = (session / "events.jsonl").read_text(encoding="utf-8")
    for observed in (output, persisted):
        assert "fatal: missing ref main" in observed
        assert "https://git.example/repo" in observed
        assert "REDACTED PRIVATE KEY" in observed
        for secret in (
            "split-header-value", "split-password-value", "short-private-key-body",
            "credential-value", "query-value", "partial-eof-value", "evil title",
        ):
            assert secret not in observed
    assert "\x1b" not in output
    assert "\x07" not in output


def test_process_obeys_cwd_env_devnull_and_literal_arguments_reports_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, session = _recording(tmp_path)
    argv = _script(
        tmp_path,
        """import os, sys
assert sys.stdin.read() == ''
print('cwd=' + os.getcwd())
print('setting=' + os.environ['DEV_TEST_SETTING'])
print('argument=' + sys.argv[1])
print('diagnostic on stderr', file=sys.stderr)
sys.exit(7)
""",
    )
    argv.append("; touch unexpected-output")
    result = runner.run_logged(
        store, session, argv, env={**os.environ, "DEV_TEST_SETTING": "provided"}, timeout=3
    )
    assert result["returncode"] == 7
    assert result["timed_out"] is False
    output = capsys.readouterr().out
    assert f"cwd={tmp_path}" in output
    assert "setting=provided" in output
    assert "argument=; touch unexpected-output" in output
    assert "diagnostic on stderr" in output
    assert not (tmp_path / "unexpected-output").exists()


def test_oversized_line_discard_keeps_following_pem_body_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, session = _recording(tmp_path)
    argv = _script(
        tmp_path,
        """import sys
print('oversized-secret-' * 2000 + '-----BEGIN PRIVATE KEY-----')
print('short-body-after-oversized-line')
print('-----END PRIVATE KEY-----')
print('useful next diagnostic')
""",
    )
    result = runner.run_logged(store, session, argv, timeout=3)
    assert result["returncode"] == 0
    output = capsys.readouterr().out
    assert "discarded oversized output line" in output
    assert "useful next diagnostic" in output
    assert "oversized-secret" not in output
    assert "short-body-after-oversized-line" not in output
    assert "short-body-after-oversized-line" not in (session / "events.jsonl").read_text()


def test_timeout_stops_process_and_descendant(tmp_path: Path) -> None:
    store, session = _recording(tmp_path)
    marker = tmp_path / "child-survived"
    argv = _script(
        tmp_path,
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', "
        + repr(
            "import time; from pathlib import Path; time.sleep(.8); "
            f"Path({str(marker)!r}).touch()"
        )
        + "])\nprint('worker ready', flush=True)\ntime.sleep(10)\n",
    )
    started = time.monotonic()
    result = runner.run_logged(store, session, argv, timeout=0.15)
    assert result["timed_out"] is True
    assert result["returncode"] != 0
    assert time.monotonic() - started < 3
    time.sleep(0.85)
    assert not marker.exists()


def test_keyboard_interrupt_kills_worker_and_is_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _recording(tmp_path)
    argv = _script(tmp_path, "import time\ntime.sleep(10)\n")
    real_popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def remember(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)  # type: ignore[call-overload]
        children.append(child)
        return child

    class InterruptedSelector:
        def __enter__(self) -> InterruptedSelector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def register(self, *_args: object) -> None:
            return None

        def get_map(self) -> dict[int, bool]:
            return {1: True}

        def select(self, **_kwargs: object) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(runner.subprocess, "Popen", remember)
    monkeypatch.setattr(runner.selectors, "DefaultSelector", InterruptedSelector)
    with pytest.raises(KeyboardInterrupt):
        runner.run_logged(store, session, argv)
    assert len(children) == 1
    assert children[0].poll() in {-signal.SIGTERM, -signal.SIGKILL}
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "process_interrupted" for event in events)


def test_argv_diagnostics_remove_secret_values() -> None:
    assert runner._safe_argv(
        ["command", "--token", "flag-value", "--password=inline-value", "ordinary"]
    ) == ["command", "--token", "[REDACTED]", "--password=[REDACTED]", "ordinary"]


def test_artifact_failure_does_not_mask_command_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _recording(tmp_path)
    argv = _script(tmp_path, "print('actual diagnostic')\nraise SystemExit(8)\n")

    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "event", unavailable)
    result = runner.run_logged(store, session, argv, timeout=3)
    assert result["returncode"] == 8
    assert result["recording_errors"] == ["Artifact recording failed: OSError"]
    assert "actual diagnostic" in result["output_tail"]


def test_cooperative_cancellation_stops_descendant(tmp_path: Path) -> None:
    store, session = _recording(tmp_path)
    marker = tmp_path / "cancelled-child-survived"
    child = (
        "import time; from pathlib import Path; time.sleep(.8); "
        f"Path({str(marker)!r}).touch()"
    )
    argv = _script(
        tmp_path,
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "print('worker ready', flush=True)\ntime.sleep(10)\n",
    )
    started = time.monotonic()
    result = runner.run_logged(
        store, session, argv, timeout=3, cancelled=lambda: time.monotonic() - started > .15
    )
    assert result["cancelled"] is True
    assert result["timed_out"] is False
    assert result["returncode"] != 0
    time.sleep(.85)
    assert not marker.exists()
    events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "process_cancelled" for event in events)


def test_already_cancelled_does_not_spawn(tmp_path: Path) -> None:
    store, session = _recording(tmp_path)
    result = runner.run_logged(store, session, ["nonexistent-command"], cancelled=lambda: True)
    assert result["cancelled"] is True
    assert result["returncode"] == 130


def test_cancellation_is_explicit_even_when_child_exits_zero(tmp_path: Path) -> None:
    store, session = _recording(tmp_path)
    argv = _script(
        tmp_path,
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "print('ready', flush=True)\ntime.sleep(10)\n",
    )
    started = time.monotonic()
    result = runner.run_logged(
        store, session, argv, timeout=3, cancelled=lambda: time.monotonic() - started > .2
    )
    assert result["returncode"] == 0
    assert result["cancelled"] is True
