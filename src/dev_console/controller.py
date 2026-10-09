"""Thread-safe browser control around the terminal-owned development repair loop."""

from __future__ import annotations

import json
import os
import queue
import re
import stat
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, cast

from corporate_kb.dev_debug.recording import SessionStore, now, redact_text
from corporate_kb.dev_debug.workflow import RepairWorkflow

_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_SECRET_KEY = re.compile(r"password|secret|token|authorization|cookie|api[_-]?key", re.I)
_ARTIFACT = re.compile(
    r"^(?:session\.json|events(?:\.previous)?\.jsonl|bundle-\d+\.json|"
    r"round-\d+/[a-z][a-z0-9-]*\.json)$"
)
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "[depth limit]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else _clean(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_clean(item, depth + 1) for item in value]
    return value


class DevConsoleController:
    """HTTP threads may queue work; only the original main thread may run GigaCode.

    Cancellation is cooperative. It never sends terminal input or signals to the
    foreground CLI; the user leaves that session with /quit before cancellation proceeds.
    """

    def __init__(
        self,
        project_root: Path,
        command: str = "gigacode",
        *,
        store: SessionStore | None = None,
        workflow_factory: Any = RepairWorkflow,
        collection_interval: float = 2.0,
    ) -> None:
        self.store = store or SessionStore(project_root)
        if self.store.project_root != project_root.resolve():
            raise ValueError("Session store belongs to another project")
        if not 0.01 <= collection_interval <= 60:
            raise ValueError("Invalid collection interval")
        self.command = command
        self._workflow_factory = workflow_factory
        self._interval = collection_interval
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=1)
        self._closed = False
        self._selected: str | None = None
        self._record_thread: threading.Thread | None = None
        self._record_stop = threading.Event()
        self._record_session: str | None = None
        self._record_error: str | None = None
        self._repair: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self._pending: dict[str, Any] | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Development console is closed")

    @staticmethod
    def _interactive_available() -> bool:
        return all(
            stream is not None and stream.isatty()
            for stream in (
                sys.stdin,
                sys.stdout,
                sys.stderr,
            )
        )

    def _session(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Invalid session ID")
        path = self.store.root / "sessions" / session_id
        state = self.store.load(path)
        if Path(state["project_root"]).resolve() != self.store.project_root:
            raise ValueError("Session belongs to another project")
        return path

    def _recording(self) -> dict[str, Any]:
        return {
            "running": bool(self._record_thread and self._record_thread.is_alive()),
            "session_id": self._record_session,
            "stopping": self._record_stop.is_set(),
            "error": self._record_error,
        }

    def _repair_state(self) -> dict[str, Any] | None:
        if self._repair is None:
            return None
        return cast(
            dict[str, Any],
            _clean(
                {
                    **self._repair,
                    "running": self._repair["status"] in {"queued", "running"},
                }
            ),
        )

    def _approval_state(self) -> dict[str, Any] | None:
        return _clean(self._pending["public"]) if self._pending else None

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            directory = self.store.root / "sessions"
            if not directory.is_dir():
                return []
            sessions = []
            for path in sorted(directory.iterdir(), key=lambda item: item.name, reverse=True)[:100]:
                if not path.is_dir() or path.is_symlink():
                    continue
                try:
                    state = self.store.load(self._session(path.name))
                except (ValueError, OSError, KeyError):
                    continue
                sessions.append(
                    _clean(
                        {
                            key: state.get(key)
                            for key in (
                                "id",
                                "label",
                                "goal",
                                "status",
                                "message",
                                "created_at",
                                "updated_at",
                                "round",
                                "last_collection",
                            )
                        }
                    )
                )
            return sessions

    def overview(self) -> dict[str, Any]:
        with self._lock:
            sessions = self.list_sessions()
            return {
                "project_root": str(self.store.project_root),
                "sessions": sessions,
                "active_session_id": self._selected or (sessions[0]["id"] if sessions else None),
                "recording": self._recording(),
                "repair": self._repair_state(),
                "pending_approval": self._approval_state(),
                "closed": self._closed,
                "interactive_available": self._interactive_available(),
            }

    def _events(self, session: Path) -> list[dict[str, Any]]:
        path = session / "events.jsonl"
        if not path.exists():
            return []
        content = self._read_file(path, tail=True)
        events = []
        for line in content.splitlines()[-100:]:
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                events.append(_clean(parsed))
        return events

    def session_detail(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._session(session_id)
            recording = self._recording()
            recording["running"] = recording["running"] and recording["session_id"] == session_id
            repair = self._repair_state()
            pending = self._approval_state()
            return {
                "session": _clean(self.store.load(session)),
                "events": self._events(session),
                "recording": recording,
                "fix": repair if repair and repair["session_id"] == session_id else None,
                "pending_approval": pending
                if pending and pending["session_id"] == session_id
                else None,
                "artifacts": self.list_artifacts(session_id),
            }

    def create_session(self, label: str = "") -> dict[str, Any]:
        if not isinstance(label, str) or len(label) > 500:
            raise ValueError("Session label must be at most 500 characters")
        with self._lock:
            self._ensure_open()
            session = self.store.create(label)
            self._selected = session.name
            return self.session_detail(session.name)

    def start_recording(
        self,
        session_id: str | None = None,
        label: str = "",
        log_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        if log_paths is not None and (
            not isinstance(log_paths, list)
            or len(log_paths) > 100
            or any(
                not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path
                for path in log_paths
            )
        ):
            raise ValueError("Invalid log paths")
        with self._lock:
            self._ensure_open()
            if self._recording()["running"]:
                if session_id is None or session_id != self._record_session:
                    raise RuntimeError("Stop the active recording before selecting another session")
                assert self._record_session is not None
                return self.session_detail(self._record_session)
            if session_id is None:
                session_id = self.create_session(label)["session"]["id"]
            session = self._session(session_id)
            if log_paths is not None:
                paths = [
                    str((self.store.project_root / Path(path).expanduser()).absolute())
                    for path in log_paths
                ]
                self.store.update(session, extra_log_paths=paths)
            self.store.event(session, "console_recording_started", "Diagnostic recording started")
            self._record_stop = threading.Event()
            self._record_session = session_id
            self._selected = session_id
            self._record_error = None
            stop = self._record_stop
            self._record_thread = threading.Thread(
                target=self._collect,
                args=(session, stop),
                daemon=True,
                name="dev-console-recorder",
            )
            self._record_thread.start()
            return self.session_detail(session_id)

    def _collect(self, session: Path, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.store.collect_once(session)
            except (OSError, ValueError) as exc:
                with self._lock:
                    self._record_error = redact_text(str(exc))[:2000]
            stop.wait(self._interval)

    def stop_recording(self, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if session_id is not None and session_id != self._record_session:
                raise ValueError("The requested session is not recording")
            self._record_stop.set()
            thread = self._record_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            return (
                self.session_detail(self._record_session)
                if self._record_session
                else self.overview()
            )

    def add_note(self, session_id: str, text: str) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ValueError("A note must contain 1..8000 characters")
        with self._lock:
            self._ensure_open()
            session = self._session(session_id)
            notes = [*self.store.load(session).get("notes", []), {"at": now(), "text": text}][-30:]
            self.store.update(session, notes=notes)
            self.store.event(session, "user_note", text)
            return self.session_detail(session_id)

    def prepare_bundle(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            session = self._session(session_id)
            path = self.store.prepare_bundle(session)
            return {"session_id": session_id, "path": path.relative_to(session).as_posix()}

    @staticmethod
    def _read_file(path: Path, *, tail: bool = False) -> str:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ARTIFACT_BYTES:
                raise ValueError("Artifact is not a bounded regular file")
            if tail:
                handle.seek(max(0, info.st_size - 128 * 1024))
            content = handle.read(_MAX_ARTIFACT_BYTES + 1)
            if len(content) > _MAX_ARTIFACT_BYTES:
                raise ValueError("Artifact exceeded its size limit while reading")
            return content.decode("utf-8", errors="replace")

    def _artifact(self, session_id: str, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not _ARTIFACT.fullmatch(relative_path):
            raise ValueError("Unsupported artifact path")
        session = self._session(session_id)
        path = session / relative_path
        if any(item.is_symlink() for item in (path, *path.parents) if item != session.parent):
            raise ValueError("Artifact paths must not contain symlinks")
        if not path.resolve().is_relative_to(session):
            raise ValueError("Artifact must remain inside its session")
        return path

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            session = self._session(session_id)
            candidates = list(session.iterdir())
            for directory in sorted(session.glob("round-*"))[:20]:
                if directory.is_dir() and not directory.is_symlink():
                    candidates.extend(directory.glob("*.json"))
            result = []
            for path in sorted(candidates):
                relative = path.relative_to(session).as_posix()
                if not _ARTIFACT.fullmatch(relative) or path.is_symlink() or not path.is_file():
                    continue
                info = path.stat()
                result.append(
                    {"path": relative, "size": info.st_size, "modified_at": info.st_mtime}
                )
            return result[:200]

    def read_artifact(self, session_id: str, relative_path: str) -> dict[str, Any]:
        with self._lock:
            path = self._artifact(session_id, relative_path)
            text = self._read_file(path)
            if path.suffix == ".json":
                try:
                    text = json.dumps(_clean(json.loads(text)), ensure_ascii=False, indent=2)
                except ValueError:
                    text = redact_text(text)
            else:
                text = redact_text(text)
            return {
                "path": relative_path,
                "content": text,
                "encoding": "utf-8",
                "size": path.stat().st_size,
                "truncated": False,
            }

    def queue_repair(self, session_id: str, goal: str = "", max_rounds: int = 3) -> dict[str, Any]:
        if not isinstance(goal, str) or len(goal) > 8000:
            raise ValueError("Goal must be at most 8000 characters")
        if (
            isinstance(max_rounds, bool)
            or not isinstance(max_rounds, int)
            or not 1 <= max_rounds <= 10
        ):
            raise ValueError("Use 1..10 repair rounds")
        with self._lock:
            self._ensure_open()
            session = self._session(session_id)
            if not self._interactive_available():
                raise RuntimeError("Repairs require the console's original interactive terminal")
            if self._repair and self._repair["status"] in {"queued", "running"}:
                raise RuntimeError("A repair is already queued or running for this project")
            self._cancel = threading.Event()
            self._repair = {
                "id": uuid.uuid4().hex,
                "session_id": session_id,
                "status": "queued",
                "goal": redact_text(goal),
                "max_rounds": max_rounds,
                "created_at": now(),
                "cancellation_requested": False,
            }
            self._selected = session_id
            self.store.event(session, "repair_queued", "Repair queued for the terminal coordinator")
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put_nowait(self._repair["id"])
            return self._repair_state() or {}

    def _ask(self, session_id: str, message: str) -> bool:
        with self._lock:
            if self._cancel.is_set() or self._closed:
                return False
            session = self._session(session_id)
            state = self.store.load(session)
            kind = {
                "awaiting_plan_approval": "plan",
                "awaiting_check_approval": "checks",
                "awaiting_scenario_check": "scenario",
                "scenario_unverified": "continue",
            }.get(str(state.get("status", "")), "confirmation")
            public: dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "session_id": session_id,
                "kind": kind,
                "message": redact_text(message),
                "created_at": now(),
            }
            # Show the exact proposal the workflow captured, not a mutable plan file.
            plan = state.get("proposed_plan")
            if isinstance(plan, dict):
                public.update(plan=plan, acceptance=plan.get("acceptance", ""))
            if kind == "checks":
                public["commands"] = state.get("proposed_checks", [])
            if kind == "scenario":
                public["acceptance"] = state.get("acceptance", "")
            pending: dict[str, Any] = {
                "public": public,
                "event": threading.Event(),
                "approved": False,
            }
            self._pending = pending
        try:
            pending["event"].wait()
            with self._lock:
                return bool(pending["approved"] and not self._cancel.is_set() and not self._closed)
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def approval(self, session_id: str, request_id: str, approved: bool) -> dict[str, Any]:
        if not isinstance(approved, bool):
            raise ValueError("Approval must be a JSON boolean")
        with self._lock:
            self._ensure_open()
            pending = self._pending
            if (
                not pending
                or pending["public"]["id"] != request_id
                or (pending["public"]["session_id"] != session_id)
                or self._cancel.is_set()
            ):
                raise RuntimeError("Approval request is stale or belongs to another session")
            pending["approved"] = approved
            self._pending = None
            pending["event"].set()
            return {"request_id": request_id, "session_id": session_id, "approved": approved}

    def approve_request(self, request_id: str, approved: bool) -> dict[str, Any]:
        with self._lock:
            if not self._pending:
                raise RuntimeError("No approval request is pending")
            return self.approval(self._pending["public"]["session_id"], request_id, approved)

    def cancel_repair(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            if not self._repair or self._repair["session_id"] != session_id:
                raise RuntimeError("No repair belongs to the requested session")
            if self._repair["status"] == "cancelled":
                return self._repair_state() or {}
            if self._repair["status"] not in {"queued", "running"}:
                raise RuntimeError("The repair has already finished")
            self._cancel.set()
            self._repair["cancellation_requested"] = True
            if self._repair["status"] == "queued":
                self._repair.update(status="cancelled", finished_at=now())
            if self._pending:
                self._pending["approved"] = False
                self._pending["event"].set()
                self._pending = None
            return self._repair_state() or {}

    def run_next_repair(self, timeout: float = 1.0) -> bool:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Repairs must run in the original terminal main thread")
        try:
            job_id = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        with self._lock:
            if (
                self._closed
                or not self._repair
                or self._repair["id"] != job_id
                or (self._repair["status"] != "queued")
            ):
                return True
            self._repair.update(status="running", started_at=now())
            job = dict(self._repair)
            cancel = self._cancel
            session = self._session(job["session_id"])
        code, error = 2, None
        try:
            runner = self._workflow_factory(
                self.store,
                session,
                command=self.command,
                max_rounds=job["max_rounds"],
                ask=lambda message: self._ask(job["session_id"], message),
                cancelled=cancel.is_set,
            )
            code = runner.run(job["goal"])
            error = None
        except (Exception, KeyboardInterrupt) as exc:
            code = 130 if isinstance(exc, KeyboardInterrupt) else 2
            error = redact_text(str(exc))[:2000]
            try:
                self.store.update(
                    session, status="needs_attention", message=error or "Repair interrupted"
                )
                self.store.event(session, "console_repair_failed", error or "Repair interrupted")
            except (OSError, ValueError):
                # Keep the in-memory failure visible even if the artifact store is unavailable.
                pass
        finally:
            with self._lock:
                if self._pending:
                    self._pending["event"].set()
                    self._pending = None
                assert self._repair is not None
                self._repair.update(
                    status="cancelled"
                    if cancel.is_set()
                    else "completed"
                    if code == 0
                    else "failed",
                    returncode=code,
                    error=error,
                    finished_at=now(),
                )
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._record_stop.set()
            if self._repair and self._repair["status"] in {"queued", "running"}:
                self.cancel_repair(self._repair["session_id"])
            if self._pending:
                self._pending["event"].set()
                self._pending = None
        self.stop_recording()
