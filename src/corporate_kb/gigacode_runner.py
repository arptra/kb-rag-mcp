"""Run GigaCode headlessly as a bounded, read-only repository analyst."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

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
_URL_PATTERN: re.Pattern[str] = re.compile(r"https?://[^\s<>\"']+")
_ANSI_PATTERN: re.Pattern[str] = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_AUTH_HINTS = ("auth", "browser", "device", "login", "sign in", "verify", "open")


class CancellationSignal(Protocol):
    def is_set(self) -> bool: ...


class GigaCodeCancelled(RuntimeError):
    """Raised when the catalog cancels an active GigaCode subprocess."""


class GigaCodeTimedOut(RuntimeError):
    """Raised when GigaCode does not stop within the configured deadline."""


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
    ) -> GigaCodeJsonResult:
        """Execute one strict JSON-contract analysis without accepting prose fallback."""
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
        )
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
    ) -> _InvocationResult:
        """Run the shared supervised process and return its structured payload."""
        status = self.status(refresh=True)
        if not status["available"]:
            raise RuntimeError(str(status["error"]))
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
        command = [
            executable,
            "--output-format",
            "stream-json",
            "--exclude-tools",
            excluded_tools,
            "--max-session-turns",
            str(self._settings.gigacode_max_session_turns),
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
                f"max_turns={self._settings.gigacode_max_session_turns}; "
                f"max_tools_advisory={self._settings.gigacode_max_tool_calls}; "
                f"read_only={str(not allow_edits).lower()}; "
                f"approval_mode={'auto-edit' if allow_edits else 'default'}; "
                "schema_delivery=prompt"
            )
        bounded_prompt = self._prompt_with_output_contract(
            prompt,
            schema=schema,
            read_only=not allow_edits,
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
                        "max_session_turns": self._settings.gigacode_max_session_turns,
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
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        readers = [
            threading.Thread(
                target=self._read_stream,
                args=("stdout", process.stdout, events),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=("stderr", process.stderr, events),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        assert process.stdin is not None
        try:
            process.stdin.write(bounded_prompt)
            if not bounded_prompt.endswith("\n"):
                process.stdin.write("\n")
            process.stdin.close()
        except BrokenPipeError:
            pass

        deadline = time.monotonic() + self._settings.gigacode_timeout_seconds + 10
        open_streams = {"stdout", "stderr"}
        result_event: dict[str, Any] | None = None
        stderr_tail: list[str] = []
        stdout_tail: list[str] = []
        assistant_messages: list[str] = []
        stream_text_parts: list[str] = []
        reported_auth_urls: set[str] = set()
        waiting_for_authentication = False
        debug_stdout: TextIO | None = None
        debug_stderr: TextIO | None = None
        if debug_directory is not None:
            debug_stdout = (debug_directory / "stdout.jsonl").open("w", encoding="utf-8")
            debug_stderr = (debug_directory / "stderr.log").open("w", encoding="utf-8")
        try:
            while process.poll() is None or open_streams:
                if cancel is not None and cancel.is_set():
                    self._stop(process)
                    raise GigaCodeCancelled("GigaCode analysis was cancelled")
                if time.monotonic() >= deadline:
                    self._stop(process)
                    raise GigaCodeTimedOut(
                        "GigaCode exceeded "
                        f"{self._settings.gigacode_timeout_seconds} seconds"
                    )
                try:
                    source, line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    open_streams.discard(source)
                    continue
                auth_url = self._authentication_url(line)
                authentication_started = False
                if auth_url is not None and auth_url not in reported_auth_urls:
                    reported_auth_urls.add(auth_url)
                    waiting_for_authentication = True
                    authentication_started = True
                    deadline = (
                        time.monotonic()
                        + self._settings.gigacode_auth_timeout_seconds
                        + self._settings.gigacode_timeout_seconds
                        + 10
                    )
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
                stdout_tail = self._tail(stdout_tail, line)
                if debug_stdout is not None:
                    debug_stdout.write(f"{line}\n")
                    debug_stdout.flush()
                event = self._parse_event(line)
                if event is None:
                    if progress is not None:
                        progress(f"GigaCode stdout (non-JSON) | {line[:1000]}")
                    continue
                if event.get("type") == "result":
                    result_event = event
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
                    deadline = time.monotonic() + self._settings.gigacode_timeout_seconds + 10
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
            for reader in readers:
                reader.join(timeout=1)
            if debug_stdout is not None:
                debug_stdout.close()
            if debug_stderr is not None:
                debug_stderr.close()
        return_code = process.wait(timeout=2)
        if return_code != 0:
            detail_lines = [*stderr_tail[-20:], *stdout_tail[-10:]]
            detail = "\n".join(detail_lines)
            error = RuntimeError(
                f"GigaCode exited with code {return_code}"
                + (f": {detail[-4000:]}" if detail else "")
            )
            self._write_debug_failure(debug_directory, error)
            raise error
        if result_event is None:
            error = RuntimeError("GigaCode completed without a result event")
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
            error = RuntimeError(
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
    def _read_stream(
        source: str,
        stream: TextIO | None,
        events: queue.Queue[tuple[str, str | None]],
    ) -> None:
        if stream is None:
            events.put((source, None))
            return
        try:
            for line in stream:
                events.put((source, line.rstrip("\r\n")))
        finally:
            stream.close()
            events.put((source, None))

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
        lowered = clean_line.lower()
        matches = _URL_PATTERN.findall(clean_line)
        if not matches:
            return None
        stripped = clean_line.strip()
        if not any(hint in lowered for hint in _AUTH_HINTS) and not stripped.startswith(
            ("http://", "https://")
        ):
            return None
        return str(matches[0]).rstrip(".,;:)]}")

    @staticmethod
    def _signals_analysis_activity(event: dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind in {"assistant", "user", "result"}:
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
            content = event.get("message", {}).get("content", [])
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
    def _stop(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            process.wait(timeout=3)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=2)
            except OSError:
                return
