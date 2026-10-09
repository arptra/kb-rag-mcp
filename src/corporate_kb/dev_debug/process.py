"""Run an explicitly selected command and record bounded, sanitized output."""

from __future__ import annotations

import hashlib
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from corporate_kb.dev_debug.recording import SessionStore, redact_text

_MAX_LINE_BYTES = 16 * 1024
_READ_BYTES = 4096
_PEM_BEGIN = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END [^-\r\n]*PRIVATE KEY-----")
_PEM_MARKER = re.compile(r"-----(BEGIN|END) [^-\r\n]*PRIVATE KEY-----")
_ANSI = re.compile(
    r"\x1b\][\s\S]*?(?:\x07|\x1b\\|$)"
    r"|\x1b[P^_][\s\S]*?(?:\x1b\\|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*$"
    r"|\x1b."
)
_SECRET_OPTION = re.compile(
    r"(?:password|passwd|secret|token|api[-_]?key|credential|authorization|cookie)", re.I
)


def _plain(text: str) -> str:
    text = _ANSI.sub("", text).replace("\t", "    ")
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


class _OutputSanitizer:
    """Wait for complete lines so chunk boundaries cannot split a secret from its key."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._discarding = False
        self._discard_tail = b""
        self._in_private_key = False

    def _line(self, raw: bytes) -> str | None:
        line = _plain(raw.decode("utf-8", errors="replace"))
        parts: list[str] = []
        while line:
            if self._in_private_key:
                end = _PEM_END.search(line)
                if end is None:
                    break
                self._in_private_key = False
                line = line[end.end():]
            else:
                start = _PEM_BEGIN.search(line)
                if start is None:
                    parts.append(line)
                    break
                parts.extend((line[:start.start()], "[REDACTED PRIVATE KEY]"))
                self._in_private_key = True
                line = line[start.end():]
        result = redact_text("".join(parts))
        return result if result.strip() else None

    def _observe_discarded(self, data: bytes) -> None:
        # Even a discarded oversized line may start/end a key that spans later lines.
        observed = self._discard_tail + data
        text = _plain(observed.decode("utf-8", errors="replace"))
        for marker in _PEM_MARKER.finditer(text):
            self._in_private_key = marker.group(1) == "BEGIN"
        self._discard_tail = observed[-256:]

    def feed(self, data: bytes, *, eof: bool = False) -> list[str]:
        output: list[str] = []
        pieces = data.split(b"\n")
        for position, piece in enumerate(pieces):
            complete = position < len(pieces) - 1
            if self._discarding:
                self._observe_discarded(piece)
            elif len(self._pending) + len(piece) > _MAX_LINE_BYTES:
                self._discarding = True
                self._observe_discarded(bytes(self._pending) + piece)
                self._pending.clear()
                output.append("[discarded oversized output line]")
            else:
                self._pending.extend(piece)
            if complete:
                if not self._discarding:
                    line = self._line(bytes(self._pending))
                    if line is not None:
                        output.append(line)
                self._pending.clear()
                self._discarding = False
                self._discard_tail = b""
        if eof:
            if self._pending and not self._discarding:
                line = self._line(bytes(self._pending))
                if line is not None:
                    output.append(line)
            self._pending.clear()
            self._discarding = False
            self._discard_tail = b""
        return output


def _safe_argv(argv: list[str]) -> list[str]:
    result: list[str] = []
    secret_value = False
    for argument in argv:
        if secret_value:
            result.append("[REDACTED]")
            secret_value = False
            continue
        result.append(redact_text(_plain(argument)))
        secret_value = argument.startswith("-") and "=" not in argument and bool(
            _SECRET_OPTION.search(argument)
        )
    return result


def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        # macOS can return EPERM for a group that just disappeared. A live direct
        # child still requires a successful signal; never swallow that failure.
        if process.poll() is None:
            raise


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    # The group can outlive its leader; do not skip cleanup merely because poll() succeeded.
    _signal_group(process, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=0.3)
    _signal_group(process, signal.SIGKILL)
    process.wait(timeout=2)


def run_logged(
    store: SessionStore,
    session: Path,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    label: str = "process",
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run a command without a shell or stdin; approvals belong to the calling CLI.

    POSIX process groups let timeout/cancellation stop descendants as well as the direct
    child. Windows users run this development workflow through WSL.
    """
    if os.name != "posix":
        raise RuntimeError("Dev command recording requires POSIX process groups; use WSL")
    if not argv or any(not isinstance(value, str) or "\0" in value for value in argv):
        raise ValueError("Command must be a nonempty list of arguments without NUL bytes")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError("Command timeout must be positive and finite")
    safe_argv = _safe_argv(argv)
    safe_label = redact_text(_plain(label))[:500]
    recording_errors: list[str] = []
    events_available = True
    terminal_available = True

    def record(kind: str, message: str, **metadata: Any) -> None:
        nonlocal events_available
        if not events_available:
            return
        try:
            store.event(session, kind, message, **metadata)
        except Exception as exc:
            events_available = False
            recording_errors.append(f"Artifact recording failed: {type(exc).__name__}")

    started = time.monotonic()
    if cancelled is not None and cancelled():
        record("process_cancelled", safe_label, argv=safe_argv)
        return {
            "argv": safe_argv,
            "returncode": 130,
            "timed_out": False,
            "cancelled": True,
            "duration_seconds": 0.0,
            "output_sha256": hashlib.sha256().hexdigest(),
            "output_tail": "",
            "recording_errors": recording_errors,
        }
    record("process_started", safe_label, argv=safe_argv)
    process = subprocess.Popen(
        argv,
        cwd=store.project_root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        start_new_session=True,
        bufsize=0,
    )
    assert process.stdout is not None
    sanitizer = _OutputSanitizer()
    timed_out = False
    was_cancelled = False
    deadline = started + timeout if timeout is not None else None
    drain_deadline: float | None = None
    output_hash = hashlib.sha256()
    output_tail = bytearray()

    def emit(lines: list[str]) -> None:
        nonlocal terminal_available
        if not lines:
            return
        text = "\n".join(lines) + "\n"
        encoded = text.encode("utf-8")
        output_hash.update(encoded)
        output_tail.extend(encoded)
        del output_tail[:-8192]
        if terminal_available:
            try:
                sys.stdout.write(text)
                sys.stdout.flush()
            except Exception as exc:
                terminal_available = False
                recording_errors.append(f"Terminal output failed: {type(exc).__name__}")
        # Split only after sanitization. This bounds each event regardless of line length.
        for offset in range(0, len(text), 12_000):
            record("process_output", text[offset:offset + 12_000], label=safe_label)

    try:
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if cancelled is not None and cancelled() and not was_cancelled:
                    was_cancelled = True
                    _stop_group(process)
                    drain_deadline = time.monotonic() + 1
                if (
                    deadline is not None
                    and now >= deadline
                    and not timed_out
                    and not was_cancelled
                    and process.poll() is None
                ):
                    timed_out = True
                    _stop_group(process)
                    drain_deadline = time.monotonic() + 1
                if process.poll() is not None and drain_deadline is None:
                    # Also prevent a detached/background child retaining our pipe indefinitely.
                    _stop_group(process)
                    drain_deadline = time.monotonic() + 1
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    break
                if not selector.get_map():
                    time.sleep(0.02)
                    continue
                for key, _events in selector.select(timeout=0.05):
                    try:
                        chunk = os.read(key.fd, _READ_BYTES)
                    except BlockingIOError:
                        continue
                    if chunk:
                        emit(sanitizer.feed(chunk))
                    else:
                        selector.unregister(key.fileobj)
            emit(sanitizer.feed(b"", eof=True))
    except KeyboardInterrupt:
        _stop_group(process)
        record("process_interrupted", safe_label, argv=safe_argv)
        raise
    finally:
        _stop_group(process)
        process.stdout.close()
    was_cancelled = was_cancelled or (cancelled is not None and cancelled())
    if was_cancelled:
        record("process_cancelled", safe_label, argv=safe_argv)
    result = {
        "argv": safe_argv,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "cancelled": was_cancelled,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_sha256": output_hash.hexdigest(),
        "output_tail": output_tail.decode("utf-8", errors="ignore"),
        "recording_errors": recording_errors,
    }
    record("process_finished", safe_label, **result)
    return result
