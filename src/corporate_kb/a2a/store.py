"""Durable agent identities, hashed credentials, tasks and separate A2A usage."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def clean(value: object, limit: int = 1000) -> str:
    text = re.sub(
        r"(?i)(bearer\s+|(?:token|password|secret)\s*[=:]\s*)\S+", r"\1[redacted]", str(value)
    )
    text = re.sub(r"a2a_[A-Za-z0-9_-]{20,}", "[redacted]", text)
    return " ".join(text.split())[:limit]


class A2AStore:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "agents.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, index_ids TEXT NOT NULL,
                    token_hash TEXT UNIQUE NOT NULL, token_prefix TEXT NOT NULL,
                    created_at TEXT NOT NULL, expires_at REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, last_used_at TEXT);
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    timestamp REAL NOT NULL, agent_id TEXT, agent_name TEXT,
                    method TEXT NOT NULL, status TEXT NOT NULL, duration_ms REAL NOT NULL,
                    error TEXT, request_summary TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS calls_agent ON calls(agent_id,id);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, context_id TEXT NOT NULL, state TEXT NOT NULL,
                    updated_at TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(agent_id,message_id));
                CREATE INDEX IF NOT EXISTS tasks_agent ON tasks(agent_id,updated_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(agents)")}
            if "credential_version" not in columns:
                db.execute(
                    "ALTER TABLE agents ADD COLUMN credential_version INTEGER NOT NULL DEFAULT 1"
                )
        self.path.chmod(0o600)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row: sqlite3.Row, call_count: int = 0) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "index_ids": json.loads(row["index_ids"]),
            "status": "revoked"
            if row["revoked"]
            else "expired"
            if row["expires_at"] <= time.time()
            else "active",
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"],
            "token_prefix": row["token_prefix"],
            "call_count": call_count,
            "credential_version": row["credential_version"],
        }

    def agents(self) -> list[dict[str, Any]]:
        with self.db() as db:
            return [
                self._public(row, row["call_count"])
                for row in db.execute(
                    "SELECT a.*, (SELECT COUNT(*) FROM calls c WHERE "
                    "c.agent_id=a.id) AS call_count "
                    "FROM agents a ORDER BY created_at DESC"
                )
            ]

    def issue(
        self, name: str, index_ids: list[str], days: int = 30, *, agent_id: str | None = None
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError("Agent name must contain 1-120 characters")
        if (
            not isinstance(index_ids, list)
            or not 1 <= len(index_ids) <= 100
            or not all(
                isinstance(item, str) and item.strip() and len(item) <= 100 for item in index_ids
            )
        ):
            raise ValueError("Select at least one index")
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("expires_in_days must be between 1 and 365")
        token = "a2a_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = time.time() + days * 86400
        with self.db() as db:
            if agent_id is None:
                agent_id = str(uuid4())
                db.execute(
                    (
                        "INSERT INTO "
                        "agents(id,name,index_ids,token_hash,token_prefix,created_at,expires_at)"
                        " VALUES(?,?,?,?,?,?,?)"
                    ),
                    (
                        agent_id,
                        name.strip(),
                        json.dumps(sorted(set(index_ids))),
                        digest,
                        token[:12],
                        now(),
                        expires,
                    ),
                )
            else:
                if not db.execute("SELECT id FROM agents WHERE id=?", (agent_id,)).fetchone():
                    raise KeyError("Unknown A2A agent")
                db.execute(
                    (
                        "UPDATE agents SET "
                        "token_hash=?,token_prefix=?,expires_at=?,revoked=0,"
                        "credential_version=credential_version+1 WHERE"
                        " id=?"
                    ),
                    (digest, token[:12], expires, agent_id),
                )
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return {"agent": self._public(row), "token": token, "expires_at": expires}

    def rotate(self, agent_id: str, days: int = 30) -> dict[str, Any]:
        agent = self.agent(agent_id)
        return self.issue(agent["name"], agent["index_ids"], days, agent_id=agent_id)

    def agent(self, agent_id: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown A2A agent")
        return self._public(row)

    def revoke(self, agent_id: str) -> dict[str, Any]:
        self.agent(agent_id)
        with self.db() as db:
            db.execute("UPDATE agents SET revoked=1 WHERE id=?", (agent_id,))
        return self.agent(agent_id)

    def authenticate(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token.startswith("a2a_") or len(token) > 256:
            return None
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM agents WHERE token_hash=? AND revoked=0 AND expires_at>?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
        return self._public(row) if row else None

    def authorized_snapshot(self, agent: dict[str, Any]) -> bool:
        """Recheck the exact credential generation and scope that accepted a request."""
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM agents WHERE id=? AND revoked=0 AND expires_at>? "
                "AND credential_version=?",
                (agent.get("id"), time.time(), agent.get("credential_version")),
            ).fetchone()
        return row is not None and json.loads(row["index_ids"]) == agent.get("index_ids")

    def record(
        self,
        agent: dict[str, Any] | None,
        method: str,
        status: str,
        duration_ms: float,
        summary: dict[str, Any],
        error: str | None = None,
    ) -> None:
        safe = {
            key: clean(value, 1000 if key == "query" else 200)
            for key, value in summary.items()
            if key in {"query", "action", "index_id", "version_scope", "task_id"}
        }
        with self.db() as db:
            db.execute(
                (
                    "INSERT INTO "
                    "calls(created_at,timestamp,agent_id,agent_name,method,status,duration_ms,error,request_summary)"
                    " VALUES(?,?,?,?,?,?,?,?,?)"
                ),
                (
                    now(),
                    time.time(),
                    agent["id"] if agent else None,
                    agent["name"] if agent else None,
                    clean(method, 80),
                    status,
                    round(duration_ms, 2),
                    clean(error) if error else None,
                    json.dumps(safe, ensure_ascii=False),
                ),
            )
            if agent:
                db.execute("UPDATE agents SET last_used_at=? WHERE id=?", (now(), agent["id"]))

    def calls(
        self, *, cursor: int = 0, limit: int = 100, agent_id: str | None = None
    ) -> dict[str, Any]:
        if cursor < 0 or not 1 <= limit <= 500:
            raise ValueError("Invalid log cursor or limit")
        where = "id>?"
        args: list[Any] = [cursor]
        if agent_id:
            where += " AND agent_id=?"
            args.append(agent_id)
        with self.db() as db:
            # Initial load returns the recent tail; subsequent polls only new rows.
            order = "ASC" if cursor else "DESC"
            rows = list(
                db.execute(
                    f"SELECT * FROM calls WHERE {where} ORDER BY id {order} LIMIT ?", (*args, limit)
                )
            )
        if not cursor:
            rows.reverse()
        calls = []
        for row in rows:
            item = dict(row)
            summary = json.loads(item.pop("request_summary"))
            item.update(summary)
            item["request_summary"] = summary
            item.pop("timestamp")
            calls.append(item)
        return {"calls": calls, "next_cursor": max([cursor, *(row["id"] for row in rows)])}

    def stats(self) -> dict[str, Any]:
        with self.db() as db:
            counts = dict(
                db.execute("SELECT status,COUNT(*) FROM calls GROUP BY status").fetchall()
            )
            recent = db.execute(
                "SELECT COUNT(*) FROM calls WHERE timestamp>?", (time.time() - 60,)
            ).fetchone()[0]
            active = db.execute(
                "SELECT COUNT(*) FROM agents WHERE revoked=0 AND expires_at>?", (time.time(),)
            ).fetchone()[0]
        return {
            "total_calls": sum(counts.values()),
            "successful_calls": counts.get("success", 0),
            "failed_calls": counts.get("error", 0),
            "active_agents": active,
            "calls_last_minute": recent,
        }

    def create_task(
        self, agent_id: str, message_id: str, fingerprint: str, task: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fingerprint,payload FROM tasks WHERE agent_id=? AND message_id=?",
                (agent_id, message_id),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ValueError("messageId was already used with a different request")
                return json.loads(old["payload"]), False
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)",
                (
                    task["id"],
                    agent_id,
                    message_id,
                    fingerprint,
                    task["contextId"],
                    task["status"]["state"],
                    task["status"]["timestamp"],
                    json.dumps(task, ensure_ascii=False),
                ),
            )
        return task, True

    def existing_task(
        self,
        agent_id: str,
        message_id: str,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT fingerprint,payload FROM tasks WHERE agent_id=? AND message_id=?",
                (agent_id, message_id),
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ValueError("messageId was already used with a different request")
        payload: dict[str, Any] = json.loads(row["payload"])
        return payload

    def task(self, agent_id: str, task_id: str) -> dict[str, Any]:
        with self.db() as db:
            row = db.execute(
                "SELECT payload FROM tasks WHERE id=? AND agent_id=?", (task_id, agent_id)
            ).fetchone()
        if row is None:
            raise KeyError("Task not found")
        payload: dict[str, Any] = json.loads(row[0])
        return payload

    def save_task(
        self,
        agent_id: str,
        task: dict[str, Any],
        *,
        credential_version: int | None = None,
        index_ids: list[str] | None = None,
    ) -> bool:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if credential_version is not None:
                agent = db.execute(
                    "SELECT index_ids FROM agents WHERE id=? AND revoked=0 AND expires_at>? "
                    "AND credential_version=?",
                    (agent_id, time.time(), credential_version),
                ).fetchone()
                if agent is None or (
                    index_ids is not None and json.loads(agent["index_ids"]) != index_ids
                ):
                    return False
            updated = db.execute(
                (
                    "UPDATE tasks SET state=?,updated_at=?,payload=? WHERE id=? "
                    "AND agent_id=? AND state NOT IN "
                    "('TASK_STATE_CANCELED','TASK_STATE_COMPLETED','TASK_STATE_FAILED',"
                    "'TASK_STATE_REJECTED')"
                ),
                (
                    task["status"]["state"],
                    task["status"]["timestamp"],
                    json.dumps(task, ensure_ascii=False),
                    task["id"],
                    agent_id,
                ),
            )
            return bool(updated.rowcount)

    def list_tasks(self, agent_id: str, params: dict[str, Any]) -> dict[str, Any]:
        if "includeArtifacts" in params and not isinstance(params["includeArtifacts"], bool):
            raise ValueError("includeArtifacts must be a boolean")
        size = params.get("pageSize", 50)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100:
            raise ValueError("pageSize must be between 1 and 100")
        raw = params.get("pageToken", "")
        if raw == "":
            raw = "0"
        if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 12:
            raise ValueError("Invalid pageToken")
        offset = int(raw)
        where, values = "agent_id=?", [agent_id]
        for field, column in (
            ("contextId", "context_id"),
            ("status", "state"),
            ("statusTimestampAfter", "updated_at"),
        ):
            if params.get(field) is not None:
                value = params[field]
                if not isinstance(value, str) or not value.strip() or len(value) > 256:
                    raise ValueError(f"{field} must contain 1-256 characters")
                if field == "status" and value not in {
                    "TASK_STATE_SUBMITTED",
                    "TASK_STATE_WORKING",
                    "TASK_STATE_INPUT_REQUIRED",
                    "TASK_STATE_COMPLETED",
                    "TASK_STATE_CANCELED",
                    "TASK_STATE_FAILED",
                    "TASK_STATE_REJECTED",
                    "TASK_STATE_AUTH_REQUIRED",
                }:
                    raise ValueError("Unknown task status")
                if field == "statusTimestampAfter":
                    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if stamp.tzinfo is None:
                        raise ValueError("statusTimestampAfter must include a timezone")
                    value = stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
                where += f" AND {column}{'>=' if field == 'statusTimestampAfter' else '='}?"
                values.append(value)
        with self.db() as db:
            total = db.execute(f"SELECT COUNT(*) FROM tasks WHERE {where}", values).fetchone()[0]
            rows = db.execute(
                f"SELECT payload FROM tasks WHERE {where} "
                "ORDER BY updated_at DESC,id LIMIT ? OFFSET ?",
                (*values, size, offset),
            ).fetchall()
        return {
            "tasks": [json.loads(row[0]) for row in rows],
            "totalSize": total,
            "pageSize": size,
            "nextPageToken": str(offset + len(rows)) if offset + len(rows) < total else "",
        }

    def recover(self) -> None:
        with self.db() as db:
            rows = db.execute(
                "SELECT agent_id,payload FROM tasks WHERE state IN "
                "('TASK_STATE_SUBMITTED','TASK_STATE_WORKING')"
            ).fetchall()
        for row in rows:
            task = json.loads(row["payload"])
            task["status"] = {"state": "TASK_STATE_FAILED", "timestamp": now()}
            task["metadata"] = {
                "error": "Server restarted before the task finished; resend with a new messageId"
            }
            self.save_task(row["agent_id"], task)
