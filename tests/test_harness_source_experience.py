"""Source ordering learns topic-specific outcomes, independently of answer reuse."""

import json
import time
from pathlib import Path

from test_harness_memory_auto import run, store
from test_harness_tool_memory import sample

from agent_harness.answer_quality import cited_evidence_refs


def test_routes_are_shared_persistent_scoped_and_reversible(tmp_path: Path) -> None:
    memory = store(tmp_path)
    scope = {"wiki", "archive", "logs", "manual", "hidden"}
    question = "Current settlement window for violet ledgers"
    first = run(
        memory, question=question, answer="Valid until Tuesday [d1].", index_ids=sorted(scope),
        context_index_ids=["parent"], evidence=[
            sample("d1", "wiki", "read_document"), sample("d2", "manual", "read_document"),
        ],
        coverage={"wiki": {"status": "searched", "count": 1},
                  "archive": {"status": "empty", "count": 0},
                  "manual": {"status": "searched", "count": 1},
                  "logs": {"status": "failed", "count": 0},
                  "hidden": {"status": "pending", "count": 0}},
        _source_assessments={"manual": "irrelevant", "logs": "irrelevant"},
        _source_revisions={"archive": "a1", "manual": "b1"},
    )
    case = memory.record_outcome("alice", first, index_ids=sorted(scope))
    assert case and case["strategy"]["source_outcomes"] == {
        "wiki": "useful", "archive": "empty", "manual": "irrelevant",
    }
    memory.record_outcome("alice", first, index_ids=sorted(scope))
    memory = store(tmp_path)
    allowed = scope | {"parent"}

    def scores(query=question, *, permitted=allowed, revisions=None):
        return memory.source_scores("bob", query, permitted, scope, revisions=revisions)

    ranked = scores("Current settlement window for violet ledger", revisions={"archive": "a1"})
    assert 0 < ranked["wiki"] < 0.6
    assert -0.45 < ranked["archive"] < ranked["manual"] < 0
    assert "logs" not in ranked and "hidden" not in ranked
    assert scores("Granite fracture mechanics") == {}
    assert scores(permitted=scope) == {}  # missing parent conversation permission
    assert scores(permitted=allowed - {"hidden"}) == {}
    assert "archive" not in scores(revisions={"archive": "a2", "manual": "b1"})
    assert memory.source_scores("alice", question, allowed, scope, exclude_run_id=first["id"]) == {}
    assert set(memory.source_scores("bob", question, allowed, {"wiki"})) == {"wiki"}
    memory.set_enabled("bob", False)
    assert scores() == {}
    memory.set_enabled("bob", True)
    with memory.runs._connect() as db:
        terms, = db.execute("SELECT features FROM source_route_terms").fetchone()
        data, = db.execute("SELECT outcomes FROM source_routes").fetchone()
        assert "violet" not in terms and "Tuesday" not in data and "d1" not in data
        assert set(json.loads(data)) == {"wiki", "archive", "manual"}
    memory.record_feedback("alice", first, rating="unhelpful", index_ids=sorted(scope))
    assert "wiki" not in scores()
    memory.record_feedback("alice", first, rating="helpful", index_ids=sorted(scope))
    assert scores()["wiki"] > 0
    memory.delete("alice", case["id"])
    assert scores() == {}
    with memory.runs._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM source_route_terms").fetchone() == (0,)


def test_failed_or_stale_search_is_not_an_absence_claim(tmp_path: Path) -> None:
    memory = store(tmp_path)
    state = run(memory, question="Queue processing interval", status="partial",
                stop_reason="model_unavailable", evidence=[], index_ids=["logs"],
                coverage={"logs": {"status": "empty", "count": 0}})
    memory.record_outcome("alice", state, index_ids=["logs"])
    assert memory.source_scores("bob", state["question"], {"logs"}, {"logs"}) == {}
    state.update(status="completed", stop_reason="completed")
    memory.record_outcome("alice", state, index_ids=["logs"])
    assert memory.source_scores("bob", state["question"], {"logs"}, {"logs"})["logs"] < 0
    with memory.runs._connect() as db:
        db.execute("UPDATE source_routes SET observed=?", (time.time() - 32 * 86400,))
    assert memory.source_scores("bob", state["question"], {"logs"}, {"logs"}) == {}
    # A second query can find a hit after all in-memory evidence slots have filled.
    state["_search_hit_ids"] = {"logs": ["item-that-did-not-fit"]}
    memory.record_outcome("alice", state, index_ids=["logs"])
    assert memory.source_scores("bob", state["question"], {"logs"}, {"logs"}) == {}


def test_old_cited_cases_seed_new_index_without_inventing_misses(tmp_path: Path) -> None:
    memory = store(tmp_path)
    scope = {"kb", "misc"}
    first = run(memory, question="Интервал обработки платежей", answer="Каждые пять минут [yes].",
                index_ids=sorted(scope), evidence=[sample("yes", "kb", "read_document")],
                coverage={"kb": {"status": "searched", "count": 1},
                          "misc": {"status": "empty", "count": 0}})
    memory.record_outcome("alice", first, index_ids=sorted(scope))
    with memory.runs._connect() as db:
        db.execute("DELETE FROM source_routes")
        db.execute("DELETE FROM source_route_config")
        db.execute("UPDATE memories SET data=json_remove(data,'$.strategy.source_outcomes')")
    memory = store(tmp_path)
    learned = memory.source_scores("bob", "Какие интервалы обработки платежа?", scope, scope)
    assert set(learned) == {"kb"} and learned["kb"] > 0


def test_shared_route_credits_reviewed_passage_and_resolves_duplicate_ids(tmp_path: Path) -> None:
    memory = store(tmp_path)
    scope = {"wiki", "process"}
    evidence = [sample("section-1", "wiki", "read_document"),
                sample("section-1", "process", "read_document")]
    evidence[0]["text"] = "A different procedure is documented here."
    assert cited_evidence_refs("See [section-1].", evidence) == set()
    assert cited_evidence_refs("See [2].", evidence) == {("process", "section-1")}
    state = run(memory, question="What is the settlement queue status?", index_ids=sorted(scope),
                answer="Queued [1] [2].", evidence=evidence, _retrieval_support={
                    "index_id": "process", "id": "section-1", "quote": "Payment is queued.",
                })
    record = memory.record_outcome("alice", state, index_ids=sorted(scope))
    assert record and record["strategy"]["source_outcomes"] == {"process": "useful"}
    assert record["strategy"]["successful_tools"][0]["index_id"] == "process"
    assert set(memory.source_scores("bob", state["question"], scope, scope)) == {"process"}
    state["answer"] = "Queued [1]."
    record = memory.record_outcome("alice", state, index_ids=sorted(scope))
    assert record and record["strategy"]["successful_tools"] == []
    assert memory.source_scores("bob", state["question"], scope, scope) == {}
