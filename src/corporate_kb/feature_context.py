"""Join the static service graph with repository-scoped RAG context for feature work."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol

from corporate_kb.mcp.tools import KnowledgeTools
from service_map.models import ServiceDependency, ServiceMapSnapshot, ServiceRecord

_MAX_SELECTED_SERVICES = 12
_MAX_EVIDENCE = 100
_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_\u0400-\u04ff-]+")


class FeatureContextCatalog(Protocol):
    """Small catalog surface needed by the feature-context planner."""

    def payload(self) -> dict[str, Any]: ...

    def service_map(self) -> dict[str, Any]: ...

    def graph_search(self, query: str, *, limit: int) -> dict[str, Any]: ...

    def graph_business_operations(self, service: str, *, limit: int) -> dict[str, Any]: ...

    def tools_for(self, index_id: str) -> KnowledgeTools: ...


class FeatureContextPlanner:
    """Build one evidence-backed implementation context across graph and RAG indexes."""

    def __init__(self, catalog: FeatureContextCatalog) -> None:
        self._catalog = catalog

    def build(
        self,
        *,
        feature: str,
        start_service: str | None = None,
        max_hops: int = 2,
        top_k_per_service: int = 2,
    ) -> dict[str, Any]:
        feature = feature.strip()
        if not feature:
            raise ValueError("feature must not be empty")
        if not 0 <= max_hops <= 4:
            raise ValueError("max_hops must be between 0 and 4")
        if not 1 <= top_k_per_service <= 5:
            raise ValueError("top_k_per_service must be between 1 and 5")

        snapshot = ServiceMapSnapshot.model_validate(self._catalog.service_map())
        services = {item.id: item for item in snapshot.services}
        if not services:
            return self._empty_payload(feature, snapshot)

        catalog_payload = self._catalog.payload()
        routes = self._routes(snapshot, catalog_payload)
        warnings: list[str] = []
        roots, discovery = self._resolve_roots(
            feature,
            start_service,
            snapshot,
            routes,
            warnings,
        )
        if not roots:
            return {
                **self._empty_payload(feature, snapshot),
                "status": "needs_service",
                "discovery": discovery,
                "candidate_services": self._candidate_services(snapshot),
                "warnings": [
                    *warnings,
                    "The graph and indexed documents could not identify a starting service. "
                    "Call the tool again with start_service from candidate_services."
                ],
            }

        selected_ids, distances = self._neighbourhood(snapshot, roots, max_hops)
        selected_ids = set(
            sorted(selected_ids, key=lambda item: (distances[item], item))[
                :_MAX_SELECTED_SERVICES
            ]
        )
        calls = self._calls(snapshot, selected_ids, warnings)
        evidence_ids = {
            evidence_id
            for call in calls
            for evidence_id in call.get("evidence_ids", [])
            if isinstance(evidence_id, str)
        }

        service_contexts = []
        root_ids = {item.id for item in roots}
        for service_id in sorted(selected_ids, key=lambda item: (distances[item], item)):
            service = services[service_id]
            route = routes.get(service_id)
            context = self._service_context(
                feature=feature,
                service=service,
                route=route,
                top_k=top_k_per_service,
                depth=distances[service_id],
                is_root=service_id in root_ids,
                warnings=warnings,
            )
            service_contexts.append(context)
            for result in context["rag"]["results"]:
                if isinstance(result, dict):
                    result["service_id"] = service.id

        evidence = [
            item.model_dump(mode="json")
            for item in snapshot.evidence
            if item.id in evidence_ids
        ][:_MAX_EVIDENCE]
        unresolved = [call for call in calls if not call["resolved"]]
        return {
            "status": "ready",
            "feature": feature,
            "analysis_generated_at": snapshot.generated_at.isoformat(),
            "discovery": discovery,
            "root_services": [item.id for item in roots],
            "service_count": len(service_contexts),
            "call_count": len(calls),
            "services": service_contexts,
            "calls": calls,
            "unresolved_targets": unresolved,
            "evidence": evidence,
            "warnings": list(dict.fromkeys(warnings)),
            "agent_guidance": [
                "Use each service.rag.index_id and the returned citations as the documentation "
                "boundary for that service.",
                "Treat calls with LOW or UNRESOLVED confidence as hypotheses and verify their "
                "evidence before editing code.",
                "The invocation_contexts describe statically linked triggers, not observed runtime "
                "timing or a guaranteed distributed trace.",
            ],
        }

    @staticmethod
    def _empty_payload(feature: str, snapshot: ServiceMapSnapshot) -> dict[str, Any]:
        return {
            "status": "empty_graph",
            "feature": feature,
            "analysis_generated_at": snapshot.generated_at.isoformat(),
            "discovery": {"method": "none", "matches": []},
            "root_services": [],
            "service_count": 0,
            "call_count": 0,
            "services": [],
            "calls": [],
            "unresolved_targets": [],
            "evidence": [],
            "warnings": [
                "The service map is empty. Import repositories and complete repository analysis "
                "before requesting feature context."
            ],
            "agent_guidance": [],
        }

    def _resolve_roots(
        self,
        feature: str,
        start_service: str | None,
        snapshot: ServiceMapSnapshot,
        routes: Mapping[str, dict[str, Any]],
        warnings: list[str],
    ) -> tuple[list[ServiceRecord], dict[str, Any]]:
        if start_service and start_service.strip():
            resolved = self._resolve_service(start_service, snapshot)
            return [resolved], {
                "method": "explicit_start_service",
                "matches": [{"service_id": resolved.id, "score": 1.0}],
            }

        scores: dict[str, float] = {}
        graph_matches: list[dict[str, Any]] = []
        try:
            result = self._catalog.graph_search(feature, limit=50)
            for item in result.get("results", []):
                if not isinstance(item, dict):
                    continue
                service_id = item.get("service_id")
                if not isinstance(service_id, str) or not service_id:
                    continue
                score = float(item.get("score", 0.0))
                scores[service_id] = max(scores.get(service_id, 0.0), score + 3.0)
                graph_matches.append(
                    {
                        "service_id": service_id,
                        "node_id": item.get("id"),
                        "node_type": item.get("type"),
                        "label": item.get("label"),
                        "score": score,
                    }
                )
        except Exception as exc:
            warnings.append(f"Graph search failed: {type(exc).__name__}: {exc}")

        feature_tokens = _tokens(feature)
        for service in snapshot.services:
            lexical = self._service_score(service, snapshot.dependencies, feature_tokens)
            if lexical > 0:
                scores[service.id] = scores.get(service.id, 0.0) + lexical

        method = "graph_and_interface_search"
        if not scores:
            rag_scores = self._discover_from_rag(feature, snapshot, routes, warnings)
            scores.update(rag_scores)
            method = "rag_document_discovery"

        ranked = [
            item
            for item in sorted(
                snapshot.services,
                key=lambda service: (-scores.get(service.id, 0.0), service.id),
            )
            if scores.get(item.id, 0.0) > 0 and item.module_state == "active"
        ]
        if not ranked:
            return [], {"method": method, "matches": graph_matches[:20]}
        best = scores[ranked[0].id]
        roots = [item for item in ranked if scores[item.id] >= best * 0.7][:3]
        matches = [
            {"service_id": item.id, "score": round(scores[item.id], 4)} for item in roots
        ]
        return roots, {"method": method, "matches": matches, "graph_hits": graph_matches[:20]}

    def _discover_from_rag(
        self,
        feature: str,
        snapshot: ServiceMapSnapshot,
        routes: Mapping[str, dict[str, Any]],
        warnings: list[str],
    ) -> dict[str, float]:
        scores: dict[str, float] = {}
        searched_indexes: dict[str, list[dict[str, Any]]] = {}
        for route in routes.values():
            index_id = route["index_id"]
            if index_id in searched_indexes:
                continue
            try:
                result = self._catalog.tools_for(index_id).search(query=feature, top_k=10)
                searched_indexes[index_id] = [
                    item for item in result.get("results", []) if isinstance(item, dict)
                ]
            except Exception as exc:
                warnings.append(
                    f"RAG discovery failed for index {index_id}: {type(exc).__name__}: {exc}"
                )
                searched_indexes[index_id] = []

        for service in snapshot.services:
            service_route = routes.get(service.id)
            if service_route is None:
                continue
            for result in searched_indexes.get(service_route["index_id"], []):
                if self._document_belongs_to_service(result, service, service_route):
                    scores[service.id] = max(scores.get(service.id, 0.0), float(result["score"]))
        return scores

    @staticmethod
    def _resolve_service(value: str, snapshot: ServiceMapSnapshot) -> ServiceRecord:
        needle = value.strip().lower()
        matches = []
        for service in snapshot.services:
            identities = {service.id.lower(), service.name.lower()}
            identities.update(alias.lower() for alias in service.aliases)
            if needle in identities:
                matches.append(service)
        if not matches:
            raise KeyError(f"Unknown service: {value}")
        if len(matches) > 1:
            raise ValueError(f"Ambiguous service: {value}")
        return matches[0]

    @staticmethod
    def _service_score(
        service: ServiceRecord,
        dependencies: list[ServiceDependency],
        feature_tokens: set[str],
    ) -> float:
        if not feature_tokens:
            return 0.0
        identities = " ".join([service.id, service.name, *service.aliases])
        score = len(feature_tokens & _tokens(identities)) * 2.0
        interfaces = [*service.entrypoints, *service.outbound_interfaces]
        score += sum(
            len(feature_tokens & _tokens(f"{item.operation} {item.target_hint or ''}")) * 1.5
            for item in interfaces
        )
        for dependency in dependencies:
            if service.id not in {dependency.source_service_id, dependency.target_service_id}:
                continue
            score += len(
                feature_tokens
                & _tokens(f"{dependency.operation} {dependency.target_hint}")
            )
        return score

    @staticmethod
    def _neighbourhood(
        snapshot: ServiceMapSnapshot,
        roots: list[ServiceRecord],
        max_hops: int,
    ) -> tuple[set[str], dict[str, int]]:
        known = {item.id for item in snapshot.services}
        distances = {item.id: 0 for item in roots}
        queue = deque((item.id, 0) for item in roots)
        while queue:
            current, depth = queue.popleft()
            if depth >= max_hops:
                continue
            adjacent = []
            for dependency in snapshot.dependencies:
                if dependency.source_service_id == current and dependency.target_service_id:
                    adjacent.append(dependency.target_service_id)
                if dependency.target_service_id == current:
                    adjacent.append(dependency.source_service_id)
            for neighbour in adjacent:
                if neighbour not in known or neighbour in distances:
                    continue
                distances[neighbour] = depth + 1
                queue.append((neighbour, depth + 1))
        return set(distances), distances

    def _calls(
        self,
        snapshot: ServiceMapSnapshot,
        selected_ids: set[str],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        calls = []
        business_cache: dict[str, dict[str, Any]] = {}
        for dependency in snapshot.dependencies:
            if dependency.source_service_id not in selected_ids:
                continue
            if dependency.target_service_id and dependency.target_service_id not in selected_ids:
                continue
            source_id = dependency.source_service_id
            if source_id not in business_cache:
                try:
                    business_cache[source_id] = self._catalog.graph_business_operations(
                        source_id,
                        limit=500,
                    )
                except Exception as exc:
                    warnings.append(
                        f"Business-operation lookup failed for {source_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    business_cache[source_id] = {}
            contexts, context_evidence = self._invocation_contexts(
                dependency,
                business_cache[source_id],
            )
            calls.append(
                {
                    "id": dependency.id,
                    "caller_service_id": source_id,
                    "callee_service_id": dependency.target_service_id,
                    "target_hint": dependency.target_hint,
                    "resolved": dependency.resolved,
                    "api": {
                        "protocol": dependency.protocol,
                        "operation": dependency.operation,
                    },
                    "invocation_contexts": contexts,
                    "confidence": dependency.confidence,
                    "evidence_ids": list(
                        dict.fromkeys([*dependency.evidence_ids, *context_evidence])
                    ),
                }
            )
        return calls

    @staticmethod
    def _invocation_contexts(
        dependency: ServiceDependency,
        business: Mapping[str, Any],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        nodes = {
            item["id"]: item
            for item in business.get("related_nodes", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        nodes.update(
            {
                item["id"]: item
                for item in business.get("operations", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
        )
        dependency_evidence = set(dependency.evidence_ids)
        contexts: list[dict[str, Any]] = []
        evidence_ids: list[str] = []
        for edge in business.get("edges", []):
            if not isinstance(edge, dict) or edge.get("type") != "EXITS_VIA":
                continue
            operation = nodes.get(str(edge.get("source")))
            exitpoint = nodes.get(str(edge.get("target")))
            if not operation or operation.get("type") != "BusinessOperation" or not exitpoint:
                continue
            exit_metadata = exitpoint.get("metadata", {})
            exit_operation = str(
                exit_metadata.get("operation")
                or exit_metadata.get("topic")
                or exitpoint.get("label")
                or ""
            )
            edge_evidence = {
                item for item in edge.get("evidence_ids", []) if isinstance(item, str)
            }
            if not (
                dependency_evidence & edge_evidence
                or _same_operation(dependency.operation, exit_operation)
            ):
                continue
            metadata = operation.get("metadata", {})
            current_evidence = [
                item
                for item in [
                    *operation.get("evidence_ids", []),
                    *edge.get("evidence_ids", []),
                    *exitpoint.get("evidence_ids", []),
                ]
                if isinstance(item, str)
            ]
            evidence_ids.extend(current_evidence)
            contexts.append(
                {
                    "business_operation": operation.get("label"),
                    "trigger_type": metadata.get("trigger_type"),
                    "trigger": metadata.get("trigger"),
                    "handler": metadata.get("handler"),
                    "exit_operation": exit_operation,
                    "evidence_ids": list(dict.fromkeys(current_evidence)),
                }
            )
        if not contexts:
            contexts.append(
                {
                    "business_operation": None,
                    "trigger_type": None,
                    "trigger": None,
                    "handler": None,
                    "exit_operation": dependency.operation,
                    "note": (
                        "A static outbound call was found, but it was not linked to a specific "
                        "entrypoint. Runtime order is unknown."
                    ),
                    "evidence_ids": dependency.evidence_ids,
                }
            )
        return contexts[:10], list(dict.fromkeys(evidence_ids))

    def _service_context(
        self,
        *,
        feature: str,
        service: ServiceRecord,
        route: dict[str, Any] | None,
        top_k: int,
        depth: int,
        is_root: bool,
        warnings: list[str],
    ) -> dict[str, Any]:
        query = f"{feature} {service.name} {service.id}"
        rag: dict[str, Any] = {
            "queried": False,
            "index_id": route.get("index_id") if route else None,
            "index_name": route.get("index_name") if route else None,
            "repository_id": route.get("repository_id") if route else None,
            "query": query,
            "document_scope": "service and owning repository",
            "result_count": 0,
            "results": [],
        }
        if route is None:
            warnings.append(f"No RAG index is connected to service {service.id}")
        else:
            try:
                tools = self._catalog.tools_for(route["index_id"])
                documents = tools.list_documents(limit=200).get("documents", [])
                eligible_ids = {
                    item["document_id"]
                    for item in documents
                    if isinstance(item, dict)
                    and isinstance(item.get("document_id"), str)
                    and self._document_belongs_to_service(item, service, route)
                }
                result = tools.search(query=query, top_k=min(20, max(top_k * 4, top_k)))
                scoped = [
                    item
                    for item in result.get("results", [])
                    if isinstance(item, dict) and item.get("document_id") in eligible_ids
                ][:top_k]
                rag.update(
                    {
                        "queried": True,
                        "result_count": len(scoped),
                        "results": scoped,
                    }
                )
                if not eligible_ids:
                    warnings.append(
                        f"Index {route['index_id']} has no documents scoped to service "
                        f"{service.id} or repository {route['repository_id']}"
                    )
                elif not scoped:
                    warnings.append(
                        f"RAG returned no relevant scoped context for service {service.id}"
                    )
            except Exception as exc:
                warnings.append(
                    f"RAG query failed for service {service.id} in index "
                    f"{route['index_id']}: {type(exc).__name__}: {exc}"
                )

        return {
            "id": service.id,
            "name": service.name,
            "role": "root" if is_root else "dependency",
            "distance_from_root": depth,
            "repository": service.repository,
            "module_path": service.module_path,
            "module_state": service.module_state,
            "source_url": service.source_url,
            "commit": service.commit,
            "entrypoints": [item.model_dump(mode="json") for item in service.entrypoints],
            "outbound_interfaces": [
                item.model_dump(mode="json") for item in service.outbound_interfaces
            ],
            "rag": rag,
        }

    @staticmethod
    def _document_belongs_to_service(
        document: Mapping[str, Any],
        service: ServiceRecord,
        route: Mapping[str, Any],
    ) -> bool:
        path = str(document.get("source_path") or "").lower()
        repository_prefix = f"repositories/{route['repository_id']}/".lower()
        ssot_path = f"ssot/{_slug(service.id)}.md".lower()
        metadata = document.get("metadata")
        metadata_service = ""
        if isinstance(metadata, dict):
            metadata_service = str(metadata.get("service") or "").strip().lower()
        identities = {service.id.lower(), service.name.lower()}
        identities.update(alias.lower() for alias in service.aliases)
        if path == ssot_path or metadata_service in identities:
            return True
        # Root OpenSpec documents can describe every module in a multi-module repository. The
        # repository prefix still prevents leakage when several repositories share one index;
        # the service name in the query ranks module-specific documents inside that boundary.
        return path.startswith(repository_prefix)

    @staticmethod
    def _routes(
        snapshot: ServiceMapSnapshot,
        catalog_payload: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        repositories = [
            item for item in catalog_payload.get("repositories", []) if isinstance(item, dict)
        ]
        indexes = {
            item["id"]: item
            for item in catalog_payload.get("indexes", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        routes: dict[str, dict[str, Any]] = {}
        for service in snapshot.services:
            repository = next(
                (
                    item
                    for item in repositories
                    if service.repository_root
                    and str(item.get("checkout_path") or "") == service.repository_root
                ),
                None,
            )
            if repository is None:
                repository = next(
                    (item for item in repositories if item.get("name") == service.repository),
                    None,
                )
            if repository is None:
                continue
            index_id = str(repository.get("index_id") or "")
            index = indexes.get(index_id, {})
            routes[service.id] = {
                "repository_id": str(repository["id"]),
                "repository_name": str(repository.get("name") or service.repository),
                "index_id": index_id,
                "index_name": str(index.get("name") or index_id),
            }
        return routes

    @staticmethod
    def _candidate_services(snapshot: ServiceMapSnapshot) -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "name": item.name,
                "repository": item.repository,
                "module_path": item.module_path,
                "module_state": item.module_state,
            }
            for item in snapshot.services
            if item.module_state == "active"
        ][:_MAX_SELECTED_SERVICES]


def _tokens(value: str) -> set[str]:
    return {item.lower() for item in _TOKEN_PATTERN.findall(value) if len(item) > 1}


def _same_operation(left: str, right: str) -> bool:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return False
    return (
        left_tokens == right_tokens
        or left_tokens.issubset(right_tokens)
        or right_tokens.issubset(left_tokens)
    )


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:48] or "service"
