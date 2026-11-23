"""Automatically collected experience remains private, inspectable and advisory."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.memory import MemoryInput, MemoryStore
from agent_harness.store import RunStore, timestamp


def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None)))


def run(memory: MemoryStore, run_id: str = "first", owner: str = "alice", **extra: Any) -> dict:
    data = {
        "id": run_id,
        "status": "completed",
        "created_at": timestamp(),
        "question": "Why do ledger payments time out?",
        "answer": "The current payment queue configuration needs review [doc].",
        "index_ids": ["payments"],
        "context_index_ids": [],
        "mode": "investigate",
        "effort": "high",
        "metrics": {"source_calls": 4, "tool_calls": 3, "model_calls": 2},
        "evidence": [{"id": "doc", "title": "Ledger retries", "index_id": "payments"}],
        "warnings": [],
        "research_trace": [{"name": "search_knowledge", "args": {"token": "hidden-secret"}}],
        "coverage": {"payments": {"status": "searched", "count": 1}},
        **extra,
    }
    memory.runs.save(owner, data)
    return data


def test_completed_run_is_remembered_after_restart_and_reused_only_as_experience(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    assert item is not None
    assert (item["status"], item["origin"], item["kind"]) == ("experience", "automatic", "case")
    assert item["strategy"]["tools"] == ["search_knowledge"]
    assert item["sources"][0]["id"] == "doc"
    assert "hidden-secret" not in json.dumps(item)
    assert not any(key.startswith("_") for key in item)
    memory = store(tmp_path)
    found = memory.retrieve("alice", "ledger payments", {"payments"}, {"payments"})
    assert len(found) == 1 and found[0]["id"] == item["id"]
    assert found[0]["status"] == "experience"
    assert found[0]["text"].startswith("НЕПОДТВЕРЖДЁННЫЙ ОПЫТ")
    assert (
        memory.retrieve(
            "alice", "ledger payments", {"payments"}, {"payments"}, exclude_run_id=first["id"]
        )
        == []
    )
    assert memory.retrieve("alice", "unrelated inventory", {"payments"}, {"payments"}) == []
    memory.record_outcome("alice", first, index_ids=["payments"])
    assert len(memory.list("alice", {"payments"})) == 1


def test_automatic_case_keeps_full_scope_and_other_owners_cannot_see_it(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, context_index_ids=["private-logs"])
    memory.record_outcome("alice", first, index_ids=["payments"])
    assert memory.list("alice", {"payments"}) == []
    assert memory.list("bob", {"payments", "private-logs"}) == []
    assert memory.retrieve("alice", "ledger", {"payments"}, {"payments"}) == []
    assert memory.retrieve("alice", "ledger", {"payments", "private-logs"}, {"payments"}) == []
    assert (
        len(
            memory.retrieve(
                "alice", "ledger", {"payments", "private-logs"}, {"payments", "private-logs"}
            )
        )
        == 1
    )


def test_feedback_updates_case_without_promoting_it_to_confirmed_knowledge(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory)
    positive = memory.record_feedback("alice", first, rating="helpful", index_ids=["payments"])
    assert positive is not None and positive["status"] == "experience"
    assert positive["feedback"]["rating"] == "helpful"
    negative = memory.record_feedback(
        "alice",
        first,
        rating="unhelpful",
        comment="Check the ledger worker instead",
        index_ids=["payments"],
    )
    assert negative is not None and negative["id"] == positive["id"]
    assert negative["kind"] == "lesson"
    assert first["answer"] not in negative["text"]
    found = memory.retrieve("alice", "ledger payments", {"payments"}, {"payments"})
    assert len(found) == 1
    assert "Check the ledger worker instead" in found[0]["text"]
    assert "Повторять этот ответ нельзя" in found[0]["text"]
    assert found[0]["feedback"]["rating"] == "unhelpful"


def test_failure_records_operation_and_scope_without_raw_exception_or_args(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, status="running", answer="", evidence=[], warnings=["hidden-secret"])
    memory.record_failure("alice", first, operation="search", index_ids=["payments"])
    memory.record_failure("alice", first, operation="read", index_ids=["payments"])
    memory.record_failure("alice", first, operation="read", index_ids=["payments"])
    first.update(status="failed")
    memory.runs.save("alice", first)
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    assert item is not None and item["kind"] == "lesson"
    assert item["outcome"] == "failed"
    assert item["failures"] == [
        {"operation": "search", "index_ids": ["payments"]},
        {"operation": "read", "index_ids": ["payments"]},
    ]
    assert "hidden-secret" not in json.dumps(item)
    assert "другим запросом" in item["text"]
    assert "идентификатор" in item["text"]
    assert memory.retrieve("alice", "ledger payments", {"payments"}, {"payments"})


def test_usage_is_counted_once_per_run_even_when_parallel_runs_interleave(tmp_path: Path) -> None:
    memory = store(tmp_path)
    item = memory.record_outcome("alice", run(memory), index_ids=["payments"])
    assert item is not None
    for consumer in ("second", "third"):
        run(memory, consumer)
    for consumer in ("second", "third", "second"):
        memory.record_usage("alice", [item["id"]], consumer)
    memory.record_usage("bob", [item["id"]], "second")
    memory.record_usage("alice", [item["id"]], "missing-run")
    updated = memory.list("alice", {"payments"})[0]
    assert updated["usage_count"] == 2
    assert updated["last_used_at"]
    assert store(tmp_path).list("alice", {"payments"})[0]["usage_count"] == 2


def test_delete_conversation_cascades_automatic_cases_and_usage_and_no_resurrection(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    second = run(memory, "second", parent_run_id=first["id"])
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    memory.record_outcome("alice", second, index_ids=["payments"])
    assert item is not None
    memory.record_usage("alice", [item["id"]], second["id"])
    assert set(memory.runs.delete("alice", first["id"])) == {"first", "second"}
    assert memory.list("alice", {"payments"}) == []
    assert memory.record_outcome("alice", first, index_ids=["payments"]) is None
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_usages").fetchone()[0] == 0


def test_disabled_memory_does_not_collect_or_retrieve_and_feedback_can_correct_old_case(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    assert item is not None
    memory.set_enabled("alice", False)
    assert memory.record_outcome("alice", run(memory, "second"), index_ids=["payments"]) is None
    assert memory.retrieve("alice", "ledger", {"payments"}, {"payments"}) == []
    memory.record_feedback("alice", first, rating="unhelpful", index_ids=["payments"])
    assert len(memory.list("alice", {"payments"})) == 1
    memory.set_enabled("alice", True)
    assert memory.retrieve("alice", "ledger", {"payments"}, {"payments"})[0]["kind"] == "lesson"


def test_automatic_retention_rotates_only_experience_and_preserves_manual_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_harness.memory as module

    monkeypatch.setattr(module, "MAX_PER_OWNER", 2)
    memory = store(tmp_path)
    note = memory.save(
        "alice",
        MemoryInput(
            title="Important",
            text="Keep this instruction",
            index_ids=["payments"],
            confirmed=True,
        ),
    )
    old = memory.record_outcome("alice", run(memory), index_ids=["payments"])
    new = memory.record_outcome("alice", run(memory, "second"), index_ids=["payments"])
    assert old is not None and new is not None
    assert {item["id"] for item in memory.list("alice", {"payments"})} == {note["id"], new["id"]}


def test_pruning_conversations_removes_experience_and_usage(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, created_at="2000-01-01T00:00:00+00:00")
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    second = run(memory, "second")
    assert item is not None
    memory.record_usage("alice", [item["id"]], second["id"])
    memory.runs.prune()
    assert memory.list("alice", {"payments"}) == []
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_usages").fetchone()[0] == 0


def test_negative_feedback_remains_retrievable_among_newer_unreviewed_answers(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    failed = memory.record_feedback(
        "alice",
        first,
        rating="unhelpful",
        comment="Inspect the queue worker",
        index_ids=["payments"],
    )
    for index in range(5):
        memory.record_outcome("alice", run(memory, f"repeat-{index}"), index_ids=["payments"])
    assert failed is not None
    found = memory.retrieve("alice", "ledger payments", {"payments"}, {"payments"})
    assert failed["id"] in {item["id"] for item in found}
