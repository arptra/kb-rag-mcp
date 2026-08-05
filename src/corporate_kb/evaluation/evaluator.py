"""Hit@K evaluation over a small curated question set."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from corporate_kb.models import SearchFilters
from corporate_kb.service import KnowledgeService


class EvaluationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    expected_documents: list[str] = Field(min_length=1)


class QuestionResult(BaseModel):
    question: str
    expected_documents: list[str]
    retrieved_source_paths: list[str]
    hits: dict[str, bool]


class EvaluationReport(BaseModel):
    top_k: int
    question_count: int
    metrics: dict[str, float]
    results: list[QuestionResult]


def load_evaluation_questions(questions_path: Path) -> list[EvaluationQuestion]:
    """Load and validate a curated benchmark without accepting client-controlled paths."""
    raw = json.loads(questions_path.resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Evaluation questions file must contain a JSON array")
    return [EvaluationQuestion.model_validate(item) for item in raw]


class Evaluator:
    def __init__(self, service: KnowledgeService, questions_path: Path) -> None:
        self._service = service
        self._questions_path = questions_path.resolve()

    def evaluate(self, *, top_k: int = 5) -> EvaluationReport:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        questions = load_evaluation_questions(self._questions_path)
        cutoffs = [cutoff for cutoff in (1, 3, 5) if cutoff <= top_k]
        hit_counts = {cutoff: 0 for cutoff in cutoffs}
        question_results: list[QuestionResult] = []
        for item in questions:
            results = self._service.search(
                item.question,
                top_k=top_k,
                filters=SearchFilters(status="current"),
            )
            retrieved = [result.source_path for result in results]
            expected = set(item.expected_documents)
            hits: dict[str, bool] = {}
            for cutoff in cutoffs:
                hit = bool(expected.intersection(retrieved[:cutoff]))
                hits[f"hit_at_{cutoff}"] = hit
                hit_counts[cutoff] += int(hit)
            question_results.append(
                QuestionResult(
                    question=item.question,
                    expected_documents=item.expected_documents,
                    retrieved_source_paths=retrieved,
                    hits=hits,
                )
            )
        denominator = len(questions)
        metrics = {
            f"hit_at_{cutoff}": (hit_counts[cutoff] / denominator * 100.0 if denominator else 0.0)
            for cutoff in cutoffs
        }
        return EvaluationReport(
            top_k=top_k,
            question_count=denominator,
            metrics=metrics,
            results=question_results,
        )
