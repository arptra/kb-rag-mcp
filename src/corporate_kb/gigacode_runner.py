"""Run GigaCode headlessly as a bounded, read-only repository analyst."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO
from urllib.parse import urlsplit

from corporate_kb.config import Settings

_SSOT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "markdown": {"type": "string", "minLength": 100},
        "analyzed_files": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2000,
        },
        "blocking_unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 200,
        },
    },
    "required": ["markdown", "analyzed_files", "blocking_unknowns"],
    "additionalProperties": False,
}
_WORKSPACE_EDIT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "needs_input"]},
        "message": {"type": "string", "minLength": 1},
        "changed_files": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 100,
        },
        "verification": {"type": "string"},
    },
    "required": ["status", "message", "changed_files", "verification"],
    "additionalProperties": False,
}
_URL_PATTERN: re.Pattern[str] = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ANSI_PATTERN: re.Pattern[str] = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_AUTH_HINT_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:auth(?:entication|orization|enticate)?|browser|device|log[ -]?in|"
    r"sign[ -]?in|verify|verification|open)\b",
    re.IGNORECASE,
)
_RESULT_DRAIN_SECONDS = 1.0
_SUPERVISOR_GRACE_SECONDS = 10.0
_STREAM_LINE_LIMIT = 2_000_000
_OUTPUT_LIMIT = 8_000_000


def browser_authentication_url(value: str) -> str | None:
    """Validate a complete browser login URL without changing its signed query."""
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        return None
    if any(character in value for character in "\\<>\"'"):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        # Accessing port also rejects malformed and out-of-range port numbers.
        _ = parsed.port
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9.:-]+", hostname):
        return None
    return value


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class GigaCodeCancelled(RuntimeError):
    """Raised when the catalog cancels an active GigaCode subprocess."""


class GigaCodeTimedOut(RuntimeError):
    """Raised when GigaCode does not stop within the configured deadline."""


class GigaCodeInvalidOutput(RuntimeError):
    """The CLI ran but returned an incomplete or unusable model response."""


class GigaCodePolicyStop(RuntimeError):
    """The native event envelope explicitly signalled a provider restriction."""

    def __init__(self) -> None:
        super().__init__("GigaCode reported a provider policy stop")


@dataclass(frozen=True, slots=True)
class GigaCodeModelProfile:
    """Deployment-verified launcher contract for model-only invocations.

    The administrator must verify that this exact launcher/version/argument set
    disables native tools and inherited MCP, hooks, and context. This declaration
    is not a sandbox or proof of isolation. Arguments may select a trusted
    launcher configuration, but cannot replace the runner's input/output, turn
    limit, fresh session, or temporary workspace. An argument-free wrapper still
    needs an explicit version and isolation notes. No CLI profile is implicit.
    """

    name: str
    cli_version: str
    arguments: tuple[str, ...]
    isolation_notes: str

    def __post_init__(self) -> None:
        for field in ("name", "cli_version", "isolation_notes"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Model profile {field} must be explicitly provided")
        if self.cli_version != self.cli_version.strip() or self.cli_version == "unknown":
            raise ValueError("Model profile cli_version must be an exact known version")
        if not isinstance(self.arguments, tuple):
            raise ValueError("Model profile arguments must be an immutable tuple")
        reserved = {
            "--output-format", "--input-format", "--max-session-turns", "--prompt",
            "--prompt-interactive", "--continue", "--resume", "--session-id",
            "--fork-session", "--sandbox-session-id", "--cwd", "--working-directory",
            "--include-directories", "--add-dir", "--directory", "--worktree", "--help",
            "--version",
        }
        reserved_short = {"-o", "-p", "-i", "-c", "-r", "-h", "-v"}
        for argument in self.arguments:
            if not isinstance(argument, str) or not argument or "\0" in argument:
                raise ValueError("Model profile arguments must be nonempty strings without NUL")
            flag = re.sub(r"([a-z])([A-Z])", r"\1-\2", argument.split("=", 1)[0])
            flag = flag.replace("_", "-").lower()
            if (
                flag in reserved
                or argument == "--"
                or (argument.startswith("-") and not argument.startswith("--")
                    and argument[:2] in reserved_short)
            ):
                raise ValueError(f"Model profile cannot override runner argument: {flag}")


@dataclass(frozen=True, slots=True)
class GigaCodeResult:
    """Validated structured result returned by one headless GigaCode run."""

    markdown: str
    analyzed_files: tuple[str, ...]
    blocking_unknowns: tuple[str, ...]
    session_id: str | None
    model: str | None
    duration_ms: int | None
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class GigaCodeJsonResult:
    """Validated arbitrary JSON result returned by a headless GigaCode run."""

    payload: dict[str, Any]
    analyzed_files: tuple[str, ...]
    session_id: str | None
    model: str | None
    duration_ms: int | None
    usage: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _InvocationResult:
    payload: dict[str, Any]
    result_event: dict[str, Any]
    parse_mode: str


class GigaCodeRunner:
    """Supervise bounded GigaCode JSON invocations with explicit tool policies."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._status_lock = threading.Lock()
        self._cached_status: tuple[float, dict[str, Any]] | None = None

    def status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Return command/version readiness without attempting a model request."""
        with self._status_lock:
            now = time.monotonic()
            if (
                not refresh
                and self._cached_status is not None
                and now - self._cached_status[0] < 30
            ):
                return dict(self._cached_status[1])
            payload = self._probe()
            self._cached_status = (now, payload)
            return dict(payload)

    def run(
        self,
        *,
        checkout: Path,
        prompt: str,
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str], None] | None = None,
        authentication_complete: Callable[[], None] | None = None,
        debug_directory: Path | None = None,
    ) -> GigaCodeResult:
        """Execute one single-shot structured repository analysis."""
        invocation = self._run_payload(
            checkout=checkout,
            prompt=prompt,
            schema=_SSOT_RESULT_SCHEMA,
            normalizer=self._normalize_result_mapping,
            allow_text_fallback=True,
            cancel=cancel,
            progress=progress,
            authentication_url=authentication_url,
            authentication_complete=authentication_complete,
            debug_directory=debug_directory,
        )
        return self._validated_result(invocation.payload, invocation.result_event)

    def run_json(
        self,
        *,
        checkout: Path,
        prompt: str,
        schema: dict[str, Any],
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str], None] | None = None,
        authentication_complete: Callable[[], None] | None = None,
        debug_directory: Path | None = None,
        allow_edits: bool = False,
        decision_only: bool = False,
    ) -> GigaCodeJsonResult:
        """Execute a JSON analysis using the existing CLI command and login.

        Decision requests use only supplied evidence and reject native tool events;
        they do not require a model profile or change the CLI launch arguments.
        This guard is not an OS sandbox: CLI settings and authentication are shared
        with ordinary analysis, and a reported tool may already have executed.
        """
        invocation = self._run_payload(
            checkout=checkout,
            prompt=prompt,
            schema=schema,
            normalizer=lambda payload: payload,
            allow_text_fallback=False,
            cancel=cancel,
            progress=progress,
            authentication_url=authentication_url,
            authentication_complete=authentication_complete,
            debug_directory=debug_directory,
            allow_edits=allow_edits,
            decision_only=decision_only,
        )
        return self._json_result(invocation)

    def run_model_json(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        profile: GigaCodeModelProfile | None = None,
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str], None] | None = None,
        authentication_complete: Callable[[], None] | None = None,
    ) -> GigaCodeJsonResult:
        """Return a JSON decision using an explicitly verified no-tools launcher.

        A fresh directory prevents this call from using a repository checkout.
        The trusted profile must separately prevent inherited user/system config
        and tool execution. Native-tool event rejection is defense in depth; an
        emitted event does not guarantee that the CLI has not executed the tool.
        JSON Schema is delivered in the prompt; callers validate its semantics.
        """
        if not isinstance(profile, GigaCodeModelProfile):
            raise ValueError("A verified GigaCode model-only launcher profile is required")
        with tempfile.TemporaryDirectory(prefix="kb-gigacode-model-") as directory:
            invocation = self._run_payload(
                checkout=Path(directory),
                prompt=prompt,
                schema=schema,
                normalizer=lambda payload: payload,
                allow_text_fallback=False,
                cancel=cancel,
                progress=progress,
                authentication_url=authentication_url,
                authentication_complete=authentication_complete,
                model_profile=profile,
            )
        return self._json_result(invocation)

    @staticmethod
    def _json_result(invocation: _InvocationResult) -> GigaCodeJsonResult:
        event = invocation.result_event
        analyzed_files = invocation.payload.get("analyzed_files", [])
        if not isinstance(analyzed_files, list) or not all(
            isinstance(item, str) for item in analyzed_files
        ):
            analyzed_files = []
        usage = event.get("usage")
        return GigaCodeJsonResult(
            payload=invocation.payload,
            analyzed_files=tuple(analyzed_files),
            session_id=str(event["session_id"]) if event.get("session_id") is not None else None,
            model=str(event["model"]) if event.get("model") is not None else None,
            duration_ms=(
                int(event["duration_ms"])
                if isinstance(event.get("duration_ms"), int)
                else None
            ),
            usage=usage if isinstance(usage, dict) else {},
        )

    def run_workspace_edit(
        self,
        *,
        checkout: Path,
        prompt: str,
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str], None] | None = None,
        authentication_complete: Callable[[], None] | None = None,
        debug_directory: Path | None = None,
    ) -> GigaCodeJsonResult:
        """Execute one bounded workspace edit without shell, web, or sub-agent tools."""
        return self.run_json(
            checkout=checkout,
            prompt=prompt,
            schema=_WORKSPACE_EDIT_RESULT_SCHEMA,
            cancel=cancel,
            progress=progress,
            authentication_url=authentication_url,
            authentication_complete=authentication_complete,
            debug_directory=debug_directory,
            allow_edits=True,
        )

    def _run_payload(
        self,
        *,
        checkout: Path,
        prompt: str,
        schema: dict[str, Any],
        normalizer: Callable[[dict[str, Any] | None], dict[str, Any] | None],
        allow_text_fallback: bool,
        cancel: CancellationSignal | None = None,
        progress: Callable[[str], None] | None = None,
        authentication_url: Callable[[str], None] | None = None,
        authentication_complete: Callable[[], None] | None = None,
        debug_directory: Path | None = None,
        allow_edits: bool = False,
        model_profile: GigaCodeModelProfile | None = None,
        decision_only: bool = False,
    ) -> _InvocationResult:
        """Run the shared supervised process and return its structured payload."""
        if decision_only and allow_edits:
            raise ValueError("A decision request cannot enable workspace edits")
        status = self.status(refresh=True)
        if not status["available"]:
            raise RuntimeError(str(status["error"]))
        if model_profile is not None:
            if allow_edits:
                raise ValueError("A model-only profile cannot enable workspace edits")
            if status["version"] != model_profile.cli_version:
                raise RuntimeError(
                    f"Model profile {model_profile.name!r} requires CLI version "
                    f"{model_profile.cli_version!r}; found {status['version']!r}"
                )
        root = checkout.resolve()
        if not root.is_dir():
            raise ValueError(f"GigaCode checkout is not a directory: {root}")
        if cancel is not None and cancel.is_set():
            raise GigaCodeCancelled("GigaCode analysis was cancelled")

        executable = str(status["executable"])
        excluded_tools = (
            "shell,agent,web_fetch,web_search"
            if allow_edits
            else "shell,write,edit,agent,web_fetch,web_search"
        )
        max_turns = 2 if model_profile is not None else self._settings.gigacode_max_session_turns
        if model_profile is not None:
            command = [
                executable, *model_profile.arguments,
                "--output-format", "stream-json", "--max-session-turns", str(max_turns),
            ]
        else:
            command = [
                executable,
                "--output-format",
                "stream-json",
                "--exclude-tools",
                excluded_tools,
                "--max-session-turns",
                str(max_turns),
            ]
            if allow_edits:
                command.extend(["--approval-mode", "auto-edit"])
        environment = os.environ.copy()
        environment.setdefault("NO_COLOR", "1")
        creation: dict[str, Any] = {"start_new_session": os.name != "nt"}
        if os.name == "nt":
            creation = {
                "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            }
        if progress is not None:
            progress(
                "GigaCode starting: "
                f"executable={executable}; cwd={root}; output=stream-json; "
                f"timeout={self._settings.gigacode_timeout_seconds}s; "
                f"auth_timeout={self._settings.gigacode_auth_timeout_seconds}s; "
                f"max_turns={max_turns}; "
                f"max_tools_advisory={self._settings.gigacode_max_tool_calls}; "
                f"read_only={str(not allow_edits).lower()}; "
                f"approval_mode={'auto-edit' if allow_edits else 'default'}; "
                "schema_delivery=prompt; "
                f"model_profile={model_profile.name if model_profile else 'none'}"
            )
        bounded_prompt = (
            self._model_prompt_with_output_contract(prompt, schema)
            if model_profile is not None or decision_only
            else self._prompt_with_output_contract(
                prompt,
                schema=schema,
                read_only=not allow_edits,
            )
        )
        if debug_directory is not None:
            debug_directory.mkdir(parents=True, exist_ok=True)
            (debug_directory / "prompt.txt").write_text(bounded_prompt, encoding="utf-8")
            (debug_directory / "schema.json").write_text(
                json.dumps(schema, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (debug_directory / "invocation.json").write_text(
                json.dumps(
                    {
                        "command": command,
                        "cwd": str(root),
                        "read_only": not allow_edits,
                        "approval_mode": "auto-edit" if allow_edits else "default",
                        "timeout_seconds": self._settings.gigacode_timeout_seconds,
                        "max_session_turns": max_turns,
                        "max_tool_calls_advisory": self._settings.gigacode_max_tool_calls,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **creation,
        )
        events: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=8)
        streams_stop = threading.Event()
        readers = [
            threading.Thread(
                target=self._read_stream,
                args=("stdout", process.stdout, events, streams_stop),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=("stderr", process.stderr, events, streams_stop),
                daemon=True,
            ),
        ]
        writer = threading.Thread(
            target=self._write_stream,
            args=(process.stdin, bounded_prompt, events, streams_stop),
            daemon=True,
        )
        deadline = (
            time.monotonic() + self._settings.gigacode_timeout_seconds + _SUPERVISOR_GRACE_SECONDS
        )
        authentication_deadline: float | None = None
        open_streams = {"stdout", "stderr", "stdin"}
        result_event: dict[str, Any] | None = None
        result_deadline: float | None = None
        cleanup_deadline: float | None = None
        process_cleaned = False
        stopped_after_result = False
        output_chars = 0
        stderr_tail: list[str] = []
        stdout_tail: list[str] = []
        assistant_messages: list[str] = []
        stream_text_parts: list[str] = []
        reported_auth_urls: set[str] = set()
        waiting_for_authentication = False
        debug_stdout: TextIO | None = None
        debug_stderr: TextIO | None = None
        try:
            for reader in readers:
                reader.start()
            writer.start()
            if debug_directory is not None:
                debug_stdout = (debug_directory / "stdout.jsonl").open("w", encoding="utf-8")
                debug_stderr = (debug_directory / "stderr.log").open("w", encoding="utf-8")
            while process.poll() is None or open_streams or not events.empty():
                if cancel is not None and cancel.is_set():
                    raise GigaCodeCancelled("GigaCode analysis was cancelled")
                now = time.monotonic()
                if cleanup_deadline is not None and now >= cleanup_deadline:
                    break
                if result_deadline is not None and now >= result_deadline:
                    # A final response does not require a well-behaved CLI shutdown.
                    # Keep a short drain window for a later error/native-tool event.
                    running = process.poll() is None
                    signalled = self._stop(process)
                    stopped_after_result = running and signalled
                    process_cleaned = True
                    result_deadline = None
                    cleanup_deadline = time.monotonic() + 0.5
                elif result_event is None and now >= deadline:
                    raise GigaCodeTimedOut(
                        "GigaCode exceeded "
                        f"{self._settings.gigacode_timeout_seconds} seconds"
                    )
                try:
                    source, line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if source == "overflow":
                    raise GigaCodeInvalidOutput("GigaCode output exceeded the safe size limit")
                if line is None:
                    open_streams.discard(source)
                    continue
                output_chars += len(line)
                if output_chars > _OUTPUT_LIMIT:
                    raise GigaCodeInvalidOutput("GigaCode output exceeded the safe size limit")
                auth_url = self._authentication_url(line)
                authentication_started = False
                if auth_url is not None and auth_url not in reported_auth_urls:
                    reported_auth_urls.add(auth_url)
                    waiting_for_authentication = True
                    authentication_started = True
                    if authentication_deadline is None:
                        authentication_deadline = (
                            time.monotonic()
                            + self._settings.gigacode_auth_timeout_seconds
                            + self._settings.gigacode_timeout_seconds
                            + _SUPERVISOR_GRACE_SECONDS
                        )
                    deadline = authentication_deadline
                    if progress is not None:
                        progress(f"GigaCode waits for browser authentication: {auth_url}")
                    if authentication_url is not None:
                        authentication_url(auth_url)
                if source == "stderr":
                    if debug_stderr is not None:
                        debug_stderr.write(f"{line}\n")
                        debug_stderr.flush()
                    stderr_tail = self._tail(stderr_tail, line)
                    if progress is not None:
                        progress(f"GigaCode stderr | {line[:2000]}")
                    continue
                event = self._parse_event(line)
                if event is not None and self._policy_stopped(event):
                    raise GigaCodePolicyStop()
                stdout_tail = self._tail(stdout_tail, line)
                if debug_stdout is not None:
                    debug_stdout.write(f"{line}\n")
                    debug_stdout.flush()
                if event is None:
                    if progress is not None:
                        progress(f"GigaCode stdout (non-JSON) | {line[:1000]}")
                    continue
                if (
                    model_profile is not None or decision_only
                ) and self._has_native_tool_event(event):
                    raise RuntimeError(
                        "GigaCode emitted a native tool event during a JSON decision; "
                        "invocation aborted. The tool may already have executed."
                    )
                if event.get("type") == "result":
                    # A later success must never erase an already reported terminal error.
                    if result_event is None or not (
                        result_event.get("is_error") or self._incomplete_result(result_event)
                    ):
                        result_event = event
                    if result_deadline is None and cleanup_deadline is None:
                        result_deadline = min(deadline, time.monotonic() + _RESULT_DRAIN_SECONDS)
                elif event.get("type") == "assistant":
                    assistant_text = self._assistant_message_text(event)
                    if assistant_text:
                        assistant_messages.append(assistant_text)
                elif event.get("type") == "stream_event":
                    stream_text = self._stream_text_delta(event)
                    if stream_text:
                        stream_text_parts.append(stream_text)
                if (
                    waiting_for_authentication
                    and not authentication_started
                    and self._signals_analysis_activity(event)
                ):
                    waiting_for_authentication = False
                    deadline = (
                        time.monotonic()
                        + self._settings.gigacode_timeout_seconds
                        + _SUPERVISOR_GRACE_SECONDS
                    )
                    if progress is not None:
                        progress("GigaCode browser authentication completed; analysis resumed")
                    if authentication_complete is not None:
                        authentication_complete()
                if progress is not None:
                    progress(self._event_summary(event))
        except BaseException as exc:
            self._write_debug_failure(debug_directory, exc)
            raise
        finally:
            if not process_cleaned:
                self._stop(process)
            streams_stop.set()
            for worker in [writer, *readers]:
                if worker.ident is not None:
                    worker.join(timeout=1)
            if debug_stdout is not None:
                debug_stdout.close()
            if debug_stderr is not None:
                debug_stderr.close()
        return_code = process.wait(timeout=2)
        if result_event is not None and self._incomplete_result(result_event):
            error: RuntimeError = GigaCodeInvalidOutput(
                "GigaCode stopped before completing the response"
            )
            self._write_debug_failure(debug_directory, error)
            raise error
        shutdown_codes = {-2, -9, -15, 130, 137, 143}
        if return_code != 0 and not (stopped_after_result and return_code in shutdown_codes):
            detail_lines = [*stderr_tail[-20:], *stdout_tail[-10:]]
            detail = "\n".join(detail_lines)
            error = RuntimeError(
                f"GigaCode exited with code {return_code}"
                + (f": {detail[-4000:]}" if detail else "")
            )
            self._write_debug_failure(debug_directory, error)
            raise error
        if result_event is None:
            error = GigaCodeInvalidOutput("GigaCode completed without a result event")
            self._write_debug_failure(debug_directory, error)
            raise error
        if bool(result_event.get("is_error")):
            error = RuntimeError(
                f"GigaCode result failed: {self._result_error(result_event)}"
            )
            self._write_debug_failure(debug_directory, error)
            raise error
        structured, parse_mode = self._result_contract(
            result_event,
            assistant_messages,
            stream_text_parts,
            normalizer=normalizer,
            allow_text_fallback=allow_text_fallback,
        )
        if progress is not None:
            progress(f"GigaCode output contract: mode={parse_mode}")
        if structured is None:
            diagnostic = self._result_diagnostic(result_event, assistant_messages)
            if progress is not None:
                progress(f"GigaCode output contract failed: {diagnostic}")
            error = GigaCodeInvalidOutput(
                "GigaCode result did not contain usable output for the requested contract; "
                + diagnostic
            )
            self._write_debug_failure(debug_directory, error)
            raise error
        if debug_directory is not None:
            (debug_directory / "result.json").write_text(
                json.dumps(
                    {
                        "parse_mode": parse_mode,
                        "payload": structured,
                        "result_event": result_event,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        return _InvocationResult(structured, result_event, parse_mode)

    @staticmethod
    def _write_debug_failure(directory: Path | None, exc: BaseException) -> None:
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "failure.json").write_text(
            json.dumps(
                {
                    "type": type(exc).__name__,
                    "message": " ".join(str(exc).split())[:8000],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _probe(self) -> dict[str, Any]:
        command = self._settings.gigacode_command.strip()
        base = {
            "enabled": self._settings.gigacode_enabled,
            "available": False,
            "command": command,
            "executable": None,
            "version": None,
            "error": None,
            "mode": "headless-json-or-markdown",
            "output_format": "stream-json",
            "schema_delivery": "prompt",
            "wall_time_enforcement": "supervisor",
            "tool_call_limit_enforcement": "prompt",
            "server_llm_url_required": False,
            "read_only": True,
            "authentication": "browser-on-first-run",
        }
        if not self._settings.gigacode_enabled:
            return {**base, "error": "GigaCode server mode is disabled"}
        executable = self._resolve_executable(command)
        if executable is None:
            return {
                **base,
                "error": (
                    f"GigaCode executable '{command}' was not found. Install GigaCode on the "
                    "RAG server, or set KB_GIGACODE_COMMAND."
                ),
            }
        try:
            checked = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {**base, "executable": executable, "error": str(exc)}
        version = (checked.stdout or checked.stderr).strip().splitlines()
        if checked.returncode != 0:
            return {
                **base,
                "executable": executable,
                "error": f"GigaCode --version exited with code {checked.returncode}",
            }
        return {
            **base,
            "available": True,
            "executable": executable,
            "version": version[-1][:300] if version else "unknown",
        }

    @staticmethod
    def _model_prompt_with_output_contract(prompt: str, schema: dict[str, Any]) -> str:
        return (
            "You are the model-only decision step of an external orchestrator. "
            "Return one JSON decision. Do not call native CLI tools. Tools described in "
            "the input are choices for the external orchestrator, not tools to execute here. "
            "Use only the supplied messages and evidence. Do not pretend to read a "
            "repository, files, or external sources.\n\n"
            + prompt.rstrip()
            + "\n\nMODEL DECISION OUTPUT CONTRACT:\n"
            + "Return exactly one JSON object, with no Markdown fences or surrounding prose, "
            + "satisfying this JSON Schema (delivered as prompt instructions):\n"
            + json.dumps(schema, ensure_ascii=False, indent=2)
        )

    @staticmethod
    def _has_native_tool_event(event: dict[str, Any]) -> bool:
        # The result's JSON decision may describe tool calls for the outer graph.
        # Inspect stream envelopes, never parse the model's textual JSON as events.
        if event.get("type") == "result":
            return False
        native_types = {
            "tool_use", "tool_call", "tool_result", "tool_response", "function_call",
            "function_result", "input_json_delta", "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }

        def native(value: object) -> bool:
            if isinstance(value, list):
                return any(native(item) for item in value)
            if not isinstance(value, dict):
                return False
            if value.get("type") in native_types or value.get("subtype") in native_types:
                return True
            if value.get("role") in {"tool", "function"}:
                return True
            if any(value.get(key) for key in ("tool_calls", "function_call", "functionCall")):
                return True
            return any(
                native(value.get(key))
                for key in (
                    "message", "content", "event", "content_block", "delta", "item", "parts",
                )
            )

        return native(event)

    def _prompt_with_output_contract(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        read_only: bool = True,
    ) -> str:
        rendered_schema = json.dumps(schema or _SSOT_RESULT_SCHEMA, ensure_ascii=False, indent=2)
        return (
            prompt.rstrip()
            + "\n\nGIGACODE OUTPUT CONTRACT:\n"
            + "Your final response must contain only one valid JSON object: no Markdown fence, "
            + "no prose before or after it. The object must satisfy this JSON Schema:\n"
            + rendered_schema
            + "\nUse no more than "
            + str(self._settings.gigacode_max_tool_calls)
            + (" read-only tool calls." if read_only else " tool calls.")
        )

    @classmethod
    def _result_contract(
        cls,
        result_event: dict[str, Any],
        assistant_messages: list[str],
        stream_text_parts: list[str],
        *,
        normalizer: Callable[[dict[str, Any] | None], dict[str, Any] | None] | None = None,
        allow_text_fallback: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        normalize = normalizer or cls._normalize_result_mapping
        candidates: list[tuple[str, object]] = [
            ("structured_result", result_event.get("structured_result")),
            ("structured_output", result_event.get("structured_output")),
            ("result", result_event.get("result")),
            ("output", result_event.get("output")),
            ("response", result_event.get("response")),
        ]
        if assistant_messages:
            candidates.append(("assistant_message", assistant_messages[-1]))
        if stream_text_parts:
            candidates.append(("stream_text", "".join(stream_text_parts)))

        for source, raw_candidate in candidates:
            decoded = cls._decode_structured_result(raw_candidate)
            normalized = normalize(decoded)
            if normalized is not None:
                return normalized, f"{source}:json"

        if allow_text_fallback:
            for source, raw_candidate in candidates:
                text = cls._extract_text(raw_candidate).strip()
                if len(text) < 100:
                    continue
                return (
                    {
                        "markdown": text,
                        "analyzed_files": [],
                        "blocking_unknowns": [
                            "GigaCode returned plain Markdown without structured file metadata."
                        ],
                    },
                    f"{source}:markdown-fallback",
                )
        return None, "unusable"

    @classmethod
    def _decode_structured_result(cls, raw_result: object) -> dict[str, Any] | None:
        if isinstance(raw_result, dict):
            return raw_result
        text = cls._extract_text(raw_result).strip()
        if not text:
            return None
        candidates = [text]
        fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        candidates.extend(item.strip() for item in fenced if item.strip())
        for candidate in candidates:
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                return decoded

        decoder = json.JSONDecoder()
        for position, character in enumerate(text):
            if character != "{":
                continue
            try:
                decoded, _ = decoder.raw_decode(text[position:])
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
        return None

    @classmethod
    def _normalize_result_mapping(
        cls,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if payload is None:
            return None
        for key in (
            "structured_result",
            "structuredResult",
            "structured_output",
            "structuredOutput",
            "data",
            "output",
            "response",
        ):
            nested = payload.get(key)
            if isinstance(nested, dict):
                normalized = cls._normalize_result_mapping(nested)
                if normalized is not None:
                    return normalized

        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            for key in ("content", "ssot", "text"):
                candidate = payload.get(key)
                if isinstance(candidate, str):
                    markdown = candidate
                    break
        if not isinstance(markdown, str):
            return None

        analyzed_files = payload.get("analyzed_files", payload.get("analyzedFiles"))
        blocking_unknowns = payload.get(
            "blocking_unknowns",
            payload.get("blockingUnknowns"),
        )
        warnings: list[str] = []
        if not isinstance(analyzed_files, list) or not all(
            isinstance(item, str) for item in analyzed_files
        ):
            analyzed_files = []
            warnings.append("GigaCode omitted structured analyzed_files metadata.")
        if not isinstance(blocking_unknowns, list) or not all(
            isinstance(item, str) for item in blocking_unknowns
        ):
            blocking_unknowns = []
            warnings.append("GigaCode omitted structured blocking_unknowns metadata.")
        return {
            "markdown": markdown,
            "analyzed_files": analyzed_files,
            "blocking_unknowns": [*blocking_unknowns, *warnings],
        }

    @classmethod
    def _extract_text(cls, value: object, *, depth: int = 0) -> str:
        if depth > 5:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                text
                for item in value
                if (text := cls._extract_text(item, depth=depth + 1))
            )
        if not isinstance(value, dict):
            return ""
        for key in ("text", "content", "message", "result", "output", "response"):
            if key not in value:
                continue
            text = cls._extract_text(value[key], depth=depth + 1)
            if text:
                return text
        return ""

    @classmethod
    def _assistant_message_text(cls, event: dict[str, Any]) -> str:
        return cls._extract_text(event.get("message")).strip()

    @classmethod
    def _stream_text_delta(cls, event: dict[str, Any]) -> str:
        nested = event.get("event")
        if not isinstance(nested, dict):
            return ""
        delta = nested.get("delta")
        return cls._extract_text(delta)

    @classmethod
    def _result_diagnostic(
        cls,
        result_event: dict[str, Any],
        assistant_messages: list[str],
    ) -> str:
        raw_result = result_event.get("result")
        preview = cls._extract_text(raw_result).strip()
        if not preview and assistant_messages:
            preview = assistant_messages[-1].strip()
        compact_preview = " ".join(preview.split())[:2000]
        return (
            f"fields={','.join(sorted(str(key) for key in result_event))}; "
            f"result_type={type(raw_result).__name__}; result_chars={len(preview)}; "
            f"preview={compact_preview or '<empty>'}"
        )

    @staticmethod
    def _resolve_executable(command: str) -> str | None:
        if not command:
            return None
        if "/" in command or "\\" in command:
            path = Path(command).expanduser().resolve()
            return str(path) if path.is_file() and os.access(path, os.X_OK) else None
        return shutil.which(command)

    @staticmethod
    def _publish(
        events: queue.Queue[tuple[str, str | None]],
        item: tuple[str, str | None],
        stop: threading.Event,
    ) -> None:
        while not stop.is_set():
            try:
                events.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    @classmethod
    def _write_stream(
        cls,
        stream: TextIO | None,
        prompt: str,
        events: queue.Queue[tuple[str, str | None]],
        stop: threading.Event,
    ) -> None:
        try:
            if stream is not None:
                stream.write(prompt if prompt.endswith("\n") else prompt + "\n")
                stream.flush()
        except (BrokenPipeError, OSError):
            # The CLI may fail before accepting input; its exit/result is authoritative.
            pass
        finally:
            if stream is not None:
                with suppress(BrokenPipeError, OSError):
                    stream.close()
            cls._publish(events, ("stdin", None), stop)

    @classmethod
    def _read_stream(
        cls,
        source: str,
        stream: TextIO | None,
        events: queue.Queue[tuple[str, str | None]],
        stop: threading.Event,
    ) -> None:
        if stream is None:
            cls._publish(events, (source, None), stop)
            return
        try:
            while not stop.is_set():
                line = stream.readline(_STREAM_LINE_LIMIT + 1)
                if not line:
                    break
                if len(line) > _STREAM_LINE_LIMIT:
                    cls._publish(events, ("overflow", None), stop)
                    break
                cls._publish(events, (source, line.rstrip("\r\n")), stop)
        finally:
            stream.close()
            cls._publish(events, (source, None), stop)

    @staticmethod
    def _policy_stopped(event: dict[str, Any]) -> bool:
        # Inspect native metadata only. The result payload, tool arguments and
        # generated text may legitimately document these fields or quote a refusal.
        kind = event.get("type")
        metadata: object
        if kind in ("result", "error"):
            metadata = event
        elif kind == "assistant":
            metadata = event.get("message")
        elif kind == "stream_event":
            stream_event = event.get("event")
            if not isinstance(stream_event, dict):
                return False
            if stream_event.get("type") == "content_block_start":
                block = stream_event.get("content_block")
                return isinstance(block, dict) and block.get("type") == "refusal"
            if stream_event.get("type") != "message_delta":
                return False
            metadata = stream_event.get("delta")
        else:
            return False
        if not isinstance(metadata, dict):
            return False
        error = metadata.get("error")
        reasons = [metadata.get("stop_reason"), metadata.get("finish_reason")]
        if isinstance(error, dict):
            reasons.extend((error.get("code"), error.get("type")))
        if any(isinstance(reason, str) and reason.casefold() in {
            "refusal", "content_filter", "blacklist", "content_policy_violation",
            "responsibleaipolicyviolation",
        } for reason in reasons):
            return True
        if metadata.get("refusal"):
            return True
        content = metadata.get("content")
        return isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") == "refusal" for part in content
        )

    @staticmethod
    def _incomplete_result(event: dict[str, Any]) -> bool:
        subtype = event.get("subtype")
        return (isinstance(subtype, str) and subtype in {
            "error_max_turns", "error_max_output_tokens", "error_max_tokens",
        }) or any(
            isinstance(event.get(key), str)
            and event[key] in {"max_tokens", "max_output_tokens", "length", "max_turns"}
            for key in ("stop_reason", "finish_reason")
        )

    @staticmethod
    def _parse_event(line: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _authentication_url(line: str) -> str | None:
        clean_line = _ANSI_PATTERN.sub("", line)
        allow_url_only = True
        authentication_context = False
        try:
            structured = json.loads(clean_line)
        except json.JSONDecodeError:
            structured = None
        if isinstance(structured, dict):
            if "type" not in structured:
                # A bare login object must identify its URL explicitly. Accept only
                # top-level auth URL fields, never arbitrary untyped payload text.
                for key in (
                    "verification_uri_complete",
                    "authentication_url",
                    "authorization_url",
                    "auth_url",
                    "verification_uri",
                    "login_url",
                ):
                    value = structured.get(key)
                    if isinstance(value, str):
                        validated = browser_authentication_url(value)
                        if validated is not None:
                            return validated
                return None
            kind = str(structured.get("type", "")).lower().replace("-", "_")
            authentication_context = kind in {"auth", "authentication", "authorization", "login"}
            authentication_context = authentication_context or kind.startswith(
                ("auth_", "authentication_", "authorization_", "login_")
            )
            if kind != "system" and not authentication_context:
                # Model output and tool results may describe arbitrary URLs, including
                # authentication endpoints in the document being analyzed.
                return None

            def text_values(value: object) -> list[str]:
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    return [
                        text
                        for key, item in value.items()
                        for text in [str(key).replace("_", " "), *text_values(item)]
                    ]
                if isinstance(value, list):
                    return [text for item in value for text in text_values(item)]
                return []

            # Decode JSON strings first: escaped slashes/newlines must not become
            # part of the browser link.
            clean_line = " ".join(text_values(structured))
            allow_url_only = False
        elif isinstance(structured, str):
            clean_line = structured
        elif structured is not None:
            return None
        matches = list(_URL_PATTERN.finditer(clean_line))
        if not matches:
            return None
        context = _URL_PATTERN.sub("", clean_line)
        if not (
            authentication_context
            or _AUTH_HINT_PATTERN.search(context)
            or (
                allow_url_only
                and clean_line.strip().lower().startswith(("http://", "https://"))
            )
        ):
            return None
        for match in matches:
            candidate = match.group()
            # Remove a matching surrounding prose/Markdown bracket only; trimming
            # arbitrary punctuation can corrupt a signed URL's query parameter.
            opening = clean_line[match.start() - 1] if match.start() else ""
            closing = {"(": ")", "[": "]", "{": "}"}.get(opening)
            if closing and candidate.endswith(closing):
                candidate = candidate[:-1]
            validated = browser_authentication_url(candidate)
            if validated is not None:
                return validated
        return None

    @staticmethod
    def _signals_analysis_activity(event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind == "result":
            return not bool(event.get("is_error"))
        if kind in {"assistant", "user"}:
            return True
        if kind != "stream_event":
            return False
        nested = event.get("event")
        if not isinstance(nested, dict):
            return False
        nested_type = str(nested.get("type", "")).lower()
        return any(
            marker in nested_type
            for marker in ("content", "message", "text", "tool", "response")
        )

    @staticmethod
    def _event_summary(event: dict[str, Any]) -> str:
        kind = str(event.get("type", "event"))
        subtype = str(event.get("subtype", ""))
        if kind == "system":
            return (
                "GigaCode session ready: "
                f"session={event.get('session_id', 'unknown')}; "
                f"model={event.get('model', 'unknown')}; subtype={subtype or 'start'}"
            )
        if kind == "result":
            raw_result = event.get("result")
            summary = (
                "GigaCode result: "
                f"subtype={subtype or 'unknown'}; is_error={bool(event.get('is_error'))}; "
                f"duration_ms={event.get('duration_ms', 'unknown')}; "
                f"fields={','.join(sorted(str(key) for key in event))}; "
                f"result_type={type(raw_result).__name__}"
            )
            if isinstance(raw_result, str):
                summary += f"; result_chars={len(raw_result)}"
            if bool(event.get("is_error")):
                error = GigaCodeRunner._result_error(event).replace("\n", " ")
                summary += f"; error={error[:2000]}"
            return summary
        if kind == "assistant":
            message = event.get("message")
            content = message.get("content", []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                content = []
            tool_names = [
                str(item.get("name"))
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_use"
            ]
            return "GigaCode assistant" + (f": tools={','.join(tool_names)}" if tool_names else "")
        if kind == "user":
            return "GigaCode tool results received"
        if kind == "stream_event":
            nested = event.get("event")
            nested_type = nested.get("type", "unknown") if isinstance(nested, dict) else "unknown"
            return f"GigaCode stream event: {nested_type}"
        return f"GigaCode event: type={kind}; subtype={subtype or 'none'}"

    @staticmethod
    def _result_error(event: dict[str, Any]) -> str:
        raw_error = event.get("error", event.get("result", "unknown error"))
        if isinstance(raw_error, dict):
            raw_error = raw_error.get("message", raw_error)
        return str(raw_error)

    @staticmethod
    def _validated_result(
        structured: object,
        result_event: dict[str, Any],
    ) -> GigaCodeResult:
        if not isinstance(structured, dict):
            raise RuntimeError(
                "GigaCode result did not contain a JSON object matching the SSOT contract"
            )
        markdown = structured.get("markdown")
        analyzed_files = structured.get("analyzed_files")
        blocking_unknowns = structured.get("blocking_unknowns")
        if not isinstance(markdown, str) or len(markdown.strip()) < 100:
            raise RuntimeError("GigaCode returned invalid SSOT Markdown")
        if not isinstance(analyzed_files, list) or not all(
            isinstance(item, str) for item in analyzed_files
        ):
            raise RuntimeError("GigaCode returned invalid analyzed_files")
        if not isinstance(blocking_unknowns, list) or not all(
            isinstance(item, str) for item in blocking_unknowns
        ):
            raise RuntimeError("GigaCode returned invalid blocking_unknowns")
        usage = result_event.get("usage")
        return GigaCodeResult(
            markdown=markdown.strip() + "\n",
            analyzed_files=tuple(analyzed_files),
            blocking_unknowns=tuple(blocking_unknowns),
            session_id=(
                str(result_event["session_id"])
                if result_event.get("session_id") is not None
                else None
            ),
            model=(
                str(result_event["model"]) if result_event.get("model") is not None else None
            ),
            duration_ms=(
                int(result_event["duration_ms"])
                if isinstance(result_event.get("duration_ms"), int)
                else None
            ),
            usage=usage if isinstance(usage, dict) else {},
        )

    @staticmethod
    def _tail(lines: list[str], line: str, *, limit: int = 100) -> list[str]:
        return [*lines[-(limit - 1) :], line]

    @staticmethod
    def _stop(process: subprocess.Popen[str]) -> bool:
        """Bound cleanup and stop the entire CLI group, including an orphaned pipe holder."""
        signalled = False
        if os.name != "nt":
            for action in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    # The parent may already be gone while a child still owns stdout/stdin.
                    os.killpg(process.pid, action)
                    signalled = True
                except OSError:
                    break
                until = time.monotonic() + 0.4
                while time.monotonic() < until:
                    process.poll()
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        return signalled
                    except PermissionError:
                        # Some process sandboxes disallow existence probes while
                        # allowing signals to children. Keep cleanup bounded.
                        break
                    time.sleep(0.02)
        else:
            actions: tuple[Callable[[], None], ...] = (
                lambda: process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)),
                process.terminate,
                process.kill,
            )
            for stop_action in actions:
                if process.poll() is not None:
                    break
                try:
                    stop_action()
                    signalled = True
                    process.wait(timeout=0.4)
                except (OSError, subprocess.TimeoutExpired):
                    continue
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.5)
        return signalled
