"""Durable Git sources, immutable skill releases and a lifecycle-bound scheduler."""

from __future__ import annotations

import base64
import difflib
import json
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gigacode_graph.config import GraphSettings
from gigacode_graph.sources import RepositorySourceManager, RepositorySpec
from skill_registry.models import SkillSnapshot, SourceConfig, safe_relative_path, scan_skills

_SOURCE_FIELDS = set(SourceConfig.model_fields)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    config_revision INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    next_check_at TEXT,
    last_error TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    publish_pending INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    relative_path TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    latest_revision TEXT,
    published_revision TEXT,
    retired INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, relative_path)
);
CREATE TABLE IF NOT EXISTS releases (
    skill_id TEXT NOT NULL REFERENCES skills(id),
    revision TEXT NOT NULL,
    version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    PRIMARY KEY(skill_id, revision),
    UNIQUE(skill_id, version)
);
CREATE TABLE IF NOT EXISTS release_files (
    skill_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    path TEXT NOT NULL,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    executable INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(skill_id, revision, path),
    FOREIGN KEY(skill_id, revision) REFERENCES releases(skill_id, revision)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active_per_source
    ON jobs(source_id) WHERE status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS skills_source ON skills(source_id);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _public_sync_error(error: str) -> str:
    """Git helpers and redirects may echo secrets; never persist their raw stderr."""
    if error.startswith("Git operation timed out"):
        return "Git operation timed out; check repository connectivity and timeout settings"
    if error.startswith("Git operation failed"):
        diagnostic = error.lower()
        if any(
            term in diagnostic
            for term in (
                "authentication",
                "permission denied",
                "access denied",
                "could not read username",
                "403",
                "401",
            )
        ):
            return (
                "Git access failed; check repository permissions and the server credential helper"
            )
        if "couldn't find remote ref" in diagnostic or "not our ref" in diagnostic:
            return "Git ref was not found; check the configured branch, tag or commit"
        if (
            "repository not found" in diagnostic
            or "does not appear to be a git repository" in diagnostic
        ):
            return "Git repository was not found or access denied; check URL and server credentials"
        if any(
            term in diagnostic for term in ("could not resolve", "connection", "unable to access")
        ):
            return (
                "Git connection failed; check repository connectivity and server Git configuration"
            )
        return "Git operation failed; check repository URL, ref and server Git credential helper"
    return error[:4000]


def _next_check(source: dict[str, Any], *, failures: int = 0) -> str | None:
    if not source["enabled"] or source["archived"] or source["interval_minutes"] == 0:
        return None
    interval = int(source["interval_minutes"])
    # Retry with bounded exponential backoff. The configured interval remains the minimum.
    minutes = max(interval, min(1440, 2 ** min(failures, 10))) if failures else interval
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")


class SkillsRegistry:
    """One registry owns a SQLite database and Git checkouts beneath ``root``.

    Construction never starts threads. The application must call ``start``/``stop``
    from its lifespan. Jobs are persisted and the queue is shared by manual and
    scheduled requests. Run one scheduler process per registry root.
    """

    def __init__(self, root: Path, *, git_timeout_seconds: int = 60) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = self.root / "registry.sqlite3"
        self.git_timeout_seconds = git_timeout_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._source_locks: dict[str, threading.Lock] = {}
        self._source_locks_lock = threading.Lock()
        self._scheduler_lock_file: Any = None
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(sources)")}
            if "publish_pending" not in columns:
                connection.execute(
                    "ALTER TABLE sources ADD COLUMN publish_pending INTEGER NOT NULL DEFAULT 0"
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
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
    def _source(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        config = json.loads(result.pop("config_json"))
        result.update(config)
        result["archived"] = bool(result["archived"])
        return result

    @classmethod
    def _require_source(cls, connection: sqlite3.Connection, source_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown skills source: {source_id}")
        return cls._source(row)

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [
                self._source(row)
                for row in connection.execute("SELECT * FROM sources ORDER BY created_at, id")
            ]

    def save_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - _SOURCE_FIELDS - {"id"}
        if unknown:
            raise ValueError(f"Unknown source fields: {', '.join(sorted(unknown))}")
        source_id = str(payload.get("id") or uuid.uuid4())
        try:
            source_id = str(uuid.UUID(source_id))
        except ValueError as exc:
            raise ValueError("Source id must be a UUID") from exc
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
            previous = self._source(row) if row else None
            values = {key: previous[key] for key in _SOURCE_FIELDS} if previous else {}
            values.update({key: value for key, value in payload.items() if key != "id"})
            config = SourceConfig.model_validate(values)
            config_json = config.model_dump_json()
            next_check = now if config.enabled and config.interval_minutes else None
            if previous:
                publish_pending = bool(previous["publish_pending"]) or (
                    config.auto_publish and not previous["auto_publish"]
                )
                connection.execute(
                    """UPDATE sources SET config_json = ?, config_revision = config_revision + 1,
                    archived = 0, updated_at = ?, next_check_at = ?, publish_pending = ?
                    WHERE id = ?""",
                    (config_json, now, next_check, int(publish_pending), source_id),
                )
            else:
                connection.execute(
                    """INSERT INTO sources
                    (id, config_json, created_at, updated_at, next_check_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (source_id, config_json, now, now, next_check),
                )
            result = self._require_source(connection, source_id)
        self._wake.set()
        return result

    def delete_source(self, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = self._require_source(connection, source_id)
            config = {key: source[key] for key in _SOURCE_FIELDS}
            config["enabled"] = False
            now = _now()
            connection.execute(
                """UPDATE sources SET archived = 1, config_json = ?, next_check_at = NULL,
                updated_at = ?, config_revision = config_revision + 1 WHERE id = ?""",
                (json.dumps(config), now, source_id),
            )
            connection.execute(
                "UPDATE skills SET retired = 1, updated_at = ? WHERE source_id = ?",
                (now, source_id),
            )
            connection.execute(
                """UPDATE jobs SET status = 'failed', finished_at = ?, error = ?
                WHERE source_id = ? AND status = 'queued'""",
                (now, "Source was archived", source_id),
            )
            result = self._require_source(connection, source_id)
        return result

    def _manager(self, cache_path: Path) -> RepositorySourceManager:
        return RepositorySourceManager(
            GraphSettings(
                _env_file=None,
                repository_cache_path=cache_path,
                git_timeout_seconds=self.git_timeout_seconds,
            )
        )

    def validate_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Allow an existing source's partial update without saving it.
        source_id = payload.get("id")
        values: dict[str, Any] = {}
        if source_id:
            with self._connect() as connection:
                source = self._require_source(connection, str(source_id))
                values.update({key: source[key] for key in _SOURCE_FIELDS})
        unknown = set(payload) - _SOURCE_FIELDS - {"id"}
        if unknown:
            raise ValueError(f"Unknown source fields: {', '.join(sorted(unknown))}")
        values.update({key: value for key, value in payload.items() if key != "id"})
        config = SourceConfig.model_validate(values)
        with tempfile.TemporaryDirectory(prefix="skill-preview-", dir=self.root) as temporary:
            manager = self._manager(Path(temporary))
            paths, records = manager.materialize([RepositorySpec(config.git_url, config.ref)])
            snapshots = scan_skills(paths[0], config.skills_path, config.recursive)
        return {
            "skills": [snapshot.summary() for snapshot in snapshots],
            "commit": records[0].commit,
            "git_url": config.git_url,
            "ref": config.ref,
            "skills_path": config.skills_path,
        }

    @staticmethod
    def _skill(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["skill_id"] = result["id"]
        result["path"] = result["relative_path"]
        result["retired"] = bool(result["retired"])
        for key, revision in (
            ("version", result["latest_revision"]),
            ("published_version", result["published_revision"]),
        ):
            release = connection.execute(
                "SELECT version FROM releases WHERE skill_id = ? AND revision = ?",
                (result["id"], revision),
            ).fetchone()
            result[key] = release["version"] if release else None
        source = connection.execute(
            "SELECT config_json FROM sources WHERE id = ?", (result["source_id"],)
        ).fetchone()
        result["source_name"] = json.loads(source["config_json"])["name"] if source else None
        return result

    @classmethod
    def _require_skill(cls, connection: sqlite3.Connection, skill_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown skill: {skill_id}")
        return cls._skill(connection, row)

    def list_skills(
        self,
        query: str = "",
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        *,
        published_only: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("limit must be 1-500 and offset must be non-negative")
        clauses: list[str] = []
        arguments: list[Any] = []
        table = "skills AS s"
        columns = "s.*"
        name = "s.name"
        description = "s.description"
        if published_only:
            table += (
                " JOIN releases AS r ON r.skill_id = s.id AND r.revision = s.published_revision"
            )
            clauses.append("s.retired = 0")
            columns += ", r.manifest_json AS published_manifest"
            name = "json_extract(r.manifest_json, '$.name')"
            description = "json_extract(r.manifest_json, '$.description')"
        if query:
            clauses.append(f"(instr(lower({name}), ?) > 0 OR instr(lower({description}), ?) > 0)")
            arguments.extend([query.lower(), query.lower()])
        if source_id is not None:
            clauses.append("s.source_id = ?")
            arguments.append(source_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM " + table + where, arguments
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT "
                + columns
                + " FROM "
                + table
                + where
                + f" ORDER BY {name}, s.id LIMIT ? OFFSET ?",
                [*arguments, limit, offset],
            ).fetchall()
            skills = []
            for row in rows:
                skill = self._skill(connection, row)
                if published_only:
                    manifest = json.loads(skill.pop("published_manifest"))
                    skill["name"] = manifest["name"]
                    skill["description"] = manifest["description"]
                skills.append(skill)
            return {"skills": skills, "total": total, "limit": limit, "offset": offset}

    def skill_detail(self, skill_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            skill = self._require_skill(connection, skill_id)
            versions = []
            for row in connection.execute(
                "SELECT manifest_json FROM releases WHERE skill_id = ? ORDER BY version DESC",
                (skill_id,),
            ):
                manifest = json.loads(row["manifest_json"])
                files = manifest.pop("files")
                manifest["file_count"] = len(files)
                manifest["total_bytes"] = sum(file["size"] for file in files)
                manifest["published"] = manifest["revision"] == skill["published_revision"]
                versions.append(manifest)
            return {"skill": skill, "versions": versions}

    @classmethod
    def _require_release(
        cls, connection: sqlite3.Connection, skill_id: str, revision: str | None
    ) -> dict[str, Any]:
        skill = cls._require_skill(connection, skill_id)
        if revision is None:
            if skill["retired"]:
                raise ValueError("Skill is retired; select an explicit historical revision")
            revision = skill["published_revision"]
            if revision is None:
                raise ValueError("Skill has no published release")
        row = connection.execute(
            "SELECT manifest_json FROM releases WHERE skill_id = ? AND revision = ?",
            (skill_id, revision),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown release for {skill_id}: {revision}")
        return dict(json.loads(row["manifest_json"]))

    def get_release(self, skill_id: str, revision: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            return self._require_release(connection, skill_id, revision)

    def read_file(self, skill_id: str, revision: str | None, path: str) -> dict[str, Any]:
        path = safe_relative_path(path)
        with self._connect() as connection:
            release = self._require_release(connection, skill_id, revision)
            row = connection.execute(
                "SELECT content, sha256 FROM release_files "
                "WHERE skill_id = ? AND revision = ? AND path = ?",
                (skill_id, release["revision"], path),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown release file: {path}")
            content = bytes(row["content"])
        try:
            body = content.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            body = base64.b64encode(content).decode("ascii")
            encoding = "base64"
        return {
            "path": path,
            "encoding": encoding,
            "content": body,
            "size": len(content),
            "sha256": row["sha256"],
        }

    def publish(self, skill_id: str, revision: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            release = self._require_release(connection, skill_id, revision)
            source = self._require_source(connection, release["source_id"])
            skill = self._require_skill(connection, skill_id)
            if source["archived"] or skill["retired"]:
                raise ValueError("Cannot publish a retired skill; restore it in the source first")
            connection.execute(
                "UPDATE skills SET published_revision = ?, updated_at = ? WHERE id = ?",
                (revision, _now(), skill_id),
            )
            return self._require_skill(connection, skill_id)

    def diff(self, skill_id: str, from_revision: str, to_revision: str) -> dict[str, Any]:
        before = self.get_release(skill_id, from_revision)
        after = self.get_release(skill_id, to_revision)
        before_files = {file["path"]: file for file in before["files"]}
        after_files = {file["path"]: file for file in after["files"]}
        changes: list[dict[str, Any]] = []
        budget = 200_000
        for path in sorted(set(before_files) | set(after_files)):
            old = before_files.get(path)
            new = after_files.get(path)
            if old == new:
                continue
            change: dict[str, Any] = {
                "path": path,
                "status": "added" if old is None else "removed" if new is None else "modified",
                "before": old,
                "after": new,
            }
            old_file = self.read_file(skill_id, from_revision, path) if old else None
            new_file = self.read_file(skill_id, to_revision, path) if new else None
            if sum(file["size"] for file in (old_file, new_file) if file) > 256 * 1024:
                change["diff"] = ""
                change["truncated"] = True
                change["reason"] = "Open pinned files individually to compare files over 256 KiB"
            elif all(file is None or file["encoding"] == "utf-8" for file in (old_file, new_file)):
                text = "".join(
                    difflib.unified_diff(
                        old_file["content"].splitlines(keepends=True) if old_file else [],
                        new_file["content"].splitlines(keepends=True) if new_file else [],
                        fromfile=f"a/{path}",
                        tofile=f"b/{path}",
                    )
                )
                change["diff"] = text[:budget]
                change["truncated"] = len(text) > budget
                budget = max(0, budget - len(change["diff"]))
            else:
                change["binary"] = True
            changes.append(change)
        return {
            "skill_id": skill_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "files": changes,
        }

    def check_updates(self, installed: list[dict[str, Any]]) -> dict[str, Any]:
        if len(installed) > 500:
            raise ValueError("At most 500 installed skills can be checked at once")
        updates: list[dict[str, Any]] = []
        with self._connect() as connection:
            for item in installed:
                skill_id = item.get("skill_id") or item.get("id")
                if not isinstance(skill_id, str) or not skill_id:
                    raise ValueError("Each installed skill needs a skill_id")
                revision = item.get("revision")
                try:
                    skill = self._require_skill(connection, skill_id)
                except KeyError:
                    updates.append(
                        {"skill_id": skill_id, "status": "unknown", "revision": revision}
                    )
                    continue
                published = skill["published_revision"]
                status = (
                    "retired"
                    if skill["retired"]
                    else "unpublished"
                    if not published
                    else "current"
                    if published == revision
                    else "pinned"
                    if item.get("pinned")
                    else "update_available"
                )
                updates.append(
                    {
                        "skill_id": skill_id,
                        "name": skill["name"],
                        "status": status,
                        "revision": revision,
                        "published_revision": published,
                        "published_version": skill["published_version"],
                        "update_available": status == "update_available",
                    }
                )
        return {"updates": updates, "checked_at": _now()}

    @staticmethod
    def _job(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        source = connection.execute(
            "SELECT config_json FROM sources WHERE id = ?", (result["source_id"],)
        ).fetchone()
        result["source_name"] = json.loads(source["config_json"])["name"] if source else None
        return result

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be 1-500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._job(connection, row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown synchronization job: {job_id}")
            return self._job(connection, row)

    def queue_sync(self, source_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            source = self._require_source(connection, source_id)
            if source["archived"]:
                raise ValueError("Source is archived")
            row = connection.execute(
                "SELECT * FROM jobs WHERE source_id = ? AND status IN ('queued', 'running')",
                (source_id,),
            ).fetchone()
            if row is None:
                job_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO jobs (id, source_id, status, created_at) "
                    "VALUES (?, ?, 'queued', ?)",
                    (job_id, source_id, _now()),
                )
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            result = self._job(connection, row)
        self._wake.set()
        return result

    def sync_now(self, source_id: str) -> dict[str, Any]:
        job = self.queue_sync(source_id)
        self._run_job(job["id"])
        return self.get_job(job["id"])

    def _source_lock(self, source_id: str) -> threading.Lock:
        with self._source_locks_lock:
            return self._source_locks.setdefault(source_id, threading.Lock())

    def _run_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        # Across processes the partial unique index and atomic claim also prevent duplicate work.
        with self._source_lock(job["source_id"]):
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                claim = connection.execute(
                    "UPDATE jobs SET status = 'running', started_at = ? "
                    "WHERE id = ? AND status = 'queued'",
                    (_now(), job_id),
                )
                if claim.rowcount != 1:
                    return
                source = self._require_source(connection, job["source_id"])
            try:
                if source["archived"]:
                    raise ValueError("Source was archived")
                manager = self._manager(self.root / "checkouts" / source["id"])
                paths, records = manager.materialize(
                    [RepositorySpec(source["git_url"], source["ref"])],
                    cancel_event=self._stop,
                )
                snapshots = scan_skills(paths[0], source["skills_path"], source["recursive"])
                self._record_success(job_id, source, snapshots, records[0].commit)
            except Exception as exc:
                self._record_failure(job_id, source, str(exc))

    def _record_success(
        self,
        job_id: str,
        source: dict[str, Any],
        snapshots: list[SkillSnapshot],
        commit: str | None,
    ) -> None:
        now = _now()
        result: dict[str, Any] = {
            "discovered": len(snapshots),
            "created": 0,
            "published": 0,
            "retired": 0,
            "unchanged": 0,
            "commit": commit,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_source(connection, source["id"])
            if current["archived"] or current["config_revision"] != source["config_revision"]:
                raise ValueError("Source configuration changed during synchronization; retry")
            observed: set[str] = set()
            for snapshot in snapshots:
                skill_id = f"{source['id']}:{snapshot.relative_path}"
                observed.add(skill_id)
                previous = connection.execute(
                    "SELECT * FROM skills WHERE id = ?", (skill_id,)
                ).fetchone()
                connection.execute(
                    """INSERT INTO skills
                    (id, source_id, relative_path, name, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name, description = excluded.description,
                    retired = 0, updated_at = excluded.updated_at""",
                    (
                        skill_id,
                        source["id"],
                        snapshot.relative_path,
                        snapshot.name,
                        snapshot.description,
                        now,
                        now,
                    ),
                )
                exists = connection.execute(
                    "SELECT version FROM releases WHERE skill_id = ? AND revision = ?",
                    (skill_id, snapshot.revision),
                ).fetchone()
                if exists is None:
                    version = connection.execute(
                        "SELECT COALESCE(MAX(version), 0) + 1 FROM releases WHERE skill_id = ?",
                        (skill_id,),
                    ).fetchone()[0]
                    manifest = {
                        "skill_id": skill_id,
                        "name": snapshot.name,
                        "description": snapshot.description,
                        "revision": snapshot.revision,
                        "version": version,
                        "source_id": source["id"],
                        "relative_path": snapshot.relative_path,
                        "git_url": source["git_url"],
                        "ref": source["ref"],
                        "skills_path": source["skills_path"],
                        "commit": commit,
                        "created_at": now,
                        "metadata": snapshot.metadata,
                        "files": [file.manifest() for file in snapshot.files],
                    }
                    connection.execute(
                        "INSERT INTO releases (skill_id, revision, version, manifest_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            skill_id,
                            snapshot.revision,
                            version,
                            json.dumps(manifest, ensure_ascii=False),
                        ),
                    )
                    connection.executemany(
                        "INSERT INTO release_files "
                        "(skill_id, revision, path, content, sha256, executable) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (
                                skill_id,
                                snapshot.revision,
                                file.path,
                                file.content,
                                file.sha256,
                                int(file.executable),
                            )
                            for file in snapshot.files
                        ],
                    )
                    result["created"] += 1
                else:
                    result["unchanged"] += 1
                connection.execute(
                    "UPDATE skills SET latest_revision = ? WHERE id = ?",
                    (snapshot.revision, skill_id),
                )
                # A no-op poll must not undo an operator's rollback. Only a newly observed
                # package or a restored skill advances the publication pointer automatically.
                changed = previous is None or previous["latest_revision"] != snapshot.revision
                restored = previous is not None and bool(previous["retired"])
                if source["auto_publish"] and (changed or restored or source["publish_pending"]):
                    connection.execute(
                        "UPDATE skills SET published_revision = ? WHERE id = ?",
                        (snapshot.revision, skill_id),
                    )
                    result["published"] += 1
            for row in connection.execute(
                "SELECT id FROM skills WHERE source_id = ? AND retired = 0", (source["id"],)
            ).fetchall():
                if row["id"] not in observed:
                    connection.execute(
                        "UPDATE skills SET retired = 1, updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    result["retired"] += 1
            connection.execute(
                """UPDATE sources SET last_checked_at = ?, last_success_at = ?,
                next_check_at = ?, last_error = NULL, failure_count = 0,
                publish_pending = 0 WHERE id = ?""",
                (now, now, _next_check(source), source["id"]),
            )
            connection.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = ?, result_json = ? "
                "WHERE id = ?",
                (now, json.dumps(result), job_id),
            )

    def _record_failure(self, job_id: str, source: dict[str, Any], error: str) -> None:
        error = _public_sync_error(error)
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_source(connection, source["id"])
            failures = int(current["failure_count"]) + 1
            # A concurrent edit already set its own next check and must not inherit old state.
            if current["config_revision"] == source["config_revision"]:
                connection.execute(
                    """UPDATE sources SET last_checked_at = ?, next_check_at = ?, last_error = ?,
                    failure_count = ? WHERE id = ?""",
                    (now, _next_check(current, failures=failures), error, failures, source["id"]),
                )
            connection.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                (now, error, job_id),
            )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            # A second ASGI worker may read/write the registry, but only one owns its scheduler.
            # flock automatically releases the lease if the process dies.
            import fcntl

            lease = (self.root / "scheduler.lock").open("a+")
            try:
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lease.close()
                return
            self._scheduler_lock_file = lease
            with self._connect() as connection:
                connection.execute(
                    """UPDATE jobs SET status = 'queued', started_at = NULL,
                    error = NULL WHERE status = 'running'"""
                )
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._scheduler_loop, name="skills-registry-scheduler", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._wake.set()
            if self._thread is not None:
                self._thread.join(timeout=max(10, self.git_timeout_seconds + 5))
                if self._thread.is_alive():
                    raise RuntimeError("Skills scheduler did not stop before timeout")
                self._thread = None
            if self._scheduler_lock_file is not None:
                self._scheduler_lock_file.close()
                self._scheduler_lock_file = None

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                now = _now()
                for source in self.list_sources():
                    if (
                        source["enabled"]
                        and not source["archived"]
                        and source["interval_minutes"] > 0
                        and (source["next_check_at"] is None or source["next_check_at"] <= now)
                    ):
                        try:
                            self.queue_sync(source["id"])
                        except (KeyError, ValueError):
                            # Source may have been archived after the list snapshot.
                            continue
                with self._connect() as connection:
                    queued = connection.execute(
                        "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
                    ).fetchone()
                if queued is not None and not self._stop.is_set():
                    self._run_job(queued["id"])
                    continue
            except Exception:
                # Persisted jobs survive transient storage errors. Avoid a hot retry loop.
                import logging

                logging.getLogger(__name__).exception("Skills registry scheduler iteration failed")
            self._wake.wait(timeout=5)
