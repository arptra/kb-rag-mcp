import json
from pathlib import Path

from corporate_kb.evaluation.evaluator import Evaluator
from corporate_kb.models import SearchResult


def result(path: str, rank: int) -> SearchResult:
    return SearchResult(
        rank=rank,
        score=1.0 / rank,
        chunk_id=f"chunk-{rank}",
        document_id=f"doc-{rank}",
        title=path,
        heading_path=path,
        text="text",
        source_path=path,
        metadata={},
    )


class FakeService:
    def search(self, query: str, *, top_k: int, filters):
        del query, filters
        return [result("wrong.md", 1), result("expected.md", 2), result("other.md", 3)][:top_k]


def test_evaluation_hit_at_1_3_5(tmp_path: Path) -> None:
    questions = tmp_path / "questions.json"
    questions.write_text(
        json.dumps([{"question": "q", "expected_documents": ["expected.md"]}]),
        encoding="utf-8",
    )

    report = Evaluator(FakeService(), questions).evaluate(top_k=5)  # type: ignore[arg-type]

    assert report.metrics == {"hit_at_1": 0.0, "hit_at_3": 100.0, "hit_at_5": 100.0}
    assert report.results[0].retrieved_source_paths == ["wrong.md", "expected.md", "other.md"]
