"""Private persisted runs with explicit restart semantics and bounded retention."""

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
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, owner TEXT NOT NULL, "
                "status TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner, created)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, owner: str, run: dict[str, Any]) -> None:
        run["updated_at"] = timestamp()
        with self._connect() as db:
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

    def list(self, owner: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT data FROM runs WHERE owner=? ORDER BY created DESC LIMIT ?",
                (owner, self.settings.max_runs_per_owner),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

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
