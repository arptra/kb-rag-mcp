"""Read-only MCP-facing application functions, independent of storage internals."""

from __future__ import annotations

from typing import Any

from corporate_kb.context import ContextCompressor, ContextExcerpt
from corporate_kb.models import Document, SearchFilters, SearchResult
from corporate_kb.service import KnowledgeService


class KnowledgeTools:
    """JSON-serializable read API backed only by KnowledgeService."""

    def __init__(self, service: KnowledgeService) -> None:
        self._service = service
        self._compressor = ContextCompressor()

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
        return {
            "query": query,
            "result_count": len(packed_results),
            "retrieved_candidate_count": len(results),
            "context_token_count": context_token_count,
            "results": packed_results,
        }

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
        return {
            "document_id": document.document_id,
            "title": document.title,
            "content": excerpt.text,
            "content_tokens": excerpt.token_count,
            "truncated": excerpt.truncated,
            "source_path": document.source_path,
            "source_url": document.source_url,
        }

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
        return {
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
        return {
            "document_count": len(documents),
            "documents": [self._document_metadata(document) for document in documents],
        }

    def stats(self) -> dict[str, Any]:
        stats = self._service.stats()
        payload = stats.model_dump(mode="json")
        payload["knowledge_directory"] = str(self._service.settings.knowledge_dir)
        payload["cache_directory"] = str(self._service.settings.cache_dir)
        return payload

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
