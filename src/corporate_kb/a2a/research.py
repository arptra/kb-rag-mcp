"""Expose a task-scoped RAG toolset to the native SDK on A2A's existing worker pool."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from threading import Event
from typing import Any

from langchain_core.tools import tool

from corporate_kb.a2a.store import A2AStore
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.usage import UsageTracker


class ResearchBridge:
    def __init__(
        self, settings: Settings, store: A2AStore, catalog: RagCatalog, controller: Any,
        *, owns_controller: bool = True,
    ) -> None:
        self.controller = controller
        self.owns_controller = owns_controller
        self.settings = settings
        self.store = store
        self.catalog = catalog
        self._stopping = Event()

    def status(self) -> dict[str, Any]:
        state = self.controller.status()
        return {key: state.get(key) for key in ("enabled", "available", "state", "message")}

    def stop(self) -> None:
        self._stopping.set()
        if self.owns_controller:
            self.controller.stop()

    def run(
        self, agent: dict[str, Any], command: dict[str, Any], task_id: str, cancel: Event,
        progress: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        if not self.status().get("available"):
            raise ValueError("Агент GigaCode не готов. Откройте A2A и завершите вход.")
        return asyncio.run(self._run(agent, command, cancel, progress))

    async def _run(
        self, agent: dict[str, Any], command: dict[str, Any], cancel: Event,
        progress: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        from portal_agents.engine import run_turn
        from portal_agents.model import create_model

        requested = command.get("index_id")
        if requested and requested not in agent["index_ids"]:
            raise PermissionError("Index is outside this agent's access")
        selection = [requested] if requested else list(agent["index_ids"])
        citations: dict[tuple[str, str], dict[str, Any]] = {}
        metrics = {"source_calls": 0, "model_calls": 0, "tool_calls": 0}

        def authorized() -> bool:
            return (not cancel.is_set() and not self._stopping.is_set()
                    and self.status().get("enabled") is True
                    and self.store.authorized_snapshot(agent))

        async def check() -> bool:
            return await asyncio.to_thread(authorized)

        def source(index_id: str) -> KnowledgeTools:
            if index_id not in selection or not authorized():
                raise PermissionError("Access to this index is unavailable")
            # A2A calls have their own usage log, independent from MCP clients.
            return KnowledgeTools(self.catalog.service_for(index_id), usage=UsageTracker())

        def cite(index_id: str, item: dict[str, Any]) -> dict[str, Any]:
            doc_id = str(item["document_id"])
            metadata = {field: item.get(field) for field in (
                "source_url", "source_updated_at", "source_version", "implementation_date",
                "implementation_date_source",
            )}
            citation = {
                **metadata, "id": doc_id, "document_id": doc_id, "index_id": index_id,
                "title": str(item.get("title") or doc_id), "url": item.get("source_url"),
                "revision": item.get("source_version"),
                "text": str(item.get("excerpt") or item.get("content") or ""),
                "truncated": bool("excerpt" in item or item.get("truncated")),
            }
            citations[(index_id, doc_id)] = citation
            return citation

        @tool
        async def list_indexes(offset: int = 0) -> dict[str, Any]:
            """List the indexes assigned to this A2A task, a page of up to 40 at a time."""
            if offset < 0 or not await check():
                raise PermissionError("Index list unavailable")
            return {"index_ids": selection[offset:offset + 40], "total": len(selection)}

        @tool
        async def search_knowledge(index_id: str, query: str, limit: int = 4) -> list[Any]:
            """Search one assigned index for relevant current document excerpts and identifiers."""
            if not 1 <= limit <= 20:
                raise ValueError("limit must be between 1 and 20")
            def search() -> dict[str, Any]:
                return source(index_id).search(query=query, top_k=limit)

            result = await asyncio.to_thread(search)
            if not await check():
                raise PermissionError("Agent access changed during search")
            return [cite(index_id, item) for item in result["results"]]

        @tool
        async def read_document(index_id: str, document_id: str) -> dict[str, Any]:
            """Read a current document by its identifier in an assigned index."""
            def read() -> dict[str, Any]:
                return source(index_id).get_document(document_id=document_id)

            result = await asyncio.to_thread(read)
            if not await check():
                raise PermissionError("Agent access changed during document read")
            return cite(index_id, result)

        async def event(kind: str, name: str) -> None:
            if kind == "model":
                metrics["model_calls"] += 1
            elif kind == "tool_start":
                metrics["tool_calls"] += 1
                if name in {"search_knowledge", "read_document"}:
                    metrics["source_calls"] += 1
            phase = ({"search_knowledge": "search", "read_document": "read"}.get(name, name)
                     if kind == "tool_start" else kind)
            progress(phase, dict(metrics))

        if not await check():
            raise PermissionError("Agent access is no longer active")
        async with create_model(self.settings, controller=self.controller) as model:
            question = command["query"]
            limit = {"low": 20, "medium": 48, "high": 96}[command.get("effort", "medium")]
            if command.get("mode") == "quick":
                limit = min(limit, 20)
            operation = asyncio.create_task(run_turn(
                model, question, [list_indexes, search_knowledge, read_document],
                on_event=event, authorize=check, recursion_limit=limit,
            ))
            try:
                async with asyncio.timeout(self.settings.gigacode_timeout_seconds
                                           + self.settings.gigacode_auth_timeout_seconds):
                    while not operation.done():
                        await asyncio.wait({operation}, timeout=0.1)
                        if not await check():
                            raise PermissionError("Task cancelled or agent access changed")
                    answer = await operation
            finally:
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
        if not await check():
            raise PermissionError("Agent access is no longer active")
        evidence = list(citations.values())
        return {
            "action": "research", "version_scope": "latest", "answer": answer,
            "status": "completed", "citations": evidence, "warnings": [], "metrics": metrics,
            "coverage": {"full_corpus": False, "evidence_count": len(evidence),
                         "index_ids": selection, "note": "Приведены прочитанные источники."},
        }
