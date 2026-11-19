"""Scoped A2A adapter for the shared, bounded knowledge research workflow."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from threading import Event, Lock, Thread
from typing import Any
from uuid import uuid4

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import ACTIVE
from corporate_kb.a2a.store import A2AStore, clean
from corporate_kb.catalog import RagCatalog
from corporate_kb.config import Settings
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.usage import UsageTracker

logger = logging.getLogger(__name__)


class AgentSources:
    def __init__(self, store: A2AStore, catalog: RagCatalog) -> None:
        self.store, self.catalog = store, catalog
        self.grants: dict[str, tuple[dict[str, Any], str]] = {}
        self.citations: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}

    def check(self, principal: Principal, index_id: str | None = None) -> dict[str, Any]:
        agent, _ = self.grants[principal.owner_id]
        if not self.store.authorized_snapshot(agent):
            raise PermissionError("Agent access changed during research")
        if index_id is not None and index_id not in agent["index_ids"]:
            raise PermissionError("Index is outside this agent's access")
        return agent

    def log(self, principal: Principal, phase: str, message: str) -> None:
        agent, task_id = self.grants[principal.owner_id]
        self.store.record_event(agent, task_id, phase, message)
        logger.info(
            "A2A research task=%s agent=%s phase=%s %s", task_id, agent["id"], phase, clean(message)
        )

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        agent = self.check(principal)
        return [
            {"id": index, "name": index}
            for index in agent["index_ids"]
            if self.catalog.has_index(index)
        ]

    def evidence(self, principal: Principal, index_id: str, item: dict[str, Any]) -> Evidence:
        doc_id = str(item["document_id"])
        fields = (
            "source_url",
            "source_updated_at",
            "source_version",
            "implementation_date",
            "implementation_date_source",
            "truncated",
        )
        details = {key: item.get(key) for key in fields}
        self.citations[principal.owner_id][(index_id, doc_id)] = details
        text = str(item.get("excerpt") or item.get("text") or item.get("content") or "")
        date = details.get("source_updated_at")
        text = (
            f"Фрагмент документа. Дата обновления: {date or 'неизвестна'}. "
            f"Дата внедрения: {details.get('implementation_date') or 'не подтверждена'}.\n{text}"
        )
        details["source_excerpt_chars"] = len(text)
        if "excerpt" in item:
            details["truncated"] = True
        return Evidence(
            id=doc_id,
            title=str(item.get("title", doc_id)),
            index_id=index_id,
            text=text,
            url=item.get("source_url") or None,
            revision=str(item.get("source_version") or "") or None,
        )

    async def search(
        self, *, query: str, index_id: str, limit: int, principal: Principal, cancel: Event
    ) -> list[Evidence]:
        self.check(principal, index_id)
        if cancel.is_set():
            raise asyncio.CancelledError
        self.log(principal, "search", f"Поиск в индексе {index_id}: {clean(query, 300)}")
        # Dedicated tracker: internal research never increments the MCP counter.
        tools = KnowledgeTools(self.catalog.service_for(index_id), usage=UsageTracker())
        result = await asyncio.to_thread(tools.search, query=query, top_k=limit)
        self.check(principal, index_id)
        self.log(principal, "search_result", f"Найдено фрагментов: {result['result_count']}")
        # The workflow keys evidence by document. Keep the highest ranked chunk,
        # rather than letting a weaker later chunk overwrite it silently.
        best: dict[str, dict[str, Any]] = {}
        for item in result["results"]:
            best.setdefault(str(item["document_id"]), item)
        return [self.evidence(principal, index_id, item) for item in best.values()]

    async def read(
        self, *, document_id: str, index_id: str, principal: Principal, cancel: Event
    ) -> Evidence:
        self.check(principal, index_id)
        if cancel.is_set():
            raise asyncio.CancelledError
        self.log(principal, "read", f"Чтение документа {document_id} в индексе {index_id}")
        tools = KnowledgeTools(self.catalog.service_for(index_id), usage=UsageTracker())
        result = await asyncio.to_thread(tools.get_document, document_id=document_id)
        self.check(principal, index_id)
        return self.evidence(principal, index_id, result)


class ResearchBridge:
    """Keep the workflow on one event loop; A2A's existing workers await its results."""

    def __init__(
        self, settings: Settings, store: A2AStore, catalog: RagCatalog, controller: Any
    ) -> None:
        self.controller = controller
        self.source = AgentSources(store, catalog)
        self.settings = HarnessSettings(
            enabled=True,
            state_dir=settings.a2a_state_dir / "research",
            max_indexes=16,
            authentication_timeout_seconds=settings.gigacode_auth_timeout_seconds,
        )
        self._lock = Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: Thread | None = None
        self._runtime: HarnessRuntime | None = None
        self._stopped = False

    def status(self) -> dict[str, Any]:
        # Never propagate the browser authorization URL to external callers.
        state = self.controller.status()
        return {key: state.get(key) for key in ("enabled", "available", "state", "message")}

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._stopped:
                raise ValueError("Агент останавливается")
            if self._loop is None:
                loop = asyncio.new_event_loop()
                self._runtime = HarnessRuntime(self.settings, self.source, self.controller)
                self._thread = Thread(target=loop.run_forever, name="a2a-research", daemon=True)
                self._thread.start()
                asyncio.run_coroutine_threadsafe(self._runtime.start(), loop).result(timeout=15)
                self._loop = loop
            return self._loop

    def run(
        self,
        agent: dict[str, Any],
        command: dict[str, Any],
        task_id: str,
        cancel: Event,
        progress: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        state = self.status()
        if not state.get("available"):
            raise ValueError(
                "Агент GigaCode не готов. Администратору нужно открыть A2A → "
                "Включить агента и завершить вход. " + str(state.get("message") or "")
            )
        loop = self._ensure_started()
        return asyncio.run_coroutine_threadsafe(
            self._run(agent, command, task_id, cancel, progress),
            loop,
        ).result()

    async def _run(
        self,
        agent: dict[str, Any],
        command: dict[str, Any],
        task_id: str,
        cancel: Event,
        progress: Callable[[str, dict[str, Any]], None],
    ) -> dict[str, Any]:
        assert self._runtime is not None
        runtime = self._runtime
        principal = Principal("a2a:" + agent["id"] + ":" + uuid4().hex)
        self.source.grants[principal.owner_id] = (agent, task_id)
        self.source.citations[principal.owner_id] = {}
        run: dict[str, Any] | None = None
        try:
            self.source.check(principal)
            requested = [command["index_id"]] if command.get("index_id") else []
            run = await runtime.submit(
                principal,
                RunRequest(
                    question=command["query"],
                    mode=command.get("mode", "investigate"),
                    effort=command.get("effort", "medium"),
                    index_ids=requested,
                ),
            )
            previous = None
            while True:
                authorized = self.source.store.authorized_snapshot(agent)
                if cancel.is_set() or not authorized or not self.status().get("enabled"):
                    await runtime.cancel(principal, run["id"])
                run = await runtime.get(principal, run["id"])
                stamp = (
                    run["phase"],
                    run["metrics"]["model_calls"],
                    run["metrics"]["source_calls"],
                    run["status"],
                )
                if stamp != previous:
                    progress(run["phase"], run["metrics"])
                    previous = stamp
                if run["status"] not in ACTIVE:
                    break
                await asyncio.sleep(0.1)
            self.source.check(principal)
            citations = self.source.citations[principal.owner_id]
            evidence = []
            for item in run["evidence"]:
                details = dict(citations.get((item["index_id"], item["id"]), {}))
                full_chars = details.pop("source_excerpt_chars", 0)
                details["truncated"] = (
                    bool(details.get("truncated")) or len(item["text"]) < full_chars
                )
                evidence.append({**item, **details})
            return {
                "action": "research",
                "version_scope": "latest",
                "answer": run["answer"],
                "status": run["status"],
                "citations": evidence,
                "warnings": run["warnings"],
                "metrics": run["metrics"],
                "coverage": {
                    "full_corpus": False,
                    "evidence_count": len(evidence),
                    "index_ids": run["index_ids"],
                    "note": "Исследованы найденные материалы. Полный экспорт базы не выполнялся.",
                },
            }
        finally:
            if run is not None and run["status"] in ACTIVE:
                await runtime.cancel(principal, run["id"])
            self.source.grants.pop(principal.owner_id, None)
            self.source.citations.pop(principal.owner_id, None)

    def stop(self) -> None:
        self.controller.stop()
        with self._lock:
            self._stopped = True
            if self._loop is not None:
                assert self._runtime is not None
                asyncio.run_coroutine_threadsafe(self._runtime.stop(), self._loop).result(
                    timeout=30
                )
                self._loop.call_soon_threadsafe(self._loop.stop)
                if self._thread is not None:
                    self._thread.join(timeout=5)
                self._loop.close()
                self._loop = None
