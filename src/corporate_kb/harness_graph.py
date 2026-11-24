"""Read the portal's published graph through its existing catalog queries."""

from __future__ import annotations

import json
from typing import Any

from agent_harness.contracts import Evidence, ServiceInventorySnapshot
from agent_harness.research import service_inventory_question, simple_service_count_question
from corporate_kb.catalog import RagCatalog

GRAPH_INDEX = "graph:system"
INVENTORY = "graph:inventory"
SERVICE_DOCUMENT = "graph:service/"
EVIDENCE_DOCUMENT = "graph:evidence/"


def _inventory(overview: dict[str, Any]) -> Evidence | None:
    services = overview["services"]
    count = overview["nodes_by_type"].get("Service", 0)
    # RagCatalog creates a placeholder before the first scan. Repository-only
    # older data without a published revision also cannot attest to a zero count.
    if not count and not overview.get("snapshot_id"):
        return None
    if (type(count) is not int or not isinstance(services, list) or len(services) != count
            or len({s["id"] for s in services}) != count):
        return None
    fact = ServiceInventorySnapshot(
        service_count=count, generated_at=overview["generated_at"],
        analysis_mode=overview["analysis_mode"], snapshot_id=overview.get("snapshot_id"),
    )
    return Evidence(
        id=INVENTORY, document_id=INVENTORY, index_id=GRAPH_INDEX,
        revision=fact.snapshot_id or fact.generated_at, title="Реестр сервисов в снимке графа",
        text=fact.model_dump_json(), service_inventory=fact,
    )


def _service(catalog: RagCatalog, identity: str) -> Evidence:
    graph = catalog.graph(view="services", service=identity, depth=1, limit=200)
    focus = graph["focus"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    node = nodes.get(focus)
    if node is None or node["type"] != "Service":
        raise LookupError("Сервис отсутствует в выбранной части снимка")
    edges = [edge for edge in graph["edges"] if focus in (edge["source"], edge["target"])]
    payload = {
        "service": node["label"], "service_id": node.get("service_id"),
        "generated_at": graph["generated_at"], "analysis_mode": graph["analysis_mode"],
        "dependencies": [
            {"from": nodes[edge["source"]]["label"], "to": nodes[edge["target"]]["label"],
             "target_type": nodes[edge["target"]]["type"], "confidence": edge["confidence"],
             "status": edge["status"], "protocol": edge["metadata"].get("protocol"),
             "evidence_document_ids": [EVIDENCE_DOCUMENT + i for i in edge["evidence_ids"][:3]]}
            for edge in edges[:50]
        ],
        "truncated": graph["truncated"] or len(edges) > 50,
    }
    document = SERVICE_DOCUMENT + focus
    return Evidence(
        id=document, document_id=document, index_id=GRAPH_INDEX,
        revision=graph.get("snapshot_id") or graph["generated_at"],
        title="Связи сервиса " + node["label"], text=json.dumps(payload, ensure_ascii=False),
    )


def read_graph(catalog: RagCatalog, document_id: str) -> Evidence:
    if document_id == INVENTORY:
        fact = _inventory(catalog.graph_overview())
        if fact is None:
            raise LookupError("Граф пока не содержит подтверждённого реестра сервисов")
        return fact
    if document_id.startswith(SERVICE_DOCUMENT):
        return _service(catalog, document_id.removeprefix(SERVICE_DOCUMENT))
    if document_id.startswith(EVIDENCE_DOCUMENT):
        identity = document_id.removeprefix(EVIDENCE_DOCUMENT)
        result = catalog.graph_evidence([identity])
        if len(result["items"]) != 1:
            raise KeyError("Graph evidence is absent from the current snapshot")
        item = result["items"][0]
        return Evidence(
            id=document_id, document_id=document_id, index_id=GRAPH_INDEX,
            title=f"{item['repository']} · {item['file']}:{item['line']}",
            revision=item["commit"], text=json.dumps(item, ensure_ascii=False),
        )
    raise KeyError("Unknown graph document")


def search_graph(catalog: RagCatalog, query: str, limit: int) -> list[Evidence]:
    overview = catalog.graph_overview()
    results: list[Evidence] = []
    count_question = simple_service_count_question(query)
    inventory_question = count_question or service_inventory_question(query)
    if inventory_question and (inventory := _inventory(overview)) is not None:
        results.append(inventory)
    if count_question:
        return results
    if len(results) >= limit or not overview["services"]:
        return results
    # Project a matched endpoint, class or event back to its owning service.
    hits = catalog.graph_search(query, limit=min(200, max(20, 3 * limit)))["results"]
    by_id = {s["service_id"]: s["id"] for s in overview["services"] if s.get("service_id")}
    identities = dict.fromkeys(
        hit["id"] if hit["type"] == "Service" else by_id.get(hit.get("service_id"))
        for hit in hits
    )
    if inventory_question:
        # An inventory query can nominate services for a later detail read. Never
        # traverse every dependency neighbourhood just to answer a count.
        services = {s["id"]: s for s in overview["services"]}
        identities.update(dict.fromkeys(services))
        for identity in identities:
            if identity is None or identity not in services:
                continue
            service = services[identity]
            document = SERVICE_DOCUMENT + identity
            results.append(Evidence(
                id=document, document_id=document, index_id=GRAPH_INDEX,
                title="Сервис " + service["label"], text=json.dumps(
                    {"service": service["label"], "service_id": service["service_id"],
                     "generated_at": overview["generated_at"]}, ensure_ascii=False,
                ),
                revision=overview.get("snapshot_id") or overview["generated_at"],
            ))
            if len(results) >= limit:
                break
        return results
    for identity in identities:
        if identity:
            results.append(_service(catalog, identity))
            if len(results) >= limit:
                break
    return results
