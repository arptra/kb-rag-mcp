"""Shared experience outlives private chats and keeps cross-user usage history."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.memory import MemoryInput, MemoryStore
from agent_harness.store import RunStore, timestamp


def memory_store(tmp_path: Path, **settings: Any) -> MemoryStore:
    return MemoryStore(RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None, **settings)))


def save_run(
    memory: MemoryStore, run_id: str, owner: str = "alice", **extra: Any
) -> dict[str, Any]:
    run = {
        "id": run_id,
        "status": "completed",
        "created_at": timestamp(),
        "question": "Why does the ledger queue stall?",
        "answer": "Check the ledger consumer and current queue limits.",
        "index_ids": ["ledger"],
        "warnings": [],
        **extra,
    }
    memory.runs.save(owner, run)
    return run


def save_case(memory: MemoryStore, run: dict[str, Any]) -> dict[str, Any]:
    item = memory.record_outcome("alice", run, index_ids=["ledger"])
    assert item is not None
    return item


def save_note(memory: MemoryStore, run_id: str, *, confirmed: bool) -> dict[str, Any]:
    return memory.save(
        "alice",
        MemoryInput(
            title="Ledger consumer",
            text="Check the consumer before changing queue limits.",
            index_ids=["ledger"],
            source_run_id=run_id,
            confirmed=confirmed,
        ),
    )


def memory_row(memory: MemoryStore, memory_id: str) -> tuple[str, str | None, dict[str, Any]]:
    with memory.runs._connect() as db:
        row = db.execute(
            "SELECT owner,source_run_id,data FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
    assert row is not None
    return row[0], row[1], json.loads(row[2])


def add_usage(memory: MemoryStore, memory_id: str, run_id: str, owner: str) -> None:
    # Seed the persisted format directly: RunStore must support cross-user records
    # already present in a database, independently of MemoryStore's retrieval policy.
    with memory.runs._connect() as db:
        db.execute("INSERT INTO memory_usages VALUES (?,?,?)", (memory_id, run_id, owner))
        row = db.execute("SELECT data FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is not None:
            item = json.loads(row[0])
            item["usage_count"] = item.get("usage_count", 0) + 1
            db.execute("UPDATE memories SET data=? WHERE id=?", (json.dumps(item), memory_id))


def usages(memory: MemoryStore) -> set[tuple[str, str, str]]:
    with memory.runs._connect() as db:
        return set(db.execute("SELECT memory_id,run_id,owner FROM memory_usages"))


def test_delete_chat_detaches_shared_descendant_memory_and_preserves_other_users(
    tmp_path: Path,
) -> None:
    memory = memory_store(tmp_path)
    parent = save_run(memory, "parent")
    child = save_run(memory, "child", parent_run_id="parent")
    grandchild = save_run(memory, "grandchild", parent_run_id="child")
    save_run(memory, "bob-consumer", owner="bob")
    case = save_case(memory, parent)
    descendant_case = save_case(memory, grandchild)
    confirmed = save_note(memory, child["id"], confirmed=True)
    draft = save_note(memory, child["id"], confirmed=False)
    add_usage(memory, case["id"], "child", "alice")
    add_usage(memory, case["id"], "bob-consumer", "bob")
    original = memory_row(memory, case["id"])[2]

    assert set(memory.runs.delete("alice", "parent")) == {"parent", "child", "grandchild"}
    for item in (case, descendant_case, confirmed):
        owner, source_id, saved = memory_row(memory, item["id"])
        assert owner == "alice"
        assert source_id is None and saved["source_run_id"] is None
    assert memory_row(memory, case["id"])[2] == {**original, "source_run_id": None}
    with memory.runs._connect() as db:
        assert db.execute("SELECT 1 FROM memories WHERE id=?", (draft["id"],)).fetchone() is None
    assert usages(memory) == {(case["id"], "bob-consumer", "bob")}
    assert memory.runs.get("bob", "bob-consumer")["id"] == "bob-consumer"


def test_deleted_chat_cannot_be_resurrected_or_reattach_its_shared_case(tmp_path: Path) -> None:
    memory = memory_store(tmp_path)
    run = save_run(memory, "deleted")
    case = save_case(memory, run)
    memory.runs.delete("alice", run["id"])
    preserved = memory_row(memory, case["id"])

    memory.runs.save("alice", run)
    assert memory.record_outcome("alice", run, index_ids=["ledger"]) is None
    assert memory_row(memory, case["id"]) == preserved
    with pytest.raises(KeyError):
        memory.runs.get("alice", run["id"])
    assert memory_store(tmp_path).runs.run_ids() == set()


def test_prune_detaches_expired_shared_memory_and_drops_only_private_drafts(
    tmp_path: Path,
) -> None:
    memory = memory_store(tmp_path)
    expired = save_run(memory, "expired", created_at="2000-01-01T00:00:00+00:00")
    save_run(memory, "current", owner="bob")
    case = save_case(memory, expired)
    confirmed = save_note(memory, "expired", confirmed=True)
    draft = save_note(memory, "expired", confirmed=False)
    add_usage(memory, case["id"], "current", "bob")

    memory.runs.prune()
    restarted = memory_store(tmp_path)
    restarted.runs.recover()
    assert restarted.runs.run_ids() == {"current"}
    for item in (case, confirmed):
        author, source_id, saved = memory_row(restarted, item["id"])
        assert author == "alice"
        assert source_id is None and saved["source_run_id"] is None
    assert memory_row(restarted, case["id"])[2]["usage_count"] == 1
    assert usages(restarted) == {(case["id"], "current", "bob")}
    with restarted.runs._connect() as db:
        assert db.execute("SELECT 1 FROM memories WHERE id=?", (draft["id"],)).fetchone() is None


@pytest.mark.parametrize("limit", ["max_runs_per_owner", "max_total_runs"])
def test_chat_count_limits_preserve_shared_knowledge(tmp_path: Path, limit: str) -> None:
    memory = memory_store(tmp_path, **{limit: 1})
    oldest = save_run(memory, "oldest")
    case = save_case(memory, oldest)
    save_run(memory, "latest")

    memory.runs.prune()

    assert memory.runs.run_ids() == {"latest"}
    assert memory_row(memory, case["id"])[1] is None
    assert memory_row(memory, case["id"])[2]["source_run_id"] is None


def test_usage_cleanup_matches_consumer_ownership_and_preserves_historical_count(
    tmp_path: Path,
) -> None:
    memory = memory_store(tmp_path)
    source = save_run(memory, "source")
    save_run(memory, "valid", owner="bob")
    save_run(memory, "wrong-owner", owner="bob")
    case = save_case(memory, source)
    add_usage(memory, case["id"], "valid", "bob")
    add_usage(memory, case["id"], "wrong-owner", "alice")
    add_usage(memory, case["id"], "missing-run", "bob")
    add_usage(memory, "missing-memory", "valid", "bob")

    memory.runs.prune()

    assert usages(memory) == {(case["id"], "valid", "bob")}
    assert memory_row(memory, case["id"])[2]["usage_count"] == 3
    memory.runs.delete("bob", "valid")
    assert usages(memory) == set()
    assert memory_row(memory, case["id"])[2]["usage_count"] == 3
    assert memory_row(memory, case["id"])[1] == source["id"]


def test_run_store_can_delete_and_prune_without_optional_memory_tables(tmp_path: Path) -> None:
    runs = RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None))
    runs.save("alice", {"id": "standalone", "status": "completed", "created_at": timestamp()})
    assert runs.delete("alice", "standalone") == ["standalone"]
    runs.prune()
