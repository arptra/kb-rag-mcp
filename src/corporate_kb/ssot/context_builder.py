"""Build one compact cross-service context from the current service SSOT index."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Literal

from corporate_kb.context import ContextCompressor
from corporate_kb.models import SearchFilters, SearchResult
from corporate_kb.service import KnowledgeService

SsotMode = Literal["business", "implementation"]

_SERVICE_ID = re.compile(
    r"(?<![\w-])([a-z][a-z0-9_.]*?(?:-[a-z0-9_.]+)*?-(?:service|svc))(?![\w-])"
)
_SPACE = re.compile(r"\s+")
_MODE_HINTS: dict[SsotMode, str] = {
    "business": (
        "назначение ответственность владелец бизнес правило процесс ограничение зависимость "
        "purpose responsibility owner business rule process constraint dependency"
    ),
    "implementation": (
        "ответственность интеграция зависимость API событие команда данные ограничение "
        "responsibility integration dependency API event command data constraint"
    ),
}


class SsotContextBuilder:
    """Run two internal retrieval stages and expose only the final evidence brief."""

    def __init__(self, service: KnowledgeService) -> None:
        self._service = service
        self._compressor = ContextCompressor()

    def build(self, *, question: str, mode: str = "implementation") -> dict[str, Any]:
        if not question.strip():
            raise ValueError("question must not be empty")
        if mode not in _MODE_HINTS:
            raise ValueError("mode must be business or implementation")
        active_mode: SsotMode = "business" if mode == "business" else "implementation"

        settings = self._service.settings
        filters = SearchFilters(
            document_type=settings.ssot_document_type,
            status="current",
        )
        seeds = self._service.search(
            question,
            top_k=settings.ssot_candidate_k,
            filters=filters,
        )
        services = self._discover_services(question, seeds)[: settings.ssot_max_services]
        if not services:
            return {
                "mode": mode,
                "service_count": 0,
                "services": [],
                "connections": [],
                "context_token_count": 0,
                "missing_information": [
                    "No current SSOT service matched the question. Check SSOT metadata and index."
                ],
            }

        expanded_query = f"{question}\n{_MODE_HINTS[active_mode]}"
        evidence_by_service: dict[str, list[SearchResult]] = {}
        for service_id in services:
            evidence_by_service[service_id] = self._service.search(
                expanded_query,
                top_k=min(20, max(settings.ssot_facts_per_service * 3, 6)),
                filters=SearchFilters(
                    service=service_id,
                    document_type=settings.ssot_document_type,
                    status="current",
                ),
            )

        payload = self._assemble(
            question=question,
            mode=active_mode,
            services=services,
            evidence_by_service=evidence_by_service,
        )
        return self._fit_budget(payload, settings.ssot_context_tokens)

    def _discover_services(self, question: str, seeds: list[SearchResult]) -> list[str]:
        scores: dict[str, float] = defaultdict(float)
        first_seen: dict[str, int] = {}

        def add(service_id: str, score: float) -> None:
            normalized = service_id.strip().casefold()
            if not normalized:
                return
            first_seen.setdefault(normalized, len(first_seen))
            scores[normalized] = max(scores[normalized], score)

        for service_id in _SERVICE_ID.findall(question.casefold()):
            add(service_id, 2.0)
        for result in seeds:
            owner = result.metadata.get("service")
            if isinstance(owner, str):
                add(owner, 1.0 + result.score)
            for service_id in _SERVICE_ID.findall(result.text.casefold()):
                # A referenced service is part of the cross-service context even when its own
                # SSOT chunk was not among the initial vector-search hits.
                add(service_id, 0.5 + result.score)

        return sorted(scores, key=lambda item: (-scores[item], first_seen[item], item))

    def _assemble(
        self,
        *,
        question: str,
        mode: SsotMode,
        services: list[str],
        evidence_by_service: dict[str, list[SearchResult]],
    ) -> dict[str, Any]:
        settings = self._service.settings
        groups: list[dict[str, Any]] = []
        connections: list[dict[str, str]] = []
        known_services = set(services)
        seen_connections: set[tuple[str, str, str]] = set()

        for service_id in services:
            facts: list[dict[str, Any]] = []
            seen_facts: set[str] = set()
            for result in evidence_by_service.get(service_id, []):
                if len(facts) >= settings.ssot_facts_per_service:
                    break
                excerpt = self._compressor.excerpt(
                    query=question,
                    text=result.text,
                    max_tokens=settings.ssot_fact_tokens,
                )
                canonical = _SPACE.sub(" ", excerpt.text).strip().casefold()
                if not canonical or canonical in seen_facts:
                    continue
                seen_facts.add(canonical)
                revision = self._revision(result)
                fact: dict[str, Any] = {
                    "fact": excerpt.text,
                    "section": result.heading_path,
                    "evidence_id": result.chunk_id,
                    "source_path": result.source_path,
                }
                if revision is not None:
                    fact["revision"] = revision
                facts.append(fact)

                mentioned = known_services & set(_SERVICE_ID.findall(result.text.casefold()))
                for target in sorted(mentioned - {service_id}):
                    key = (service_id, target, result.chunk_id)
                    if key in seen_connections:
                        continue
                    seen_connections.add(key)
                    connections.append(
                        {
                            "from": service_id,
                            "mentions": target,
                            "evidence_id": result.chunk_id,
                        }
                    )
            groups.append({"service": service_id, "facts": facts})

        missing = [
            f"No relevant current SSOT evidence was found for {group['service']}."
            for group in groups
            if not group["facts"]
        ]
        return {
            "mode": mode,
            "service_count": len(groups),
            "services": groups,
            "connections": connections,
            "missing_information": missing,
            "context_token_count": 0,
        }

    def _fit_budget(self, payload: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        """Remove lowest-priority evidence until the entire JSON payload fits the budget."""
        while self._payload_tokens(payload) > max_tokens:
            groups = payload["services"]
            removable = next(
                (group for group in reversed(groups) if len(group["facts"]) > 1),
                None,
            )
            if removable is not None:
                removable["facts"].pop()
                continue
            if payload["connections"]:
                payload["connections"].pop()
                continue
            if len(groups) > 1:
                removed = groups.pop()
                payload["missing_information"].append(
                    f"Evidence for {removed['service']} was omitted by the context budget."
                )
                payload["service_count"] = len(groups)
                continue
            break
        payload["context_token_count"] = self._payload_tokens(payload)
        return payload

    def _payload_tokens(self, payload: dict[str, Any]) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return self._compressor.count_tokens(encoded)

    @staticmethod
    def _revision(result: SearchResult) -> str | None:
        for key in ("commit_sha", "git_commit", "revision", "version"):
            value = result.metadata.get(key)
            if value not in (None, ""):
                return str(value)
        return None
