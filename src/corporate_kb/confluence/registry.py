"""Durable Confluence sources, immutable revisions and lifecycle-bound synchronization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import frontmatter
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet

from corporate_kb.confluence.client import ConfluenceClient
from corporate_kb.confluence.models import (
    Attachment,
    ConfluenceError,
    Page,
    SourceConfig,
    SyncCancelled,
)
from corporate_kb.loaders.base import make_document
from corporate_kb.loaders.normalizer import DocumentNormalizer
from corporate_kb.models import Document

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, config_json TEXT NOT NULL, credential BLOB NOT NULL,
 paused INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT,
 last_success_at TEXT, next_check_at TEXT, last_error TEXT,
 failure_count INTEGER NOT NULL DEFAULT 0, publish_pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), index_id TEXT NOT NULL,
 status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued', message TEXT NOT NULL,
 created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT, error TEXT,
 progress_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_source_job ON jobs(source_id)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS pages (
 source_id TEXT NOT NULL REFERENCES sources(id), page_id TEXT NOT NULL,
 title TEXT NOT NULL, parent_id TEXT, version INTEGER NOT NULL,
 source_updated_at TEXT, url TEXT NOT NULL, fingerprint TEXT NOT NULL,
 revision_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'current', last_seen_job TEXT,
 PRIMARY KEY(source_id,page_id)
);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), page_id TEXT NOT NULL,
 version INTEGER NOT NULL, source_updated_at TEXT, created_at TEXT NOT NULL,
 document_path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assets (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), page_id TEXT NOT NULL,
 attachment_id TEXT NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
 media_type TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
 extraction_json TEXT, error TEXT, created_at TEXT NOT NULL,
 UNIQUE(source_id,attachment_id,version)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
 timestamp TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL,
 message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id,id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        staged = Path(handle.name)
        try:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    os.replace(staged, path)


class ConfluenceRegistry:
    """Source configuration never exposes credentials; scheduler never starts in __init__."""

    def __init__(
        self,
        state_dir: Path,
        *,
        index_exists: Callable[[str], bool],
        knowledge_root: Callable[[str], Path],
        publish: Callable[..., Any],
        archive_document: Callable[[str, Document], Any] | None = None,
        page_visibility: Callable[[str, str, bool], Any] | None = None,
        index_guard: Callable[[str], AbstractContextManager[Any]] | None = None,
        image_extractor: Any = None,
        client_factory: Callable[[SourceConfig, str], Any] | None = None,
    ) -> None:
        self.root = state_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path = self.root / "confluence.sqlite3"
        self.index_exists = index_exists
        self.knowledge_root = knowledge_root
        self.publish = publish
        self.archive_document = archive_document
        self.page_visibility = page_visibility
        self.index_guard = index_guard
        self.image_extractor = image_extractor
        self.client_factory = client_factory or ConfluenceClient
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lease: Any = None
        self._cancel: dict[str, threading.Event] = {}
        self._active_tokens: set[str] = set()
        self._state_lock = threading.RLock()
        key_path = self.root / "credentials.key"
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
        self._cipher = Fernet(key_path.read_bytes())
        with self._db() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
        self.database_path.chmod(0o600)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _source_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM sources WHERE id = ? AND archived = 0", (source_id,)
        ).fetchone()
        if row is None:
            raise KeyError("Unknown Confluence source")
        return cast(sqlite3.Row, row)

    def _source_payload(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(row["config_json"])
        payload.update(
            {
                key: row[key]
                for key in (
                    "id",
                    "created_at",
                    "updated_at",
                    "last_checked_at",
                    "last_success_at",
                    "next_check_at",
                    "last_error",
                )
            }
        )
        payload["paused"] = bool(row["paused"])
        payload["credential_configured"] = bool(row["credential"])
        active = connection.execute(
            "SELECT status FROM jobs WHERE source_id=? AND status IN ('queued','running')",
            (row["id"],),
        ).fetchone()
        payload["status"] = (
            "paused"
            if row["paused"] or not payload["enabled"]
            else active["status"]
            if active
            else "error"
            if row["last_error"]
            else "idle"
        )
        for table, key in (
            ("pages", "page_count"),
            ("revisions", "revision_count"),
            ("assets", "asset_count"),
        ):
            payload[key] = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE source_id=?", (row["id"],)
            ).fetchone()[0]
        return payload

    def get_source(self, source_id: str) -> dict[str, Any]:
        with self._db() as connection:
            return self._source_payload(connection, self._source_row(connection, source_id))

    def list_sources(self) -> list[dict[str, Any]]:
        with self._db() as connection:
            return [
                self._source_payload(connection, row)
                for row in connection.execute(
                    "SELECT * FROM sources WHERE archived=0 ORDER BY created_at,id"
                )
            ]

    def test_connection(
        self,
        config: dict[str, Any],
        token: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        """Read the root only; do not save configuration, credentials or page content."""
        values = dict(config)
        if source_id:
            with self._db() as connection:
                source = self._source_row(connection, source_id)
                values = {**json.loads(source["config_json"]), **values}
                if not token:
                    token = self._cipher.decrypt(source["credential"]).decode()
        parsed = SourceConfig.model_validate(values)
        if not self.index_exists(parsed.index_id):
            raise ValueError("Select an existing RAG index")
        if not token:
            raise ValueError("Confluence token is required")
        client = self.client_factory(parsed, token)
        try:
            root_id = client.root_id()
            page = client.page(root_id, body=False)
            return {
                "status": "connected",
                "root_page_id": root_id,
                "root_page_title": page.title,
                "root_page_version": page.version,
                "index_id": parsed.index_id,
                "deployment": "cloud" if getattr(client, "cloud", False) else "data_center",
            }
        except ConfluenceError:
            raise
        except Exception:
            raise ConfluenceError("Confluence connection check failed") from None
        finally:
            client.close()

    def upsert_source(
        self,
        config: dict[str, Any],
        token: str | None = None,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        values = dict(config)
        if "interval_minutes" in values and "schedule" not in values:
            interval = int(values.pop("interval_minutes"))
            values["schedule"] = {
                "kind": "interval" if interval else "manual",
                "interval_minutes": interval or 60,
            }
        with self._db() as connection:
            if source_id:
                previous = self._source_row(connection, source_id)
                merged = {**json.loads(previous["config_json"]), **values}
            else:
                previous = None
                merged = values
            parsed = SourceConfig.model_validate(merged)
            if not self.index_exists(parsed.index_id):
                raise ValueError("Select an existing RAG index")
            if previous and json.loads(previous["config_json"])["index_id"] != parsed.index_id:
                raise ValueError("Changing a source index requires a new source connection")
            if (
                previous
                and connection.execute(
                    "SELECT 1 FROM jobs WHERE source_id=? AND status='running'", (source_id,)
                ).fetchone()
            ):
                raise ValueError(
                    "Wait for synchronization to finish or cancel it before changing the source"
                )
            if token is not None:
                token = token.strip()
                if not token or len(token) > 8192 or "\n" in token or "\r" in token:
                    raise ValueError("A valid Confluence token is required")
                encrypted = self._cipher.encrypt(token.encode())
            elif previous:
                encrypted = previous["credential"]
            else:
                raise ValueError("Confluence token is required")
            source_id = source_id or uuid.uuid4().hex
            now = _now()
            if previous:
                connection.execute(
                    (
                        "UPDATE sources SET "
                        "config_json=?,credential=?,updated_at=?,next_check_at=?,"
                        "last_error=NULL WHERE id=?"
                    ),
                    (
                        _json(parsed.model_dump()),
                        encrypted,
                        now,
                        parsed.schedule.next_at(),
                        source_id,
                    ),
                )
            else:
                connection.execute(
                    (
                        "INSERT INTO "
                        "sources(id,config_json,credential,created_at,updated_at,next_check_at) "
                        "VALUES(?,?,?,?,?,?)"
                    ),
                    (
                        source_id,
                        _json(parsed.model_dump()),
                        encrypted,
                        now,
                        now,
                        parsed.schedule.next_at(),
                    ),
                )
        self._wake.set()
        return self.get_source(source_id)

    def set_paused(self, source_id: str, paused: bool) -> dict[str, Any]:
        with self._db() as connection:
            source = self._source_row(connection, source_id)
            config = SourceConfig.model_validate_json(source["config_json"])
            connection.execute(
                "UPDATE sources SET paused=?,updated_at=?,next_check_at=? WHERE id=?",
                (int(paused), _now(), None if paused else config.schedule.next_at(), source_id),
            )
        self._wake.set()
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> dict[str, Any]:
        with self._db() as connection:
            self._source_row(connection, source_id)
            if connection.execute(
                "SELECT 1 FROM jobs WHERE source_id=? AND status IN ('running','queued')",
                (source_id,),
            ).fetchone():
                raise ValueError(
                    "Cancel the active synchronization before disconnecting this source"
                )
            connection.execute(
                "UPDATE sources SET archived=1,paused=1,next_check_at=NULL WHERE id=?", (source_id,)
            )
        return {"id": source_id, "status": "disconnected", "archive_retained": True}

    def queue_sync(self, source_id: str) -> dict[str, Any]:
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = self._source_row(connection, source_id)
            existing = connection.execute(
                "SELECT id FROM jobs WHERE source_id=? AND status IN ('queued','running')",
                (source_id,),
            ).fetchone()
            if existing:
                job_id = existing["id"]
            else:
                config = SourceConfig.model_validate_json(source["config_json"])
                if not self.index_exists(config.index_id):
                    raise ValueError("The bound RAG index no longer exists")
                job_id = uuid.uuid4().hex
                connection.execute(
                    (
                        "INSERT INTO "
                        "jobs(id,source_id,index_id,status,message,created_at) "
                        "VALUES(?,?,?,'queued','Synchronization queued',?)"
                    ),
                    (job_id, source_id, config.index_id, _now()),
                )
        self._wake.set()
        return self.job_status(job_id)

    @staticmethod
    def _job_payload(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["type"] = "confluence"
        result["progress"] = json.loads(result.pop("progress_json"))
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self._db() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown synchronization job")
            return self._job_payload(row)

    def jobs(self, *, source_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._db() as connection:
            rows = connection.execute(
                (
                    "SELECT * FROM jobs WHERE (? IS NULL OR source_id=?) ORDER BY "
                    "created_at DESC LIMIT ?"
                ),
                (source_id, source_id, min(max(limit, 1), 500)),
            )
            return [self._job_payload(row) for row in rows]

    def job_log(self, job_id: str, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        self.job_status(job_id)
        if cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("Invalid log cursor or limit")
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, cursor, limit + 1),
            ).fetchall()
        items = [
            {
                "cursor": row["id"],
                "timestamp": row["timestamp"],
                "stage": row["stage"],
                "level": row["level"],
                "message": row["message"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "next_cursor": items[-1]["cursor"] if items else cursor,
            "has_more": len(rows) > limit,
        }

    def _log(
        self, job_id: str, stage: str, message: str, *, level: str = "info", **details: Any
    ) -> None:
        with self._state_lock:
            for token in self._active_tokens:
                message = message.replace(token, "[REDACTED]")
        message = re.sub(r"(?i)(authorization\s*[:=]\s*)([^\n]+)", r"\1[REDACTED]", message)
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT progress_json FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            progress = json.loads(row[0]) if row else {}
            progress.update(details)
            progress["stage"] = stage
            connection.execute(
                (
                    "INSERT INTO "
                    "events(job_id,timestamp,stage,level,message,details_json) "
                    "VALUES(?,?,?,?,?,?)"
                ),
                (job_id, _now(), stage, level, message[:4000], _json(details)),
            )
            connection.execute(
                "UPDATE jobs SET phase=?,message=?,progress_json=? WHERE id=?",
                (stage, message[:500], _json(progress), job_id),
            )

    def _index_progress(self, job_id: str, event: Any) -> None:
        if isinstance(event, dict):
            details = {
                key: value for key, value in event.items() if key not in {"message", "stage"}
            }
            details["index_stage"] = event.get("stage", "indexing")
            self._log(
                job_id,
                "indexing",
                str(event.get("message", event.get("stage", "Indexing"))),
                **details,
            )
        else:
            self._log(job_id, "indexing", str(event))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._state_lock:
            self._cancel.setdefault(job_id, threading.Event()).set()
        with self._db() as connection:
            connection.execute(
                (
                    "UPDATE jobs SET "
                    "status='cancelled',completed_at=?,message='Cancelled; saved "
                    "checkpoints retained' WHERE id=? AND status='queued'"
                ),
                (_now(), job_id),
            )
        return self.job_status(job_id)

    def source_pages(self, source_id: str) -> list[dict[str, Any]]:
        with self._db() as connection:
            self._source_row(connection, source_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM pages WHERE source_id=? ORDER BY title,page_id", (source_id,)
                )
            ]

    def browse_pages(
        self, source_id: str, *, offset: int = 0, limit: int = 100, query: str = ""
    ) -> dict[str, Any]:
        if offset < 0 or not 1 <= limit <= 200 or len(query) > 1000:
            raise ValueError("Invalid page pagination or query")
        with self._db() as connection:
            self._source_row(connection, source_id)
            connection.create_function("casefold", 1, lambda value: str(value).casefold())
            where = "source_id=? AND (instr(casefold(title),?)>0 OR instr(page_id,?)>0)"
            values = (source_id, query.strip().casefold(), query.strip())
            total = connection.execute(
                f"SELECT COUNT(*) FROM pages WHERE {where}", values
            ).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM pages WHERE {where} ORDER BY title,page_id LIMIT ? OFFSET ?",
                (*values, limit, offset),
            ).fetchall()
        return {
            "pages": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + len(rows) < total,
        }

    def asset_path(self, asset_id: str, *, source_id: str, version: int | None = None) -> Path:
        with self._db() as connection:
            # Disconnecting stops synchronization while retaining the corpus and
            # its evidence. Archived source assets keep the same scope checks.
            if not connection.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise KeyError("Unknown Confluence source")
            row = connection.execute(
                (
                    "SELECT a.* FROM assets a JOIN pages p ON p.source_id=a.source_id "
                    "AND p.page_id=a.page_id WHERE a.id=? AND a.source_id=? AND "
                    "p.state!='access_lost'"
                ),
                (asset_id, source_id),
            ).fetchone()
        if row is None or (version is not None and row["version"] != version):
            raise KeyError("Unknown source attachment version")
        path = (self.root / str(row["path"])).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise KeyError("Attachment archive unavailable")
        return path

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            import fcntl

            lease = (self.root / "scheduler.lock").open("a+")
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                return
            self._lease = lease
            with self._db() as connection:
                connection.execute(
                    "UPDATE jobs SET status='queued',message='Resuming after server "
                    "restart' WHERE status='running'"
                )
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="confluence-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            with self._state_lock:
                for event in self._cancel.values():
                    event.set()
            if self._thread:
                self._thread.join(timeout=35)
                if self._thread.is_alive():
                    raise RuntimeError("Confluence scheduler is still stopping")
                self._thread = None
            if self._lease:
                self._lease.close()
                self._lease = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                for source in self.list_sources():
                    if (
                        source["enabled"]
                        and not source["paused"]
                        and source["next_check_at"]
                        and source["next_check_at"] <= _now()
                    ):
                        self.queue_sync(source["id"])
                with self._db() as connection:
                    job = connection.execute(
                        "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
                    ).fetchone()
                if job and not self._stop.is_set():
                    self.run_job(job["id"])
                    continue
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Confluence scheduler iteration failed")
            self._wake.wait(timeout=5)

    def run_job(self, job_id: str) -> dict[str, Any]:
        """Run a queued job; exposed for deterministic workers and offline tests."""
        with self._db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError("Unknown synchronization job")
            if job["status"] != "queued":
                return self._job_payload(job)
            source = self._source_row(connection, job["source_id"])
            config = SourceConfig.model_validate_json(source["config_json"])
            connection.execute(
                (
                    "UPDATE jobs SET "
                    "status='running',started_at=?,completed_at=NULL,error=NULL WHERE "
                    "id=?"
                ),
                (_now(), job_id),
            )
        with self._state_lock:
            cancel = self._cancel.setdefault(job_id, threading.Event())
        token = ""
        client = None
        try:
            token = self._cipher.decrypt(source["credential"]).decode()
            with self._state_lock:
                self._active_tokens.add(token)
            self._check_cancel(cancel)
            if not self.index_exists(config.index_id):
                raise ValueError("Bound index no longer exists")
            client = self.client_factory(config, token)
            if hasattr(client, "cancel"):
                client.cancel = cancel
            with self._guard(config.index_id, cancel):
                result = self._synchronize(job_id, source["id"], config, client, cancel)
            now = _now()
            with self._db() as connection:
                connection.execute(
                    (
                        "UPDATE sources SET "
                        "last_checked_at=?,last_success_at=?,next_check_at=?,"
                        "last_error=NULL,failure_count=0 WHERE id=?"
                    ),
                    (now, now, config.schedule.next_at(), source["id"]),
                )
                connection.execute(
                    "UPDATE jobs SET status=?,completed_at=?,result_json=?,message=? WHERE id=?",
                    (
                        "completed_with_warnings" if result["warnings"] else "completed",
                        now,
                        _json(result),
                        "Synchronization completed" if result["changed"] else "No changes",
                        job_id,
                    ),
                )
        except SyncCancelled:
            self._log(
                job_id, "cancelled", "Saved checkpoints retained for the next synchronization"
            )
            with self._db() as connection:
                # Shutdown is resumable; a deliberate user cancellation is terminal.
                status = "queued" if self._stop.is_set() else "cancelled"
                connection.execute(
                    "UPDATE jobs SET status=?,completed_at=? WHERE id=?",
                    (status, None if status == "queued" else _now(), job_id),
                )
        except Exception as exc:
            if cancel.is_set():
                with self._db() as connection:
                    status = "queued" if self._stop.is_set() else "cancelled"
                    connection.execute(
                        "UPDATE jobs SET status=?,completed_at=?,message=? WHERE id=?",
                        (
                            status,
                            None if status == "queued" else _now(),
                            "Cancelled; saved checkpoints retained",
                            job_id,
                        ),
                    )
                return self.job_status(job_id)
            message = (
                str(exc)
                if isinstance(exc, (ConfluenceError, ValueError))
                else f"Synchronization failed ({type(exc).__name__})"
            )
            self._log(job_id, "failed", message, level="error")
            with self._db() as connection:
                failures = int(source["failure_count"]) + 1
                connection.execute(
                    (
                        "UPDATE sources SET "
                        "last_checked_at=?,next_check_at=?,last_error=?,failure_count=? "
                        "WHERE id=?"
                    ),
                    (
                        _now(),
                        config.schedule.next_at(failures=failures),
                        message,
                        failures,
                        source["id"],
                    ),
                )
                connection.execute(
                    "UPDATE jobs SET status='failed',completed_at=?,error=? WHERE id=?",
                    (_now(), message, job_id),
                )
        finally:
            if client is not None:
                client.close()
            with self._state_lock:
                self._active_tokens.discard(token)
                self._cancel.pop(job_id, None)
        return self.job_status(job_id)

    @contextmanager
    def _guard(self, index_id: str, cancel: threading.Event) -> Iterator[None]:
        guard = self.index_guard(index_id) if self.index_guard else nullcontext()
        # Catalog returns a threading.Lock; acquire cooperatively while another build runs.
        if hasattr(guard, "acquire") and hasattr(guard, "release"):
            while not guard.acquire(timeout=0.1):
                self._check_cancel(cancel)
            try:
                self._check_cancel(cancel)
                yield
            finally:
                guard.release()
        else:
            with guard:
                yield

    @staticmethod
    def _check_cancel(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise SyncCancelled("Synchronization cancelled")

    def _current_path(self, index_id: str, source_id: str, page_id: str) -> Path:
        # Remote names never become filesystem paths.
        safe_page = hashlib.sha256(page_id.encode()).hexdigest()
        root = self.knowledge_root(index_id).resolve()
        path = root / "confluence" / source_id / f"{safe_page}.md"
        if not path.resolve().is_relative_to(root):
            raise ValueError("Unsafe Confluence materialization path")
        return path

    def _synchronize(
        self,
        job_id: str,
        source_id: str,
        config: SourceConfig,
        client: Any,
        cancel: threading.Event,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "discovered": 0,
            "changed": 0,
            "unchanged": 0,
            "archived_versions": 0,
            "assets": 0,
            "warnings": [],
            "index_id": config.index_id,
        }
        self._log(job_id, "discovery", "Resolving root and discovering page tree")
        root_id = client.root_id()
        # Discovery must complete before interpreting absences as removals.
        page_ids = list(dict.fromkeys(client.page_ids(root_id)))
        result["discovered"] = len(page_ids)
        self._log(job_id, "discovery", "Page tree discovery completed", pages_total=len(page_ids))
        for position, page_id in enumerate(page_ids, 1):
            self._check_cancel(cancel)
            self._log(
                job_id,
                "fetch",
                "Checking page and attachment versions",
                page_id=page_id,
                pages_done=position - 1,
                pages_total=len(page_ids),
            )
            try:
                summary = client.page(page_id, body=False)
                attachments = client.attachments(page_id)
                fingerprint = hashlib.sha256(
                    _json(
                        {
                            "version": summary.version,
                            "title": summary.title,
                            "parent_id": summary.parent_id,
                            "updated_at": summary.updated_at,
                            "assets": sorted((a.id, a.version) for a in attachments),
                            "images": config.analyze_images,
                            "extractor": getattr(self.image_extractor, "identity", "none"),
                        }
                    ).encode()
                ).hexdigest()
                with self._db() as connection:
                    previous = connection.execute(
                        "SELECT * FROM pages WHERE source_id=? AND page_id=?", (source_id, page_id)
                    ).fetchone()
                current_path = self._current_path(config.index_id, source_id, page_id)
                changed = (
                    previous is None
                    or previous["fingerprint"] != fingerprint
                    or previous["state"] != "current"
                    or not current_path.is_file()
                )
                if changed:
                    page = client.page(page_id, body=True)
                    if page.version != summary.version:
                        raise ConfluenceError(
                            "Page changed during synchronization; retry will fetch a "
                            "consistent version"
                        )
                    self._archive_source(source_id, page)
                    # Inline SVG diagrams do not appear in the attachment API.
                    for svg in BeautifulSoup(page.body, "html.parser").find_all("svg"):
                        svg_text = str(svg)
                        svg_hash = hashlib.sha256(svg_text.encode()).hexdigest()
                        attachments.append(
                            Attachment(
                                id=f"inline-svg-{svg_hash}",
                                title=f"inline-diagram-{svg_hash[:12]}.svg",
                                version=page.version,
                                media_type="image/svg+xml",
                                download_url=page.url,
                                updated_at=page.updated_at,
                                extra={"inline_svg": svg_text},
                            )
                        )
                    self._log(
                        job_id,
                        "attachments",
                        "Processing page attachments",
                        page_id=page_id,
                        attachments_total=len(attachments),
                    )
                    image_blocks = []
                    asset_metadata = []
                    attachment_warning_count = len(result["warnings"])
                    for attachment in attachments:
                        self._check_cancel(cancel)
                        try:
                            asset, block = self._asset(
                                job_id, source_id, page_id, attachment, config, client, cancel
                            )
                            result["assets"] += 1
                            asset_metadata.append(
                                {
                                    "id": asset["id"],
                                    "filename": asset["title"],
                                    "mime_type": asset["media_type"],
                                    "source_id": source_id,
                                    "version": asset["version"],
                                    "updated_at": attachment.updated_at or asset["created_at"],
                                    "status": asset.get("extraction_status", "archived"),
                                    "extracted_text": block,
                                    "description": "",
                                }
                            )
                            if block:
                                image_blocks.append(block)
                            if asset.get("extraction_status") in {
                                "partial",
                                "unreadable",
                                "unsupported",
                                "error",
                            }:
                                warning = f"Image {attachment.id}: {asset['extraction_status']}"
                                result["warnings"].append(warning)
                                self._log(job_id, "images", warning, level="warning")
                        except SyncCancelled:
                            raise
                        except Exception as exc:
                            warning = f"Attachment {attachment.id}: {type(exc).__name__}"
                            result["warnings"].append(warning)
                            self._log(
                                job_id, "attachments", warning, level="warning", page_id=page_id
                            )
                    self._log(
                        job_id,
                        "normalization",
                        "Normalizing page text and image evidence",
                        page_id=page_id,
                    )
                    document = self._document(
                        config, source_id, page, fingerprint, image_blocks, asset_metadata
                    )
                    document = self._archive(
                        config.index_id, source_id, page_id, document, page.version, page.updated_at
                    )
                    _atomic_write(
                        current_path,
                        frontmatter.dumps(
                            frontmatter.Post(document.content, **document.metadata)
                        ).encode(),
                    )
                    revision_id = str(document.metadata["revision_id"])
                    with self._db() as connection:
                        stored_fingerprint = (
                            fingerprint
                            if len(result["warnings"]) == attachment_warning_count
                            else ""
                        )
                        connection.execute(
                            (
                                "INSERT INTO "
                                "pages(source_id,page_id,title,parent_id,version,source_updated_at,"
                                "url,fingerprint,revision_id,state,last_seen_job) "
                                "VALUES(?,?,?,?,?,?,?,?,?,'current',?) "
                                "ON CONFLICT(source_id,page_id) DO UPDATE SET "
                                "title=excluded.title,parent_id=excluded.parent_id,"
                                "version=excluded.version,source_updated_at=excluded.source_updated_at,"
                                "url=excluded.url,fingerprint=excluded.fingerprint,"
                                "revision_id=excluded.revision_id,state='current',"
                                "last_seen_job=excluded.last_seen_job"
                            ),
                            (
                                source_id,
                                page_id,
                                page.title,
                                page.parent_id,
                                page.version,
                                page.updated_at,
                                page.url,
                                stored_fingerprint,
                                revision_id,
                                job_id,
                            ),
                        )
                        connection.execute(
                            "UPDATE sources SET publish_pending=1 WHERE id=?", (source_id,)
                        )
                    result["changed"] += 1
                    if self.page_visibility:
                        self.page_visibility(
                            config.index_id, f"confluence:{source_id}:{page_id}", True
                        )
                else:
                    result["unchanged"] += 1
                    with self._db() as connection:
                        connection.execute(
                            "UPDATE pages SET last_seen_job=? WHERE source_id=? AND page_id=?",
                            (job_id, source_id, page_id),
                        )
                if config.include_history:
                    result["archived_versions"] += self._history(
                        job_id, source_id, config, client, page_id, summary.version, cancel
                    )
            except ConfluenceError as exc:
                if exc.status_code not in {403, 404}:
                    raise
                state = "access_lost" if exc.status_code == 403 else "unavailable"
                self._hide_page(config.index_id, source_id, page_id, state, job_id)
                warning = f"Page {page_id} unavailable (HTTP {exc.status_code}); archive retained"
                result["warnings"].append(warning)
                result["changed"] += 1
                self._log(job_id, "fetch", warning, level="warning")
            self._log(
                job_id,
                "checkpoint",
                "Page checkpoint saved",
                page_id=page_id,
                pages_done=position,
                pages_total=len(page_ids),
                changed=result["changed"],
                unchanged=result["unchanged"],
            )
        self._check_cancel(cancel)
        with self._db() as connection:
            missing = connection.execute(
                (
                    "SELECT page_id FROM pages WHERE source_id=? AND state='current' "
                    "AND (last_seen_job IS NULL OR last_seen_job!=?)"
                ),
                (source_id, job_id),
            ).fetchall()
        for row in missing:
            self._hide_page(config.index_id, source_id, row["page_id"], "removed_from_tree", job_id)
            result["changed"] += 1
        with self._db() as connection:
            dirty = connection.execute(
                "SELECT publish_pending FROM sources WHERE id=?", (source_id,)
            ).fetchone()[0]
        if dirty or result["archived_versions"]:
            self._log(
                job_id,
                "indexing",
                "Publishing changes to the selected index",
                index_id=config.index_id,
            )
            self._check_cancel(cancel)
            publication = self.publish(
                config.index_id,
                cancel=cancel,
                progress=lambda event: self._index_progress(job_id, event),
            )
            result["index_build"] = (
                publication if isinstance(publication, dict) else {"status": "completed"}
            )
            result["publication_status"] = (
                "queued"
                if isinstance(publication, dict)
                and publication.get("status") in {"queued", "running"}
                else "completed"
            )
            if isinstance(publication, dict) and publication.get("id"):
                result["index_build_job_id"] = publication["id"]
            with self._db() as connection:
                connection.execute("UPDATE sources SET publish_pending=0 WHERE id=?", (source_id,))
            self._log(
                job_id,
                "publication",
                "Index build queued"
                if result["publication_status"] == "queued"
                else "Index publication completed",
                index_id=config.index_id,
            )
        else:
            result["publication_status"] = "unchanged"
            self._log(job_id, "publication", "No changes; index build is unnecessary")
        return result

    def _hide_page(
        self, index_id: str, source_id: str, page_id: str, state: str, job_id: str
    ) -> None:
        self._current_path(index_id, source_id, page_id).unlink(missing_ok=True)
        with self._db() as connection:
            connection.execute(
                "UPDATE pages SET state=?,last_seen_job=? WHERE source_id=? AND page_id=?",
                (state, job_id, source_id, page_id),
            )
            connection.execute("UPDATE sources SET publish_pending=1 WHERE id=?", (source_id,))
        if self.page_visibility and state in {"access_lost", "unavailable"}:
            self.page_visibility(index_id, f"confluence:{source_id}:{page_id}", False)

    def _document(
        self,
        config: SourceConfig,
        source_id: str,
        page: Page,
        fingerprint: str,
        image_blocks: list[str],
        assets: list[dict[str, Any]] | None = None,
    ) -> Document:
        soup = BeautifulSoup(page.body, "html.parser")
        warnings = []
        for macro in soup.find_all(["ac:structured-macro", "ac:macro"]):
            name = macro.get("ac:name", "unknown")
            if not macro.get_text(strip=True):
                warnings.append(f"[Макрос {name}: нет доступного текстового представления]")
        content = DocumentNormalizer().html_to_markdown(str(soup))
        content = "\n\n".join([f"# {page.title}", content, *warnings, *image_blocks]).strip()
        logical_id = f"confluence:{source_id}:{page.id}"
        revision_id = hashlib.sha256(
            f"{logical_id}:{page.version}:{fingerprint}:{content}".encode()
        ).hexdigest()
        metadata: dict[str, Any] = {
            "title": page.title,
            "source_id": logical_id,
            "source_url": page.url,
            "source_kind": "confluence",
            "confluence_source_id": source_id,
            "page_id": logical_id,
            "remote_page_id": page.id,
            "page_version": page.version,
            "source_version": str(page.version),
            "revision_id": revision_id,
            "source_updated_at": page.updated_at,
            "parent_page_id": page.parent_id,
            "authority": "confluence",
            "authority_priority": 80,
            "status": "current",
            "assets": assets or [],
        }
        timestamps = [page.updated_at, *(item.get("updated_at") for item in assets or [])]
        known_dates = []
        for value in timestamps:
            if value:
                try:
                    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    known_dates.append(
                        parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
                    )
                except ValueError:
                    pass
        metadata["snapshot_updated_at"] = max(known_dates).isoformat() if known_dates else _now()
        return make_document(
            path=self._current_path(config.index_id, source_id, page.id),
            knowledge_root=self.knowledge_root(config.index_id),
            # Match the materialized Markdown loader so archive and active documents
            # have the same immutable document_id; source_kind identifies the provider.
            source_type="markdown",
            content=content,
            front_matter=metadata,
        )

    def _archive(
        self,
        index_id: str,
        source_id: str,
        page_id: str,
        document: Document,
        version: int,
        updated_at: str | None,
    ) -> Document:
        revision_id = str(document.metadata["revision_id"])
        archive = self.root / "revisions" / source_id / f"{revision_id}.json"
        if archive.is_file():
            document = Document.model_validate_json(archive.read_text())
        else:
            _atomic_write(archive, document.model_dump_json().encode())
        if self.archive_document:
            self.archive_document(index_id, document)
        with self._db() as connection:
            connection.execute(
                (
                    "INSERT OR IGNORE INTO "
                    "revisions(id,source_id,page_id,version,source_updated_at,"
                    "created_at,document_path) VALUES(?,?,?,?,?,?,?)"
                ),
                (
                    revision_id,
                    source_id,
                    page_id,
                    version,
                    updated_at,
                    _now(),
                    archive.relative_to(self.root).as_posix(),
                ),
            )
        return document

    def _archive_source(self, source_id: str, page: Page) -> None:
        payload = page.model_dump_json().encode()
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / "originals" / source_id / f"{digest}.json"
        if not path.is_file():
            _atomic_write(path, payload)

    def _history(
        self,
        job_id: str,
        source_id: str,
        config: SourceConfig,
        client: Any,
        page_id: str,
        current_version: int,
        cancel: threading.Event,
    ) -> int:
        count = 0
        self._log(job_id, "history", "Checking available historical page versions", page_id=page_id)
        with self._db() as connection:
            known = {
                row[0]
                for row in connection.execute(
                    "SELECT version FROM revisions WHERE source_id=? AND page_id=?",
                    (source_id, page_id),
                )
            }
        for version in client.versions(page_id):
            self._check_cancel(cancel)
            if version >= current_version or version in known:
                continue
            page = client.page(page_id, version=version)
            if page.version != version:
                raise ConfluenceError("Confluence did not return the requested historical version")
            self._archive_source(source_id, page)
            doc = self._document(
                config,
                source_id,
                page,
                f"historical:{version}",
                ["Историческая версия: вложения текущей страницы к ней не подставляются."],
            )
            self._archive(config.index_id, source_id, page_id, doc, version, page.updated_at)
            count += 1
        return count

    def _asset(
        self,
        job_id: str,
        source_id: str,
        page_id: str,
        attachment: Attachment,
        config: SourceConfig,
        client: Any,
        cancel: threading.Event,
    ) -> tuple[dict[str, Any], str]:
        with self._db() as connection:
            existing = connection.execute(
                "SELECT * FROM assets WHERE source_id=? AND attachment_id=? AND version=?",
                (source_id, attachment.id, attachment.version),
            ).fetchone()
        if existing:
            row = dict(existing)
            self._log(
                job_id,
                "attachments",
                "Attachment archive cache hit",
                attachment_id=attachment.id,
                version=attachment.version,
            )
        else:
            inline_svg = attachment.extra.get("inline_svg")
            data = (
                inline_svg.encode() if isinstance(inline_svg, str) else client.download(attachment)
            )
            if len(data) > config.max_attachment_bytes:
                raise ConfluenceError("Attachment exceeds configured size limit")
            digest = hashlib.sha256(data).hexdigest()
            asset_id = hashlib.sha256(
                f"{source_id}:{attachment.id}:{attachment.version}".encode()
            ).hexdigest()
            extension = Path(attachment.title).suffix.lower()
            if extension not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
                ".svg",
                ".bmp",
                ".pdf",
                ".drawio",
                ".xml",
            }:
                extension = ".bin"
            path = self.root / "assets" / source_id / f"{asset_id}{extension}"
            _atomic_write(path, data)
            row = {
                "id": asset_id,
                "source_id": source_id,
                "page_id": page_id,
                "attachment_id": attachment.id,
                "version": attachment.version,
                "title": attachment.title,
                "media_type": attachment.media_type,
                "sha256": digest,
                "path": path.relative_to(self.root).as_posix(),
                "extraction_json": None,
                "error": None,
                "created_at": _now(),
            }
            with self._db() as connection:
                connection.execute(
                    (
                        "INSERT INTO "
                        "assets(id,source_id,page_id,attachment_id,version,title,"
                        "media_type,sha256,path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                    ),
                    tuple(
                        row[key]
                        for key in (
                            "id",
                            "source_id",
                            "page_id",
                            "attachment_id",
                            "version",
                            "title",
                            "media_type",
                            "sha256",
                            "path",
                            "created_at",
                        )
                    ),
                )
            self._log(
                job_id,
                "attachments",
                "Attachment archived",
                attachment_id=attachment.id,
                version=attachment.version,
                bytes=len(data),
            )
        text = ""
        path = self.root / row["path"]
        if config.analyze_images and attachment.media_type.startswith("image/"):
            cached_extraction = json.loads(row["extraction_json"]) if row["extraction_json"] else {}
            identity = getattr(self.image_extractor, "identity", "unknown")
            if cached_extraction.get("extractor") == identity:
                text = cached_extraction["markdown"]
                row["extraction_status"] = cached_extraction.get("result", {}).get("status", "ok")
            elif self.image_extractor:
                self._log(
                    job_id, "images", "Analyzing image with GigaCode", attachment_id=attachment.id
                )
                try:
                    output = self.image_extractor.extract(
                        path,
                        cancel=cancel,
                        progress=lambda message: self._log(
                            job_id, "images", message, attachment_id=attachment.id
                        ),
                    )
                    text = output.markdown()
                    row["extraction_status"] = output.status
                    encoded = _json(
                        {
                            "markdown": text,
                            "result": output.model_dump(by_alias=True),
                            "extractor": identity,
                        }
                    )
                    with self._db() as connection:
                        connection.execute(
                            "UPDATE assets SET extraction_json=?,error=NULL WHERE id=?",
                            (encoded, row["id"]),
                        )
                except Exception as exc:
                    self._check_cancel(cancel)
                    row["extraction_status"] = "error"
                    text = (
                        f"Image extraction unavailable ({type(exc).__name__}); original retained."
                    )
                    with self._db() as connection:
                        connection.execute(
                            "UPDATE assets SET error=? WHERE id=?", (type(exc).__name__, row["id"])
                        )
            else:
                row["extraction_status"] = "error"
                text = "Image extraction unavailable: GigaCode image extractor is not configured."
        block = ""
        if text:
            block = (
                f"### Image: {attachment.title}\n\nAttachment {attachment.id}, "
                f"version {attachment.version}, SHA256 {row['sha256']}.\n\n{text}"
            )
        return row, block
