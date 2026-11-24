"""Real LangChain compaction preserves private history and processes long turns in full."""

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.context import (
    CompactionError,
    ContextBudgetError,
    ContextPolicy,
    ConversationContext,
)
from agent_harness.store import RunStore, timestamp

CONSTRAINT = "EARLY_REQUIREMENT: inspect staging only."


def store(tmp_path: Path) -> RunStore:
    return RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None))


def turn(db: RunStore, key: str, *, parent: str | None = None,
         question: str = CONSTRAINT, answer: str = "Observed state. " * 30) -> dict[str, Any]:
    run = {"id": key, "parent_run_id": parent, "question": question, "answer": answer,
           "index_ids": ["a"], "evidence": [], "status": "completed", "created_at": timestamp()}
    db.save("alice", run)
    return run


def policy(**kwargs: Any) -> ContextPolicy:
    return ContextPolicy(history_tokens=1800, trigger_tokens=1400, keep_tokens=500,
                         summary_input_tokens=6000, summary_output_tokens=300, **kwargs)


class Summarizer:
    def __init__(self) -> None:
        self.transcripts: list[list[dict[str, str]]] = []

    async def __call__(self, messages: list[dict[str, str]]) -> str:
        transcript = json.loads(messages[-1]["content"])
        self.transcripts.append(transcript)
        assert CONSTRAINT in str(transcript)
        return CONSTRAINT + " Preserve prior verified observations and pending checks."


async def prepare(context: ConversationContext, parent: str, invoke: Any,
                  **kwargs: Any) -> dict[str, Any]:
    return await context.prepare(
        owner=kwargs.pop("owner", "alice"), run={"id": "next", "parent_run_id": parent},
        allowed=kwargs.pop("allowed", {"a"}), selected={"a"},
        policy=kwargs.pop("policy", policy()), invoke_summary=invoke,
        cancel=kwargs.pop("cancel", Event()), **kwargs,
    )


@pytest.mark.asyncio
async def test_long_single_answer_is_partitioned_without_losing_any_fragment(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    original = "  FIRST observation.\n\n" + "Verified line.\n" * 1000 + " LAST_FACT end.  "
    turn(db, "long", answer=original)
    summarizer = Summarizer()
    result = await prepare(ConversationContext(db), "long", summarizer)
    assert result["compactions"] > 0
    assert result["tokens_after"] <= 1800
    assert CONSTRAINT in str(result["messages"])
    assert "LAST_FACT" in str(result["messages"])
    # Every original assistant fragment is either summarized or still verbatim.
    consumed = [item["content"] for transcript in summarizer.transcripts
                for item in transcript if item["role"] == "ai"]
    tail = [item["content"] for item in result["messages"] if item["role"] == "assistant"]
    assert "".join([*consumed, *tail]) == original
    assert db.get("alice", "long")["answer"] == original


@pytest.mark.asyncio
async def test_cache_survives_restart_and_is_scoped_to_owner_branch_and_sources(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    parent = None
    for number in range(24):
        key = f"r{number}"
        turn(db, key, parent=parent, question=CONSTRAINT if not parent else f"Detail {number}")
        parent = key
    assert parent
    summarizer = Summarizer()
    context = ConversationContext(db)
    first = await prepare(context, parent, summarizer)
    assert first["history_turns"] == 24 and first["compactions"]
    calls = len(summarizer.transcripts)
    restored = ConversationContext(store(tmp_path))
    second = await prepare(restored, parent, summarizer)
    assert second["reused_summary"] and second["compactions"] == 0
    assert len(summarizer.transcripts) == calls
    foreign = await prepare(restored, parent, summarizer, owner="bob")
    revoked = await prepare(restored, parent, summarizer, allowed=set())
    assert not foreign["messages"] and not revoked["messages"]
    assert foreign["history_truncated"] and revoked["history_truncated"]
    turn(db, "branch", parent="r0", question="OTHER_BRANCH_ONLY", answer="Branch conclusion")
    branch = await prepare(restored, "branch", summarizer)
    assert not branch["reused_summary"]
    assert "OTHER_BRANCH_ONLY" in str(branch["messages"])
    assert "Detail 23" not in str(branch["messages"])


@pytest.mark.asyncio
async def test_failure_and_cancellation_do_not_save_a_partial_summary(tmp_path: Path) -> None:
    db = store(tmp_path)
    turn(db, "long", answer="Large observation. " * 1000)
    context = ConversationContext(db)

    async def failing(messages: Any) -> str:
        raise RuntimeError("private provider error")

    with pytest.raises(CompactionError, match="Исходная переписка сохранена"):
        await prepare(context, "long", failing)
    cancel = Event()

    async def cancelled(messages: Any) -> str:
        cancel.set()
        return "Summary"

    with pytest.raises(asyncio.CancelledError):
        await prepare(context, "long", cancelled, cancel=cancel)
    with db._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0
    assert len(db.get("alice", "long")["answer"]) > 13000


@pytest.mark.asyncio
async def test_disabled_compaction_never_silently_discards_history(tmp_path: Path) -> None:
    db = store(tmp_path)
    turn(db, "long", answer="Long prior answer. " * 1000)
    with pytest.raises(ContextBudgetError):
        await prepare(ConversationContext(db), "long", Summarizer(), policy=policy(enabled=False))


@pytest.mark.asyncio
async def test_oversized_summary_is_not_sliced_or_cached(tmp_path: Path) -> None:
    db = store(tmp_path)
    turn(db, "long", answer="Long prior answer. " * 1000)

    async def huge(messages: Any) -> str:
        return "Still too long. " * 1000

    with pytest.raises(ContextBudgetError, match="Сводка превысила"):
        await prepare(ConversationContext(db), "long", huge)
    with db._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_deleting_chat_immediately_removes_its_private_summary(tmp_path: Path) -> None:
    db = store(tmp_path)
    turn(db, "long", answer="Long prior answer. " * 1000)
    await prepare(ConversationContext(db), "long", Summarizer())
    with db._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 1
    db.delete("alice", "long")
    with db._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM conversation_summaries").fetchone()[0] == 0
