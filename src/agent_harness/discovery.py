"""Untrusted resource suggestions. Discovering a URL never authorizes contacting it."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from agent_harness.integrations_config import IntegrationSettings, normalize_repository_url
from agent_harness.store import timestamp

_LINK = re.compile(r"(?:https?://|ssh://)[^\s<>\"'`]+|\bgit@[a-zA-Z0-9.-]+:[^\s<>\"'`]+")
_MAX_PER_OWNER = 200


def resource_url(value: str) -> str | None:
    """Remove tracking/secret queries and reject embedded HTTP credentials; no I/O."""
    value = value.rstrip(".,;:!?)]}")
    if len(value) > 2000 or any(c.isspace() or ord(c) < 32 for c in value):
        return None
    if value.startswith(("git@", "ssh://")):
        try:
            return normalize_repository_url(value)
        except ValueError:
            return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https", "ssh"}
            or not parsed.hostname
            or parsed.password
            or (parsed.username and parsed.scheme != "ssh")
        ):
            return None
        _ = parsed.port
        if parsed.scheme == "ssh" and (
            not re.fullmatch(r"[a-zA-Z0-9_.-]+", parsed.username or "git")
            or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", parsed.path)
        ):
            return None
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, "", ""))
    except ValueError:
        return None


def _kind(url: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if url.startswith("git@") or parsed.scheme == "ssh" or path.endswith(".git"):
        return "repository"
    if path.endswith("/mcp"):
        return "mcp"
    if path.endswith("/a2a"):
        return "a2a"
    return "web"


class DiscoveryStore:
    def __init__(self, state_dir: Path, retention_days: int = 30) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "discoveries.sqlite3"
        self.retention_days = retention_days
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS discoveries ("
                "id TEXT PRIMARY KEY, owner TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL, "
                "status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, "
                "UNIQUE(owner,url));"
                "CREATE TABLE IF NOT EXISTS sightings ("
                "discovery_id TEXT NOT NULL, run_id TEXT NOT NULL, index_id TEXT NOT NULL, "
                "PRIMARY KEY(discovery_id,run_id,index_id), "
                "FOREIGN KEY(discovery_id) REFERENCES discoveries(id) ON DELETE CASCADE);"
                "CREATE INDEX IF NOT EXISTS discovery_owner ON discoveries(owner,updated);"
            )
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def scan(self, owner: str, run: dict[str, Any]) -> None:
        """Caller supplies an ACL-filtered run; model-authored answers are never scanned."""
        inputs = [(str(run.get("question", "")), "", None)]
        inputs.extend(
            (str(item.get("text", "")), str(item["index_id"]), item.get("url"))
            for item in run.get("evidence", [])
            if item.get("kind", "source") == "source"
        )
        now = timestamp()
        candidates: set[tuple[str, str]] = set()
        for text, index_id, source_url in inputs:
            for match in _LINK.finditer(text[:50000]):
                url = resource_url(match[0])
                if not url or url == resource_url(str(source_url or "")):
                    continue
                candidates.add((url, index_id))
                if len(candidates) >= 40:
                    break
            if len(candidates) >= 40:
                break
        if not candidates:
            return
        with self._connect() as db:
            for url, index_id in sorted(candidates):
                db.execute(
                    "INSERT OR IGNORE INTO discoveries VALUES(?,?,?,?,?,?,?)",
                    (uuid4().hex, owner, url, _kind(url), "discovered", now, now),
                )
                row = db.execute(
                    "SELECT id FROM discoveries WHERE owner=? AND url=?", (owner, url)
                ).fetchone()
                assert row is not None
                db.execute(
                    "INSERT OR IGNORE INTO sightings VALUES(?,?,?)", (row[0], run["id"], index_id)
                )
            cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
            db.execute("DELETE FROM discoveries WHERE created<?", (cutoff,))
            db.execute(
                "DELETE FROM discoveries WHERE id IN (SELECT id FROM discoveries "
                "WHERE owner=? ORDER BY created DESC LIMIT -1 OFFSET ?)",
                (owner, _MAX_PER_OWNER),
            )
            db.execute(
                "DELETE FROM discoveries WHERE id IN (SELECT id FROM discoveries "
                "ORDER BY created DESC LIMIT -1 OFFSET 10000)"
            )

    def list(
        self,
        owner: str,
        allowed: set[str],
        settings: IntegrationSettings,
        *,
        run_id: str | None = None,
        requests: bool = False,
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT d.*,s.run_id,s.index_id FROM discoveries d "
                "JOIN sightings s ON s.discovery_id=d.id WHERE "
                + ("d.status='requested'" if requests else "d.owner=?")
                + (" AND s.run_id=?" if run_id else "")
                + " ORDER BY d.updated DESC LIMIT 8000",
                (() if requests else (owner,)) + ((run_id,) if run_id else ()),
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            # Only an explicit access request shares a URL with administrators.
            if not requests and row["index_id"] and row["index_id"] not in allowed:
                continue
            if row["id"] in output:
                continue
            connected = next(
                (
                    ("repo:" if kind == "repository" else "connection:") + resource.id
                    for kind, resources in (
                        ("repository", settings.repositories),
                        ("connection", settings.connections),
                    )
                    for resource in resources
                    if resource_url(resource.url) == row["url"]
                    and resource.enabled
                    and (
                        settings.sandbox.enabled
                        if kind == "repository"
                        else getattr(resource, "kind", "") == "a2a"
                        or bool(getattr(resource, "allowed_tools", []))
                    )
                    and (not resource.allowed_owners or row["owner"] in resource.allowed_owners)
                ),
                None,
            )
            output[row["id"]] = {
                "id": row["id"],
                "url": row["url"],
                "kind": row["kind"],
                "name": urlsplit(row["url"]).hostname or "Новый источник",
                "status": "configured" if connected else row["status"],
                "run_id": "" if requests else row["run_id"],
                "index_id": "" if requests else row["index_id"],
                "owner_id": row["owner"],
                "created_at": row["created"],
                "resource_id": connected,
            }
        return list(output.values())[:_MAX_PER_OWNER]

    def set_status(self, owner: str, discovery_id: str, status: str) -> None:
        if status not in {"requested", "dismissed"}:
            raise ValueError("Unsupported discovery status")
        with self._connect() as db:
            result = db.execute(
                "UPDATE discoveries SET status=?,updated=? WHERE id=? AND owner=?",
                (status, timestamp(), discovery_id, owner),
            )
            if not result.rowcount:
                raise KeyError("Resource not found")

    def remove_run(self, owner: str, run_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "DELETE FROM sightings WHERE run_id=? AND discovery_id IN "
                "(SELECT id FROM discoveries WHERE owner=?)",
                (run_id, owner),
            )
            db.execute(
                "DELETE FROM discoveries WHERE owner=? AND NOT EXISTS "
                "(SELECT 1 FROM sightings WHERE discovery_id=discoveries.id)",
                (owner,),
            )
