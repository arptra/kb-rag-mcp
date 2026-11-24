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
            self._migrate_conversations(db)

    @staticmethod
    def _migrate_conversations(db: sqlite3.Connection, owner: str | None = None) -> None:
        """Assign legacy chains using only parents belonging to the same owner."""
        rows = db.execute(
            "SELECT owner,id,data FROM runs" + (" WHERE owner=?" if owner is not None else ""),
            (owner,) if owner is not None else (),
        ).fetchall()
        grouped: dict[str, dict[str, dict[str, Any]]] = {}
        for author, key, data in rows:
            grouped.setdefault(author, {})[key] = json.loads(data)
        for author, runs in grouped.items():
            resolved: dict[str, str] = {}
            for key in runs:
                trail: list[str] = []
                current = key
                while current not in resolved:
                    item = runs[current]
                    stored = item.get("conversation_id")
                    if isinstance(stored, str) and stored:
                        conversation_id = stored
                        break
                    if current in trail:
                        conversation_id = min(trail[trail.index(current) :])
                        break
                    trail.append(current)
                    parent = item.get("parent_run_id")
                    if parent not in runs:
                        conversation_id = current
                        break
                    current = parent
                else:
                    conversation_id = resolved[current]
                for member in trail:
                    resolved[member] = conversation_id
                resolved[key] = conversation_id
            for key, conversation_id in resolved.items():
                if runs[key].get("conversation_id") != conversation_id:
                    runs[key]["conversation_id"] = conversation_id
                    db.execute(
                        "UPDATE runs SET data=? WHERE id=? AND owner=?",
                        (json.dumps(runs[key], ensure_ascii=False), key, author),
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
        self._save(owner, run, checkpoint=False)

    def save_checkpoint(self, owner: str, run: dict[str, Any]) -> None:
        """Execution callbacks may only update the still-active same-generation run."""
        self._save(owner, run, checkpoint=True)

    def _save(self, owner: str, run: dict[str, Any], *, checkpoint: bool) -> None:
        run["updated_at"] = timestamp()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM deleted_runs WHERE id=?", (run["id"],)).fetchone():
                return
            previous = db.execute(
                "SELECT owner,status,data FROM runs WHERE id=?", (run["id"],)
            ).fetchone()
            if checkpoint and previous is None:
                return
            if previous is not None:
                if previous[0] != owner:
                    return
                stored = json.loads(previous[2])
                generation = int(run.get("resume_count", 0))
                previous_generation = int(stored.get("resume_count", 0))
                if checkpoint and (
                    previous[1] not in ACTIVE or generation != previous_generation
                ):
                    return
                if generation < previous_generation or (
                    previous[1] not in ACTIVE
                    and run["status"] in ACTIVE
                    and generation <= previous_generation
                ):
                    return
                run.setdefault("conversation_id", stored.get("conversation_id") or run["id"])
            if not run.get("conversation_id"):
                parent = db.execute(
                    "SELECT data FROM runs WHERE id=? AND owner=?",
                    (run.get("parent_run_id"), owner),
                ).fetchone()
                parent_data = json.loads(parent[0]) if parent else {}
                run["conversation_id"] = parent_data.get("conversation_id") or run["id"]
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

    def conversations(self, owner: str) -> builtins.list[dict[str, Any]]:
        """Return every retained owner run; callers explicitly paginate conversation views."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT data FROM runs WHERE owner=? ORDER BY created,id", (owner,)
            ).fetchall()
            runs = [json.loads(row[0]) for row in rows]
            # Normal writes and startup already populate the ID. Only rescan for
            # old-format rows, not every time the chat interface polls history.
            if any(not run.get("conversation_id") for run in runs):
                self._migrate_conversations(db, owner)
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM runs WHERE owner=? ORDER BY created,id", (owner,)
                    )
                ]
        return runs

    def delete_conversation(self, owner: str, conversation_id: str) -> builtins.list[str]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._migrate_conversations(db, owner)
            runs = {
                key: json.loads(data)
                for key, data in db.execute("SELECT id,data FROM runs WHERE owner=?", (owner,))
            }
            deleted = {
                key for key, run in runs.items() if run.get("conversation_id") == conversation_id
            }
            if not deleted:
                raise KeyError("Conversation not found")
            self._delete_selected(db, owner, runs, deleted)
        return sorted(deleted)

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
            self._delete_selected(db, owner, runs, deleted)
        return sorted(deleted)

    def _delete_selected(
        self, db: sqlite3.Connection, owner: str, runs: dict[str, Any], deleted: set[str]
    ) -> None:
        if any(runs[key]["status"] in ACTIVE for key in deleted):
            raise ValueError("Сначала остановите все активные запросы этого чата.")
        for key in deleted:
            db.execute(
                "INSERT OR IGNORE INTO deleted_runs VALUES (?,?,?)", (key, owner, timestamp())
            )
            db.execute("DELETE FROM runs WHERE id=? AND owner=?", (key, owner))
        self._cleanup_memory_references(db)

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
        for table in ("memory_usages", "memory_outcomes"):
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            if not has_memories:
                db.execute(f"DELETE FROM {table}")
            else:
                # A memory's author and the consuming run's owner can differ. Keep
                # historical usage/quality counters when individual run links expire.
                db.execute(
                    f"DELETE FROM {table} WHERE NOT EXISTS "
                    f"(SELECT 1 FROM memories WHERE memories.id={table}.memory_id) "
                    f"OR NOT EXISTS (SELECT 1 FROM runs WHERE runs.id={table}.run_id "
                    f"AND runs.owner={table}.owner)"
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
