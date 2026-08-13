"""Read-only MCP-facing application functions, independent of storage internals."""

from __future__ import annotations

import json
import math
import secrets
import time
from threading import Lock
from typing import Any

from corporate_kb.context import ContextCompressor, ContextExcerpt
from corporate_kb.evaluation.evaluator import load_evaluation_questions
from corporate_kb.models import Document, SearchFilters, SearchResult
from corporate_kb.service import KnowledgeService
from corporate_kb.ssot import SsotContextBuilder
from corporate_kb.usage import UsageTracker

_BENCHMARK_LOCK = Lock()
_BASELINE_TOP_K = 5
_PACKED_TOP_K = 3


class KnowledgeTools:
    """JSON-serializable read API backed only by KnowledgeService."""

    def __init__(
        self,
        service: KnowledgeService,
        *,
        ssot_service: KnowledgeService | None = None,
        usage: UsageTracker | None = None,
    ) -> None:
        self._service = service
        self._compressor = ContextCompressor()
        self._ssot = SsotContextBuilder(ssot_service or service)
        self.usage = usage or UsageTracker()

    def ssot_context(
        self,
        *,
        question: str,
        mode: str = "implementation",
    ) -> dict[str, Any]:
        """Return one current cross-service SSOT brief assembled by internal searches."""
        if mode not in {"business", "implementation"}:
            raise ValueError("mode must be business or implementation")
        payload = self._ssot.build(question=question, mode=mode)
        self.usage.record(
            "ssot_context",
            context_tokens=int(payload["context_token_count"]),
            result_count=int(payload["service_count"]),
        )
        return payload

    def search(
        self,
        *,
        query: str,
        top_k: int = 3,
        min_score: float | None = None,
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = "current",
        authority: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        candidate_k = min(20, max(top_k, self._service.settings.search_candidate_k))
        results = self._service.search(
            query,
            top_k=candidate_k,
            min_score=min_score,
            filters=SearchFilters(
                service=service,
                domain=domain,
                document_type=document_type,
                status=status,
                authority=authority,
                source_type=source_type,
            ),
        )
        packed_results, context_token_count = self._pack_search_results(
            query=query,
            results=results,
            requested_top_k=top_k,
        )
        payload = {
            "query": query,
            "result_count": len(packed_results),
            "retrieved_candidate_count": len(results),
            "context_token_count": context_token_count,
            "results": packed_results,
        }
        self.usage.record(
            "kb_search",
            context_tokens=context_token_count,
            result_count=len(packed_results),
        )
        return payload

    def get_document(
        self,
        document_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        document = self._service.get_document(document_id)
        excerpt = self._excerpt(
            query=document.title,
            text=document.content,
            max_tokens=max_tokens,
        )
        payload = {
            "document_id": document.document_id,
            "title": document.title,
            "content": excerpt.text,
            "content_tokens": excerpt.token_count,
            "truncated": excerpt.truncated,
            "source_path": document.source_path,
            "source_url": document.source_url,
        }
        self.usage.record("kb_get_document")
        return payload

    def get_chunk(self, chunk_id: str, max_tokens: int | None = None) -> dict[str, Any]:
        """Return a bounded, lazily requested source chunk from a search result."""
        chunk = self._service.get_chunk(chunk_id)
        excerpt = self._excerpt(
            query=f"{chunk.title} {chunk.heading_path}",
            text=chunk.text,
            max_tokens=max_tokens,
        )
        location = chunk.source_url or chunk.source_path
        section = f" — {chunk.heading_path}" if chunk.heading_path else ""
        payload = {
            "chunk_id": chunk.chunk_id,
            "document_id": chunk.document_id,
            "title": chunk.title,
            "heading_path": chunk.heading_path,
            "text": excerpt.text,
            "text_tokens": excerpt.token_count,
            "truncated": excerpt.truncated,
            "source_path": chunk.source_path,
            "source_url": chunk.source_url,
            "citation": f"{chunk.title}{section} ({location})",
        }
        self.usage.record("kb_get_chunk")
        return payload

    def list_documents(
        self,
        *,
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        documents = self._service.list_documents(
            filters=SearchFilters(
                service=service,
                domain=domain,
                document_type=document_type,
                status=status,
            ),
            limit=limit,
        )
        payload = {
            "document_count": len(documents),
            "documents": [self._document_metadata(document) for document in documents],
        }
        self.usage.record("kb_list_documents")
        return payload

    def stats(self) -> dict[str, Any]:
        stats = self._service.stats()
        payload = stats.model_dump(mode="json")
        payload["knowledge_directory"] = str(self._service.settings.knowledge_dir)
        payload["cache_directory"] = str(self._service.settings.cache_dir)
        self.usage.record("kb_stats")
        return payload

    def run_context_benchmark(self, password: str) -> dict[str, Any]:
        """Compare legacy full chunks with packed excerpts after secondary authentication."""
        self._validate_benchmark_password(password)
        if not _BENCHMARK_LOCK.acquire(blocking=False):
            raise RuntimeError("Context benchmark is already running")
        try:
            payload = self._run_context_benchmark()
            self.usage.record("kb_run_context_benchmark")
            return payload
        finally:
            _BENCHMARK_LOCK.release()

    def _validate_benchmark_password(self, supplied: str) -> None:
        configured = self._service.settings.benchmark_password
        if configured is None:
            raise PermissionError("Context benchmark is disabled by the server administrator")
        expected = configured.get_secret_value()
        if len(expected) < 16:
            raise PermissionError("Context benchmark is disabled by the server administrator")
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise PermissionError("Invalid benchmark password")

    def _run_context_benchmark(self) -> dict[str, Any]:
        settings = self._service.settings
        questions = load_evaluation_questions(settings.benchmark_questions_path)
        if not questions:
            raise ValueError("Benchmark must contain at least one question")
        if len(questions) > settings.benchmark_max_questions:
            raise ValueError(
                f"Benchmark contains {len(questions)} questions; "
                f"maximum is {settings.benchmark_max_questions}"
            )

        retrieval_k = min(20, max(_BASELINE_TOP_K, settings.search_candidate_k))
        baseline_hits = 0
        candidate_hits = 0
        packed_hits = 0
        baseline_tokens = 0
        packed_tokens = 0
        baseline_bytes = 0
        packed_bytes = 0
        elapsed_ms: list[float] = []
        failures: list[dict[str, Any]] = []

        for item in questions:
            started = time.perf_counter()
            candidates = self._service.search(
                item.question,
                top_k=retrieval_k,
                filters=SearchFilters(status="current"),
            )
            packed, context_tokens = self._pack_search_results(
                query=item.question,
                results=candidates,
                requested_top_k=_PACKED_TOP_K,
            )
            elapsed_ms.append((time.perf_counter() - started) * 1000.0)

            baseline_payload = self._baseline_payload(
                query=item.question,
                results=candidates[:_BASELINE_TOP_K],
            )
            packed_payload = {
                "query": item.question,
                "result_count": len(packed),
                "retrieved_candidate_count": len(candidates),
                "context_token_count": context_tokens,
                "results": packed,
            }
            baseline_json = self._compact_json(baseline_payload)
            packed_json = self._compact_json(packed_payload)
            baseline_tokens += self._compressor.count_tokens(baseline_json)
            packed_tokens += self._compressor.count_tokens(packed_json)
            baseline_bytes += len(baseline_json.encode("utf-8"))
            packed_bytes += len(packed_json.encode("utf-8"))

            expected = set(item.expected_documents)
            baseline_paths = [result.source_path for result in candidates[:_BASELINE_TOP_K]]
            candidate_paths = [result.source_path for result in candidates]
            packed_paths = [str(result["source_path"]) for result in packed]
            baseline_hit = bool(expected.intersection(baseline_paths))
            candidate_hit = bool(expected.intersection(candidate_paths))
            packed_hit = bool(expected.intersection(packed_paths))
            baseline_hits += int(baseline_hit)
            candidate_hits += int(candidate_hit)
            packed_hits += int(packed_hit)
            if not packed_hit:
                failures.append(
                    {
                        "question": item.question,
                        "expected_documents": item.expected_documents,
                        "packed_source_paths": packed_paths,
                        "candidate_hit": candidate_hit,
                    }
                )

        count = len(questions)
        baseline_rate = self._rate(baseline_hits, count)
        packed_rate = self._rate(packed_hits, count)
        return {
            "status": "passed" if packed_rate >= baseline_rate else "quality_regression",
            "question_count": count,
            "quality": {
                "candidate_hit_rate_percent": self._rate(candidate_hits, count),
                "baseline_hit_at_5_percent": baseline_rate,
                "packed_hit_at_3_percent": packed_rate,
                "quality_delta_percentage_points": round(packed_rate - baseline_rate, 2),
            },
            "context": {
                "baseline_payload_token_estimate": baseline_tokens,
                "packed_payload_token_estimate": packed_tokens,
                "token_reduction_percent": self._reduction(baseline_tokens, packed_tokens),
                "baseline_json_bytes": baseline_bytes,
                "packed_json_bytes": packed_bytes,
                "byte_reduction_percent": self._reduction(baseline_bytes, packed_bytes),
            },
            "latency_ms": {
                "average": round(sum(elapsed_ms) / count, 2) if count else 0.0,
                "p95": self._percentile(elapsed_ms, 0.95),
            },
            "policy": {
                "retrieval_candidates": retrieval_k,
                "baseline_top_k": _BASELINE_TOP_K,
                "packed_top_k": _PACKED_TOP_K,
                "excerpt_tokens": settings.search_excerpt_tokens,
                "context_tokens": settings.search_context_tokens,
                "max_chunks_per_document": settings.search_max_chunks_per_document,
            },
            "failed_questions": failures,
        }

    @staticmethod
    def _baseline_payload(*, query: str, results: list[SearchResult]) -> dict[str, Any]:
        return {
            "query": query,
            "result_count": len(results),
            "results": [result.model_dump(mode="json") for result in results],
        }

    @staticmethod
    def _compact_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    @staticmethod
    def _rate(hits: int, count: int) -> float:
        return round(hits / count * 100.0, 2) if count else 0.0

    @staticmethod
    def _reduction(before: int, after: int) -> float:
        return round((1.0 - after / before) * 100.0, 2) if before else 0.0

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = max(0, math.ceil(len(ordered) * percentile) - 1)
        return round(ordered[index], 2)

    def _pack_search_results(
        self,
        *,
        query: str,
        results: list[SearchResult],
        requested_top_k: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Choose diverse evidence under one strict LLM context budget."""
        settings = self._service.settings
        packed: list[dict[str, Any]] = []
        chunks_per_document: dict[str, int] = {}
        remaining = settings.search_context_tokens
        for result in results:
            if len(packed) >= requested_top_k or remaining < 1:
                break
            seen = chunks_per_document.get(result.document_id, 0)
            if seen >= settings.search_max_chunks_per_document:
                continue
            excerpt = self._compressor.excerpt(
                query=query,
                text=result.text,
                max_tokens=min(settings.search_excerpt_tokens, remaining),
            )
            if not excerpt.text:
                continue
            packed.append(
                self._search_result(
                    result,
                    excerpt.text,
                    excerpt.token_count,
                    excerpt.truncated,
                )
            )
            chunks_per_document[result.document_id] = seen + 1
            remaining -= excerpt.token_count
        return packed, settings.search_context_tokens - remaining

    def _excerpt(
        self,
        *,
        query: str,
        text: str,
        max_tokens: int | None,
    ) -> ContextExcerpt:
        limit = self._service.settings.document_context_tokens if max_tokens is None else max_tokens
        if not 1 <= limit <= self._service.settings.document_context_tokens:
            raise ValueError(
                "max_tokens must be between 1 and "
                f"{self._service.settings.document_context_tokens}"
            )
        return self._compressor.excerpt(query=query, text=text, max_tokens=limit)

    @staticmethod
    def _search_result(
        result: SearchResult,
        excerpt: str,
        excerpt_tokens: int,
        truncated: bool,
    ) -> dict[str, Any]:
        location = result.source_url or result.source_path
        section = f" — {result.heading_path}" if result.heading_path else ""
        return {
            "rank": result.rank,
            "score": round(result.score, 6),
            "chunk_id": result.chunk_id,
            "document_id": result.document_id,
            "title": result.title,
            "heading_path": result.heading_path,
            "excerpt": excerpt,
            "excerpt_tokens": excerpt_tokens,
            "truncated": truncated,
            "source_path": result.source_path,
            "source_url": result.source_url,
            "citation": f"{result.title}{section} ({location})",
        }

    @staticmethod
    def _document_metadata(document: Document) -> dict[str, Any]:
        return {
            "document_id": document.document_id,
            "title": document.title,
            "source_path": document.source_path,
            "source_type": document.source_type,
            "source_id": document.source_id,
            "source_url": document.source_url,
            "content_hash": document.content_hash,
            "metadata": document.metadata,
            "loaded_at": document.loaded_at.isoformat(),
        }
