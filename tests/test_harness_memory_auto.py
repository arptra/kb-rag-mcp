"""Shared experience is inspectable, advisory and filtered by its complete source scope."""

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


def test_retrieving_legacy_cases_drops_automatic_notice_but_preserves_manual_notes(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    notice = "Иногда генеративные модели могут создавать некорректные ответы."
    case = memory.record_outcome("alice", run(memory, answer=notice), index_ids=["payments"])
    assert case is not None and notice in case["text"]
    finding = notice + " Ledger records a timeout [doc]."
    verified = memory.record_outcome(
        "alice", run(memory, "mixed", answer=finding), index_ids=["payments"],
    )
    assert verified is not None
    manual = memory.save("alice", MemoryInput(
        title="Ledger manual", text="Пример ответа модели:\n" + notice,
        index_ids=["payments"], confirmed=True,
    ))
    selected = {"payments"}
    found = {item["id"]: item for item in memory.retrieve("bob", "ledger", selected, selected)}
    assert notice not in found[case["id"]]["text"]
    assert "search_knowledge" in found[case["id"]]["text"]
    assert notice in found[manual["id"]]["text"]
    assert finding in found[verified["id"]]["text"]
    matches = memory.retrieve("bob", "некорректные", selected, selected)
    assert {item["id"] for item in matches} == {manual["id"], verified["id"]}
    assert notice in memory.get("alice", case["id"])["text"]


def test_automatic_case_is_shared_only_with_readers_who_have_its_full_scope(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, context_index_ids=["private-logs"])
    memory.record_outcome("alice", first, index_ids=["payments"])
    assert memory.list("alice", {"payments"}) == []
    assert len(memory.list("bob", {"payments", "private-logs"})) == 1
    assert memory.list("bob", {"payments"}) == []
    assert memory.retrieve("bob", "ledger", {"payments"}, {"payments"}) == []
    assert memory.retrieve("bob", "ledger", {"payments", "private-logs"}, {"payments"}) == []
    assert (
        len(
            memory.retrieve(
                "bob", "ledger", {"payments", "private-logs"}, {"payments", "private-logs"}
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


def test_delete_conversation_preserves_shared_cases_and_cleans_usage_without_resurrection(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    second = run(memory, "second", parent_run_id=first["id"])
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    second_item = memory.record_outcome("alice", second, index_ids=["payments"])
    assert item is not None
    assert second_item is not None
    memory.record_usage("alice", [item["id"]], second["id"])
    assert set(memory.runs.delete("alice", first["id"])) == {"first", "second"}
    retained = memory.list("alice", {"payments"})
    assert {entry["id"] for entry in retained} == {item["id"], second_item["id"]}
    assert all(entry["source_run_id"] is None for entry in retained)
    assert memory.get("alice", item["id"])["usage_count"] == 1
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

    monkeypatch.setattr(module, "MAX_TOTAL", 2)
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
    run(memory, "consumer", owner="bob")
    assert old is not None
    memory.record_usage("bob", [old["id"]], "consumer")
    new = memory.record_outcome("bob", run(memory, "second", owner="bob"), index_ids=["payments"])
    assert old is not None and new is not None
    assert {item["id"] for item in memory.list("alice", {"payments"})} == {note["id"], new["id"]}
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_usages").fetchone()[0] == 0


def test_pruning_conversations_preserves_experience_and_live_usage(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, created_at="2000-01-01T00:00:00+00:00")
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    second = run(memory, "second")
    assert item is not None
    memory.record_usage("alice", [item["id"]], second["id"])
    memory.runs.prune()
    retained = memory.list("alice", {"payments"})
    assert len(retained) == 1 and retained[0]["id"] == item["id"]
    assert retained[0]["source_run_id"] is None
    assert retained[0]["usage_count"] == 1
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_usages").fetchone()[0] == 1


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


def test_shared_manual_knowledge_and_automatic_experience_keep_author_only_mutations(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    first = run(memory)
    case = memory.record_outcome("alice", first, index_ids=["payments"])
    note_input = MemoryInput(
        title="Ledger guidance", text="Read the worker logs", index_ids=["payments"], confirmed=True
    )
    note = memory.save("alice", note_input)
    draft = memory.save("alice", note_input.model_copy(update={"confirmed": False}))
    assert case is not None
    assert case["visibility"] == note["visibility"] == "shared"
    assert draft["visibility"] == "private"
    assert case["can_edit"] and case["can_delete"]
    visible = [memory.public(item) for item in memory.list("bob", {"payments"})]
    assert {item["id"] for item in visible} == {note["id"], case["id"]}
    for item in visible:
        assert item["visibility"] == "shared"
        assert item["can_edit"] is item["can_delete"] is False
        assert "source_run_id" not in item
        assert not any(key.startswith("_") for key in item)
        assert "alice" not in json.dumps(item)
    retrieved = memory.retrieve("bob", "ledger", {"payments"}, {"payments"})
    assert {item["id"] for item in retrieved} == {case["id"], note["id"]}
    assert "alice" not in json.dumps(retrieved)
    for item in (case, note, draft):
        with pytest.raises(KeyError):
            memory.get("bob", item["id"])
        with pytest.raises(KeyError):
            memory.save("bob", note_input, memory_id=item["id"])
        with pytest.raises(KeyError):
            memory.delete("bob", item["id"])
    own = [memory.public(item) for item in memory.list("alice", {"payments"})]
    assert len(own) == 3 and all(item["can_edit"] and item["can_delete"] for item in own)


def test_old_database_rows_are_shared_after_restart_without_rewriting_them(tmp_path: Path) -> None:
    memory = store(tmp_path)
    item = {
        "id": "legacy-note",
        "title": "Ledger queue",
        "text": "Check queue backlog",
        "kind": "note",
        "status": "active",
        "index_ids": ["payments"],
        "source_run_id": "old-private-chat",
        "created_at": timestamp(),
        "updated_at": timestamp(),
        "_match_question": "ledger",
    }
    with memory.runs._connect() as db:
        db.execute(
            "INSERT INTO memories VALUES (?,?,?,?,?,?)",
            (
                item["id"],
                "alice",
                "active",
                item["source_run_id"],
                item["updated_at"],
                json.dumps(item),
            ),
        )
    memory = store(tmp_path)
    public = memory.public(memory.list("bob", {"payments"})[0])
    assert public["visibility"] == "shared" and public["can_edit"] is False
    assert "source_run_id" not in public
    assert memory.retrieve("bob", "ledger", {"payments"}, {"payments"})[0]["id"] == item["id"]
    with memory.runs._connect() as db:
        row = db.execute("SELECT owner,data FROM memories WHERE id=?", (item["id"],)).fetchone()
    assert row[0] == "alice" and json.loads(row[1]) == item


def test_retrieval_ranks_older_matching_memories_beyond_ui_list_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_harness.memory as module

    monkeypatch.setattr(module, "LIST_LIMIT", 2)
    memory = store(tmp_path)
    wanted = memory.save(
        "alice",
        MemoryInput(
            title="Ledger backlog", text="Inspect worker", index_ids=["payments"], confirmed=True
        ),
    )
    for index in range(3):
        memory.save(
            "alice",
            MemoryInput(
                title=f"Inventory {index}",
                text="Unrelated stock",
                index_ids=["payments"],
                confirmed=True,
            ),
        )
    assert len(memory.list("bob", {"payments"})) == 2
    assert wanted["id"] not in {item["id"] for item in memory.list("bob", {"payments"})}
    assert memory.retrieve("bob", "ledger", {"payments"}, {"payments"})[0]["id"] == wanted["id"]


def test_shared_usage_is_counted_once_per_consumer_run_and_deleted_for_all_consumers(
    tmp_path: Path,
) -> None:
    memory = store(tmp_path)
    case = memory.record_outcome("alice", run(memory), index_ids=["payments"])
    assert case is not None
    run(memory, "bob-run", owner="bob")
    run(memory, "carol-run", owner="carol")
    for consumer, run_id in (("bob", "bob-run"), ("carol", "carol-run"), ("bob", "bob-run")):
        memory.record_usage(consumer, [case["id"], case["id"]], run_id)
    memory.record_usage("alice", [case["id"]], "bob-run")
    memory.record_usage("bob", [case["id"]], "missing")
    assert memory.list("carol", {"payments"})[0]["usage_count"] == 2
    memory.delete("alice", case["id"])
    assert memory.list("bob", {"payments"}) == []
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memory_usages").fetchone()[0] == 0


def test_individual_opt_out_does_not_disable_or_erase_shared_experience(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory)
    memory.record_outcome("alice", first, index_ids=["payments"])
    memory.set_enabled("alice", False)
    memory.set_enabled("bob", False)
    assert memory.retrieve("alice", "ledger", {"payments"}, {"payments"}) == []
    assert memory.retrieve("bob", "ledger", {"payments"}, {"payments"}) == []
    assert memory.retrieve("carol", "ledger", {"payments"}, {"payments"})
    assert (
        memory.record_outcome("bob", run(memory, "bob-run", owner="bob"), index_ids=["payments"])
        is None
    )
    memory.record_feedback("alice", first, rating="unhelpful", index_ids=["payments"])
    lesson = memory.retrieve("carol", "ledger", {"payments"}, {"payments"})[0]
    assert lesson["kind"] == "lesson" and lesson["feedback"]["rating"] == "unhelpful"
    assert len(memory.list("bob", {"payments"})) == 1


def test_feedback_of_another_users_run_cannot_change_shared_experience(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory)
    memory.record_outcome("alice", first, index_ids=["payments"])
    assert memory.record_feedback("bob", first, rating="unhelpful", index_ids=["payments"]) is None
    assert len(memory.list("bob", {"payments"})) == 1
    assert memory.retrieve("bob", "ledger", {"payments"}, {"payments"})[0]["feedback"] is None


def test_shared_retention_refuses_to_evict_confirmed_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_harness.memory as module

    monkeypatch.setattr(module, "MAX_TOTAL", 1)
    memory = store(tmp_path)
    memory.save(
        "alice", MemoryInput(title="Ledger", text="Keep", index_ids=["payments"], confirmed=True)
    )
    assert memory.record_outcome("bob", run(memory, owner="bob"), index_ids=["payments"]) is None
    with pytest.raises(ValueError, match="Память заполнена"):
        memory.save("bob", MemoryInput(title="Other", text="More", index_ids=["payments"]))
    assert len(memory.list("bob", {"payments"})) == 1


def test_one_author_can_contribute_beyond_the_ui_page_without_a_personal_quota(
    tmp_path: Path,
) -> None:
    from agent_harness.memory import LIST_LIMIT

    memory = store(tmp_path)
    for index in range(LIST_LIMIT + 1):
        memory.save(
            "alice",
            MemoryInput(
                title=f"Ledger {index}",
                text="Check backlog",
                index_ids=["payments"],
                confirmed=True,
            ),
        )
    assert len(memory.list("bob", {"payments"})) == LIST_LIMIT
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == LIST_LIMIT + 1


def test_shared_pages_filter_full_acl_and_private_drafts_before_counting_and_slicing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_harness.memory as module

    monkeypatch.setattr(module, "LIST_LIMIT", 2)
    memory = store(tmp_path)
    note = MemoryInput(
        title="Ledger guidance", text="Inspect queue", index_ids=["payments"], confirmed=True
    )
    oldest_own = memory.save("alice", note)
    foreign = memory.save("bob", note)
    memory.save("bob", note.model_copy(update={"confirmed": False}))
    memory.save("bob", note.model_copy(update={"index_ids": ["payments", "restricted"]}))
    memory.save("alice", note.model_copy(update={"index_ids": ["restricted"], "confirmed": False}))
    own_draft = memory.save("alice", note.model_copy(update={"confirmed": False}))
    case = memory.record_outcome(
        "bob", run(memory, "bob-case", owner="bob"), index_ids=["payments"]
    )
    assert case is not None

    first = memory.page("alice", {"payments"})
    assert first["total"] == 4 and first["next_offset"] == 2
    assert [item["id"] for item in first["items"]] == [case["id"], own_draft["id"]]
    second = memory.page("alice", {"payments"}, offset=first["next_offset"])
    assert second["total"] == 4 and second["next_offset"] is None
    assert [item["id"] for item in second["items"]] == [foreign["id"], oldest_own["id"]]
    assert memory.list("alice", {"payments"}, offset=2) == second["items"]
    assert memory.public(second["items"][0])["can_edit"] is False
    assert memory.public(second["items"][1])["can_edit"] is True
    # Loading an older page exposes the author's controls and supports the same write path.
    updated = memory.save(
        "alice", note.model_copy(update={"text": "Inspect retries"}), memory_id=oldest_own["id"]
    )
    assert updated["text"] == "Inspect retries" and updated["can_delete"] is True


def test_shared_page_beyond_end_and_empty_scope_do_not_expose_other_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_harness.memory as module

    monkeypatch.setattr(module, "LIST_LIMIT", 2)
    memory = store(tmp_path)
    note = MemoryInput(title="Ledger", text="Inspect", index_ids=["payments"], confirmed=True)
    memory.save("alice", note)
    memory.save("bob", note)
    for offset in (2, 1000):
        assert memory.page("carol", {"payments"}, offset=offset) == {
            "items": [],
            "total": 2,
            "next_offset": None,
        }
    assert memory.page("carol", set()) == {"items": [], "total": 0, "next_offset": None}
    with pytest.raises(ValueError, match="nonnegative"):
        memory.page("carol", {"payments"}, offset=-1)
