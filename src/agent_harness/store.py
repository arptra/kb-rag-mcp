"""Private persisted runs with explicit restart semantics and bounded retention."""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_harness.config import HarnessSettings

ACTIVE = {"queued", "running", "cancelling"}


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


class RunStore:
    def __init__(self, settings: HarnessSettings) -> None:
        self.settings = settings
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.state_dir / "runs.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "status TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner, created)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS deleted_runs "
                "(id TEXT PRIMARY KEY, owner TEXT NOT NULL, deleted TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, owner: str, run: dict[str, Any]) -> None:
        run["updated_at"] = timestamp()
        with self._connect() as db:
            if db.execute("SELECT 1 FROM deleted_runs WHERE id=?", (run["id"],)).fetchone():
                return
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "status=excluded.status, data=excluded.data WHERE runs.owner=excluded.owner",
                (
                    run["id"],
                    owner,
                    run["status"],
                    run["created_at"],
                    json.dumps(run, ensure_ascii=False),
                ),
            )

    def get(self, owner: str, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM runs WHERE id=? AND owner=?", (run_id, owner)
            ).fetchone()
        if row is None:
            raise KeyError("Run not found")
        return dict(json.loads(row[0]))

    def list(self, owner: str) -> builtins.list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT data FROM runs WHERE owner=? ORDER BY created DESC LIMIT ?",
                (owner, self.settings.max_runs_per_owner),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def run_ids(self) -> set[str]:
        with self._connect() as db:
            return {str(row[0]) for row in db.execute("SELECT id FROM runs")}

    def delete(self, owner: str, run_id: str) -> builtins.list[str]:
        """Delete a conversation and descendants atomically; active work cannot disappear."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,data FROM runs WHERE owner=?", (owner,)).fetchall()
            runs = {str(key): json.loads(data) for key, data in rows}
            if run_id not in runs:
                raise KeyError("Run not found")
            deleted = {run_id}
            while True:
                children = {key for key, run in runs.items() if run.get("parent_run_id") in deleted}
                if children <= deleted:
                    break
                deleted |= children
            if any(runs[key]["status"] in ACTIVE for key in deleted):
                raise ValueError("Сначала остановите все активные запросы этого чата.")
            for key in deleted:
                db.execute(
                    "INSERT OR IGNORE INTO deleted_runs VALUES (?,?,?)", (key, owner, timestamp())
                )
                db.execute("DELETE FROM runs WHERE id=? AND owner=?", (key, owner))
            self._cleanup_memory_references(db)
        return sorted(deleted)

    @staticmethod
    def _cleanup_memory_references(db: sqlite3.Connection) -> None:
        """Retain shared knowledge after chat deletion; usage belongs to its consumer."""
        # These tables are optional for standalone RunStore and older databases.
        has_memories = db.execute("SELECT 1 FROM sqlite_master WHERE name='memories'").fetchone()
        if has_memories:
            orphaned = db.execute(
                "SELECT id,status,data FROM memories WHERE source_run_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM runs WHERE runs.id=memories.source_run_id "
                "AND runs.owner=memories.owner)"
            ).fetchall()
            for memory_id, status, data in orphaned:
                if status in {"active", "experience"}:
                    item = json.loads(data)
                    item["source_run_id"] = None
                    db.execute(
                        "UPDATE memories SET source_run_id=NULL,data=? WHERE id=?",
                        (json.dumps(item, ensure_ascii=False), memory_id),
                    )
                elif status == "draft":
                    db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_usages'").fetchone():
            if not has_memories:
                db.execute("DELETE FROM memory_usages")
            else:
                # A memory's author and the consuming run's owner can differ. Keep
                # historical usage_count even when individual run links expire.
                db.execute(
                    "DELETE FROM memory_usages WHERE NOT EXISTS "
                    "(SELECT 1 FROM memories WHERE memories.id=memory_usages.memory_id) "
                    "OR NOT EXISTS (SELECT 1 FROM runs WHERE runs.id=memory_usages.run_id "
                    "AND runs.owner=memory_usages.owner)"
                )

    def recover(self) -> None:
        with self._connect() as db:
            rows = db.execute(
                "SELECT owner,data FROM runs WHERE status IN ('queued','running','cancelling')"
            )
            interrupted = list(rows)
        for owner, data in interrupted:
            run = json.loads(data)
            run.update(status="interrupted", phase="finished", completed_at=timestamp())
            run["warnings"].append(
                "Сервис перезапущен. Запуск прерван; автоматического resume нет."
            )
            self.save(owner, run)
        self.prune()

    def prune(self) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=self.settings.retention_days)).isoformat()
        with self._connect() as db:
            db.execute(
                "DELETE FROM runs "
                "WHERE status NOT IN ('queued','running','cancelling') AND created<?",
                (cutoff,),
            )
            owners = [row[0] for row in db.execute("SELECT DISTINCT owner FROM runs")]
            for owner in owners:
                db.execute(
                    "DELETE FROM runs WHERE id IN (SELECT id FROM runs WHERE owner=? "
                    "AND status NOT IN ('queued','running','cancelling') ORDER BY created DESC "
                    "LIMIT -1 OFFSET ?)",
                    (owner, self.settings.max_runs_per_owner),
                )
            db.execute(
                "DELETE FROM runs WHERE id IN (SELECT id FROM runs "
                "WHERE status NOT IN ('queued','running','cancelling') ORDER BY created DESC "
                "LIMIT -1 OFFSET ?)",
                (self.settings.max_total_runs,),
            )
            db.execute("DELETE FROM deleted_runs WHERE deleted<?", (cutoff,))
            self._cleanup_memory_references(db)
