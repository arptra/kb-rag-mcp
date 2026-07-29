"""Read-only MCP-facing application functions, independent of storage internals."""

from __future__ import annotations

from typing import Any

from corporate_kb.models import Document, SearchFilters, SearchResult
from corporate_kb.service import KnowledgeService


class KnowledgeTools:
    """JSON-serializable read API backed only by KnowledgeService."""

    def __init__(self, service: KnowledgeService) -> None:
        self._service = service

    def search(
        self,
        *,
        query: str,
        top_k: int = 5,
        min_score: float | None = None,
        service: str | None = None,
        domain: str | None = None,
        document_type: str | None = None,
        status: str | None = "current",
        authority: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, Any]:
        results = self._service.search(
            query,
            top_k=top_k,
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
        stats = self._service.stats()
        return {
            "query": query,
            "result_count": len(results),
            "index": {
                "indexed_at": stats.indexed_at.isoformat(),
                "embedding_model": stats.embedding_model,
                "knowledge_hash": stats.knowledge_hash,
            },
            "results": [self._search_result(result) for result in results],
        }

    def get_document(self, document_id: str) -> dict[str, Any]:
        document = self._service.get_document(document_id)
        return {
            "document_id": document.document_id,
            "title": document.title,
            "content": document.content,
            "source_path": document.source_path,
            "source_url": document.source_url,
            "metadata": document.metadata,
            "content_hash": document.content_hash,
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

    @staticmethod
    def _search_result(result: SearchResult) -> dict[str, Any]:
        location = result.source_url or result.source_path
        section = f" — {result.heading_path}" if result.heading_path else ""
        payload = result.model_dump(mode="json")
        payload["citation"] = f"{result.title}{section} ({location})"
        payload["score"] = round(result.score, 6)
        return payload

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
