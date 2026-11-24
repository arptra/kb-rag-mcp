"""Tool choices are shared only with retained supporting evidence and its complete ACL."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from test_harness_memory_auto import run, store

from agent_harness.memory import MemoryInput


def sample(doc: str, index: str, tool: str, remote: str = "") -> dict[str, Any]:
    return {"id": doc, "index_id": index, "title": "Ledger", "text": "Payment is queued.",
            "tool_origin": {"index_id": index, "tool": tool, "remote_tool": remote,
                            "arguments": "PRIVATE_ARGUMENT"}}


def test_shared_choice_credits_only_cited_source_in_retained_answer(tmp_path: Path) -> None:
    memory = store(tmp_path)
    a, b = "connection:support", "payments"
    evidence = [sample("used", a, "mcp_call", "getPayment"), sample("unused", a, "a2a_ask"),
                sample("duplicate", b, "search_knowledge"), sample("duplicate", a, "mcp_call", "v"),
                sample("snippet", b, "read_document"), sample("link", b, "read_document")]
    first = run(memory, index_ids=[a, b], evidence=evidence,
                answer="Queue holds payment [used]. [duplicate] `[snippet]` [link](https://test).",
                research_trace=[{"name": "model_response"}, {"name": "mcp_call"}])
    choice = {"tool": "mcp_call", "remote_tool": "getPayment", "index_id": a,
              "evidence_ids": ["used"]}
    item = memory.record_outcome("alice", first, index_ids=[a, b])
    assert item and item["strategy"]["successful_tools"] == [choice]
    assert item["strategy"]["tools"] == ["mcp_call"] and "PRIVATE" not in json.dumps(item)
    memory = store(tmp_path)
    found = memory.retrieve("bob", "ledger payments", {a, b}, {a, b})
    assert found[0]["strategy"]["successful_tools"] == [choice]
    assert not memory.retrieve("bob", "ledger payments", {a}, {a})
    assert not memory.retrieve("bob", "ledger payments", {a, b}, {b})

    negative = memory.record_feedback("alice", first, rating="unhelpful", index_ids=[a, b])
    assert negative and not negative["strategy"]["successful_tools"]
    positive = memory.record_feedback("alice", first, rating="helpful", index_ids=[a, b])
    assert positive and positive["strategy"]["successful_tools"] == [choice]
    memory.save("alice", MemoryInput(title="Confirmed", text="Operator note", kind="case",
                                     confirmed=True, index_ids=[a]), memory_id=item["id"])
    revised = memory.record_feedback("alice", first, rating="unhelpful", index_ids=[a, b])
    assert revised and revised["text"] == "Operator note" and revised["status"] == "active"
    assert not revised["strategy"]["successful_tools"] and set(revised["index_ids"]) == {a, b}


@pytest.mark.parametrize("status", ["partial", "failed", "timed_out", "interrupted"])
def test_incomplete_run_records_source_observation_separately(tmp_path: Path, status: str) -> None:
    memory = store(tmp_path)
    first = run(memory, status=status, evidence=[sample("doc", "payments", "read_document")])
    item = memory.record_outcome("alice", first, index_ids=["payments"])
    assert item and not item["strategy"]["successful_tools"]
    assert item["strategy"]["observed_tools"] == [
        {"tool": "read_document", "index_id": "payments", "evidence_ids": ["doc"]},
    ]


def test_concurrent_failures_preserve_each_tool_and_only_known_reasons(tmp_path: Path) -> None:
    memory = store(tmp_path)
    first = run(memory, status="running", index_ids=["connection:support"], evidence=[])
    barrier = Barrier(4)

    def fail(number: int) -> None:
        barrier.wait(timeout=5)
        memory.record_failure(
            "alice", first, index_ids=["connection:support"], operation="external", tool="mcp_call",
            remote_tool=f"lookup_{number}", reason="timeout" if number > 0 else "PRIVATE_STACK",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fail, range(4)))
    first["status"] = "failed"
    memory.runs.save("alice", first)
    item = memory.record_outcome("alice", first, index_ids=["connection:support"])
    assert item and len(item["failures"]) == 4 and "PRIVATE_STACK" not in json.dumps(item)
    failures = {f["remote_tool"]: f.get("reason") for f in item["failures"]}
    assert failures == {f"lookup_{n}": "timeout" if n else None for n in range(4)}
    allowed = {"connection:support"}
    found = memory.retrieve("bob", "ledger payments", allowed, allowed)
    assert found[0]["failures"] == item["failures"]


def test_backfill_uses_owner_and_cited_rag_evidence_not_external_trace(tmp_path: Path) -> None:
    memory = store(tmp_path)
    a, b = "payments", "connection:support"
    evidence = [
        {"id": "doc", "index_id": a, "text": "Current route", "title": "Route",
         "read_document": True},
        {"id": "remote", "index_id": b, "text": "Ticket", "title": "Incident",
         "checked_with": "mcp_call"},
    ]
    first = run(memory, evidence=evidence, answer="Queue checked [doc] [remote].", index_ids=[a, b])
    card = memory.record_outcome("alice", first, index_ids=[a, b])
    assert card
    with memory.runs._connect() as db:
        original = memory.get("alice", card["id"])
        original = {k: v for k, v in original.items() if k not in {"_reader", "_author"}}
        original["strategy"].pop("successful_tools")
        db.execute("UPDATE memories SET data=? WHERE id=?", (json.dumps(original), card["id"]))
    memory = store(tmp_path)
    learned = memory.get("alice", card["id"])
    assert learned["strategy"]["successful_tools"] == [
        {"tool": "read_document", "index_id": a, "evidence_ids": ["doc"]},
    ]
    assert learned["updated_at"] == original["updated_at"]
    memory.runs.delete("alice", first["id"])
    assert store(tmp_path).list("bob", {a, b})[0]["strategy"] == learned["strategy"]


def test_unsaved_automatic_case_has_inspectable_reason(tmp_path: Path, monkeypatch: Any) -> None:
    memory = store(tmp_path)
    monkeypatch.setattr(memory, "maximum", lambda: 1)
    memory.save("alice", MemoryInput(title="Confirmed", text="Keep", index_ids=["payments"],
                                     confirmed=True))
    first = run(memory)
    assert memory.record_outcome("alice", first, index_ids=["payments"]) is None
    assert first["memory_contribution"] == {"updated": False, "reason": "full"}
    memory.set_enabled("alice", False)
    assert memory.record_outcome("alice", first, index_ids=["payments"]) is None
    assert first["memory_contribution"] == {"updated": False, "reason": "disabled"}


@pytest.mark.parametrize("invalid", ["scope", "foreign", "feedback", "malformed", "failed_read"])
def test_old_case_cannot_inherit_unproven_or_restricted_choice(
    tmp_path: Path, invalid: str,
) -> None:
    memory = store(tmp_path)
    evidence: dict[str, Any] = {"id": "doc", "text": "Queue detail", "index_id": "payments",
                                "title": "Record", "read_document": True}
    first = run(memory, evidence=[evidence])
    card = memory.record_outcome("alice", first, index_ids=["payments"])
    assert card
    with memory.runs._connect() as db:
        item = json.loads(db.execute("SELECT data FROM memories WHERE id=?", (card["id"],))
                          .fetchone()[0])
        item["strategy"].pop("successful_tools")
        if invalid == "scope":
            first["context_index_ids"] = ["restricted"]
        elif invalid == "feedback":
            first["feedback"] = {"rating": "unhelpful"}
        elif invalid == "malformed":
            first["evidence"] = [{"id": "missing index and body"}]
        elif invalid == "failed_read":
            evidence["tool_outcome"] = "failed"
        else:
            db.execute("UPDATE memories SET owner='another-user' WHERE id=?", (card["id"],))
        db.execute("UPDATE runs SET data=? WHERE id=?", (json.dumps(first), first["id"]))
        db.execute("UPDATE memories SET data=? WHERE id=?", (json.dumps(item), card["id"]))
    restored = store(tmp_path).list("reader", {"payments"})
    assert len(restored) == 1 and not restored[0]["strategy"].get("successful_tools")
