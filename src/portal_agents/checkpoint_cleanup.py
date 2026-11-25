"""Roll back an explicitly restricted turn in the portal's SQLite checkpointer."""
from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@dataclass(frozen=True)
class CheckpointBoundary:
    checkpoint_row: int
    write_row: int
    parent_writes: list

    @classmethod
    async def capture(
        cls, saver: AsyncSqliteSaver, thread: str, parent: str | None,
    ) -> CheckpointBoundary:
        if parent is None:
            return cls(0, 0, [])
        async with saver.lock:
            [(checkpoint_row, write_row)] = await saver.conn.execute_fetchall(
                """SELECT (SELECT COALESCE(MAX(rowid),0) FROM checkpoints WHERE thread_id=?),
                          (SELECT COALESCE(MAX(rowid),0) FROM writes WHERE thread_id=?)""",
                (thread, thread),
            )
            # A continuation may attach a pending write to its published parent. The SDK
            # can replace these rows, so retain the pre-turn values for that checkpoint.
            parent_writes = list(await saver.conn.execute_fetchall(
                """SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, task_path,
                          idx, channel, type, value FROM writes
                   WHERE thread_id=? AND checkpoint_ns='' AND checkpoint_id=?""", (thread, parent),
            ))
        return cls(checkpoint_row, write_row, parent_writes)

    async def rollback(self, saver: AsyncSqliteSaver, thread: str) -> None:
        # The caller still holds the per-chat lock. Other chats may share this DB.
        async with saver.lock:
            try:
                await saver.conn.execute("DELETE FROM writes WHERE thread_id=? AND rowid>?",
                                         (thread, self.write_row))
                await saver.conn.execute("DELETE FROM checkpoints WHERE thread_id=? AND rowid>?",
                                         (thread, self.checkpoint_row))
                if self.parent_writes:
                    await saver.conn.executemany(
                        """INSERT OR IGNORE INTO writes(thread_id,checkpoint_ns,checkpoint_id,
                           task_id,task_path,idx,channel,type,value) VALUES(?,?,?,?,?,?,?,?,?)""",
                        self.parent_writes,
                    )
                await saver.conn.commit()
            except BaseException:
                await saver.conn.rollback()
                raise
