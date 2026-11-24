"""Reuse outcomes improve strategy selection without treating answers as proven facts."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.memory import MemoryInput, MemoryStore
from agent_harness.store import RunStore, timestamp


def memory_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None)))


def note(memory: MemoryStore, title: str = "Ledger queue") -> dict[str, Any]:
    return memory.save(
        "alice",
        MemoryInput(
            title=title,
            text="Inspect the documented worker configuration.",
            index_ids=["payments"],
            confirmed=True,
        ),
    )


def consumer(memory: MemoryStore, run_id: str = "consumer", **extra: Any) -> dict[str, Any]:
    run = {
        "id": run_id,
        "status": "completed",
        "created_at": timestamp(),
        "question": "Investigate ledger queue",
        "answer": "Review the worker.",
        "index_ids": ["payments"],
        "context_index_ids": [],
        "evidence": [],
        "metrics": {},
        "warnings": [],
        **extra,
    }
    memory.runs.save("bob", run)
    return run


def quality(memory: MemoryStore, memory_id: str) -> dict[str, int]:
    return memory.public(memory.get("alice", memory_id))["quality"]


def test_reuse_outcomes_and_changed_feedback_are_idempotent_and_survive_restart(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(memory)
    memory.record_usage("bob", [item["id"]], run["id"])
    for _ in range(2):
        memory.record_outcome("bob", run, index_ids=["payments"])
    assert quality(memory, item["id"]) == {
        "uses": 1,
        "completed": 1,
        "incomplete": 0,
        "helpful": 0,
        "unhelpful": 0,
    }
    assert memory.public(memory.get("alice", item["id"]))["learning_state"] == "observed"
    for _ in range(2):
        memory.record_feedback("bob", run, rating="helpful", index_ids=["payments"])
    # Re-saving an older outcome does not erase explicit feedback.
    memory.record_outcome("bob", run, index_ids=["payments"])
    assert quality(memory, item["id"])["helpful"] == 1
    memory = memory_store(tmp_path)
    memory.record_feedback("bob", run, rating="unhelpful", index_ids=["payments"])
    assert quality(memory, item["id"]) == {
        "uses": 1,
        "completed": 1,
        "incomplete": 0,
        "helpful": 0,
        "unhelpful": 1,
    }
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_outcomes").fetchone()[0] == 1


def test_resumed_incomplete_research_updates_one_observation_instead_of_double_counting(
    tmp_path: Path,
):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(memory, status="timed_out")
    memory.record_usage("bob", [item["id"]], run["id"])
    memory.record_outcome("bob", run, index_ids=["payments"])
    assert quality(memory, item["id"])["incomplete"] == 1
    run["status"] = "completed"
    memory.runs.save("bob", run)
    memory.record_outcome("bob", run, index_ids=["payments"])
    assert quality(memory, item["id"])["incomplete"] == 0
    assert quality(memory, item["id"])["completed"] == 1


def test_helpful_reuse_breaks_relevance_ties_but_cannot_override_a_better_match(tmp_path: Path):
    memory = memory_store(tmp_path)
    learned = note(memory, "Ledger")
    newer = note(memory, "Ledger")
    more_relevant = note(memory, "Ledger queue backlog")
    run = consumer(memory)
    memory.record_usage("bob", [learned["id"]], run["id"])
    memory.record_feedback("bob", run, rating="helpful", index_ids=["payments"])
    found = memory.retrieve("carol", "ledger", {"payments"}, {"payments"})
    assert found[0]["id"] == learned["id"]
    assert found[0]["quality"]["helpful"] == 1
    assert found[0]["learning_state"] == "helpful"
    assert next(index for index, row in enumerate(found) if row["id"] == learned["id"]) < next(
        index for index, row in enumerate(found) if row["id"] == newer["id"]
    )
    assert (
        memory.retrieve("carol", "ledger queue backlog", {"payments"}, {"payments"})[0]["id"]
        == more_relevant["id"]
    )


def test_negative_consumer_rating_is_a_caution_without_leaking_private_context(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(
        memory,
        question="SECRET-QUESTION",
        answer="SECRET-ANSWER",
        context_index_ids=["private-logs"],
        evidence=[{"id": "SECRET-DOCUMENT", "index_id": "private-logs", "title": "Secret"}],
    )
    memory.record_usage("bob", [item["id"]], run["id"])
    memory.record_feedback(
        "bob",
        run,
        rating="unhelpful",
        comment="SECRET-COMMENT",
        index_ids=["payments", "private-logs"],
    )
    shared = memory.public(memory.get("alice", item["id"]))
    for key in ("title", "text", "kind", "status", "index_ids"):
        assert shared[key] == item[key]
    assert "SECRET" not in json.dumps(shared) and "bob" not in json.dumps(shared)
    assert shared["learning_state"] == "needs_review"
    visible = memory.retrieve("carol", "ledger", {"payments"}, {"payments"})
    assert len(visible) == 1 and visible[0]["id"] == item["id"]
    assert "не доказывает ошибку" in visible[0]["text"]
    assert "Повторять этот ответ нельзя" not in visible[0]["text"]
    assert "SECRET" not in json.dumps(visible)
    own_case = memory.list("bob", {"payments", "private-logs"})[0]
    assert "SECRET-COMMENT" in own_case["text"]
    assert set(own_case["index_ids"]) == {"payments", "private-logs"}


def test_unproven_or_foreign_usage_cannot_credit_or_discredit_shared_knowledge(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(memory, memory_used=[{"id": item["id"]}])
    memory.record_feedback("bob", run, rating="unhelpful", index_ids=["payments"])
    assert quality(memory, item["id"])["unhelpful"] == 0
    memory.record_usage("alice", [item["id"]], run["id"])
    memory.record_outcome("alice", run, index_ids=["payments"])
    assert quality(memory, item["id"])["uses"] == 0
    assert quality(memory, item["id"])["completed"] == 0


def test_personal_opt_out_stops_automatic_learning_without_erasing_past_evidence(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(memory)
    memory.record_usage("bob", [item["id"]], run["id"])
    memory.set_enabled("bob", False)
    assert memory.record_outcome("bob", run, index_ids=["payments"]) is None
    assert quality(memory, item["id"])["uses"] == 1
    assert quality(memory, item["id"])["completed"] == 0


def test_chat_deletion_keeps_learned_quality_and_new_runs_add_to_it(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    first = consumer(memory)
    memory.record_usage("bob", [item["id"]], first["id"])
    memory.record_feedback("bob", first, rating="helpful", index_ids=["payments"])
    memory.runs.delete("bob", first["id"])
    assert quality(memory, item["id"])["helpful"] == 1
    second = consumer(memory, "second")
    memory.record_usage("bob", [item["id"]], second["id"])
    memory.record_outcome("bob", second, index_ids=["payments"])
    assert quality(memory, item["id"]) == {
        "uses": 2,
        "completed": 2,
        "incomplete": 0,
        "helpful": 1,
        "unhelpful": 0,
    }
    memory.delete("alice", item["id"])
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_outcomes").fetchone()[0] == 0


def test_automatic_case_remembers_plan_and_safe_tool_strategy(tmp_path: Path):
    memory = memory_store(tmp_path)
    plan = {
        "goal": "Locate queue delay",
        "questions": ["Worker timeout?", "Broker load?"],
        "completed": ["Read worker config"],
        "gaps": ["Need current broker metrics"],
        "next_action": "Read broker telemetry",
        "round": 3,
        "raw_prompt": "SECRET-INTERNAL",
        "owner": "SECRET-OWNER",
    }
    run = consumer(
        memory,
        research_plan=plan,
        research_trace=[
            {"name": "read_document", "args": {"token": "SECRET-TOKEN"}, "debug": "SECRET-DEBUG"}
        ],
    )
    item = memory.record_outcome("bob", run, index_ids=["payments"])
    assert item is not None
    assert item["research_plan"] == {
        key: plan[key]
        for key in (
            "goal",
            "questions",
            "completed",
            "gaps",
            "next_action",
            "round",
        )
    }
    assert item["strategy"]["tools"] == ["read_document"]
    assert "SECRET" not in json.dumps(item)
    learned = memory.retrieve("carol", "ledger", {"payments"}, {"payments"})[0]
    assert learned["research_plan"]["gaps"] == ["Need current broker metrics"]
    assert learned["strategy"]["tools"] == ["read_document"]
    assert "НЕПОДТВЕРЖДЁННЫЙ" in learned["text"]


def test_quality_stays_when_author_confirms_or_edits_a_record(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    run = consumer(memory)
    memory.record_usage("bob", [item["id"]], run["id"])
    memory.record_feedback("bob", run, rating="helpful", index_ids=["payments"])
    updated = memory.save(
        "alice",
        MemoryInput(
            title="Ledger refined",
            text="Inspect updated settings",
            index_ids=["payments"],
            confirmed=True,
        ),
        memory_id=item["id"],
    )
    assert updated["quality"]["helpful"] == 1


def test_eviction_removes_assessment_links_for_evicted_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import agent_harness.memory as module

    memory = memory_store(tmp_path)
    source = consumer(memory, "source")
    old = memory.record_outcome("bob", source, index_ids=["payments"])
    assert old is not None
    second = consumer(memory, "second")
    memory.record_usage("bob", [old["id"]], second["id"])
    monkeypatch.setattr(module, "MAX_TOTAL", 1)
    memory.record_feedback("bob", second, rating="helpful", index_ids=["payments"])
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM memory_outcomes").fetchone()[0] == 0


@pytest.mark.parametrize("terminal", ["completed", "partial", "timed_out", "cancelled"])
def test_late_failure_cannot_replace_terminal_case_or_feedback(tmp_path: Path, terminal: str):
    memory = memory_store(tmp_path)
    run = consumer(memory, status="running", resume_count=0)
    stale = {**run}
    memory.record_failure("bob", run, operation="search", index_ids=["payments"])
    run["status"] = terminal
    memory.runs.save("bob", run)
    memory.record_feedback(
        "bob", run, rating="helpful", comment="Keep this assessment", index_ids=["payments"],
    )
    before = memory.list("bob", {"payments"})
    # Test both the detached active snapshot and its shared dict that has already
    # acquired terminal state; neither may overwrite the user's assessment.
    for late in (stale, run):
        memory.record_failure("bob", late, operation="read", index_ids=["payments"])
    assert memory.list("bob", {"payments"}) == before


@pytest.mark.parametrize("stored_status", ["cancelling", "running"])
def test_cancelled_or_old_resume_failure_cannot_write_experience(
    tmp_path: Path, stored_status: str,
):
    memory = memory_store(tmp_path)
    stale = consumer(memory, status="running", resume_count=0)
    generation = 1 if stored_status == "running" else 0
    current = {**stale, "status": stored_status, "resume_count": generation}
    memory.runs.save("bob", current)
    memory.record_failure("bob", stale, operation="search", index_ids=["payments"])
    assert memory.list("bob", {"payments"}) == []
    if stored_status == "running":
        memory.record_failure("bob", current, operation="read", index_ids=["payments"])
        assert memory.list("bob", {"payments"})[0]["failures"] == [
            {"operation": "read", "index_ids": ["payments"]},
        ]


def test_old_generation_outcome_cannot_reverse_new_resume_learning(tmp_path: Path):
    memory = memory_store(tmp_path)
    item = note(memory)
    stale = consumer(memory, status="timed_out", resume_count=0)
    memory.record_usage("bob", [item["id"]], stale["id"])
    memory.record_outcome("bob", stale, index_ids=["payments"])
    current = {**stale, "status": "completed", "resume_count": 1}
    memory.runs.save("bob", current)
    memory.record_outcome("bob", current, index_ids=["payments"])
    assert quality(memory, item["id"])["completed"] == 1
    assert memory.record_outcome("bob", stale, index_ids=["payments"]) is None
    assert quality(memory, item["id"])["completed"] == 1
    assert quality(memory, item["id"])["incomplete"] == 0
    automatic = [row for row in memory.list("bob", {"payments"}) if row["origin"] == "automatic"]
    assert len(automatic) == 1 and automatic[0]["outcome"] == "completed"


def test_deleted_run_cannot_be_recreated_in_memory_by_late_failure(tmp_path: Path):
    memory = memory_store(tmp_path)
    stale = consumer(memory, status="running")
    memory.runs.save("bob", {**stale, "status": "cancelled"})
    memory.runs.delete("bob", stale["id"])
    memory.record_failure("bob", stale, operation="search", index_ids=["payments"])
    assert memory.list("bob", {"payments"}) == []
