import json
from datetime import UTC, datetime
from threading import Event
from typing import Any

import httpx
import pytest
from test_harness_integration import ADMIN, TOKEN, _service

from agent_harness.contracts import Principal
from corporate_kb.catalog import RagCatalog
from corporate_kb.harness_graph import GRAPH_INDEX, INVENTORY
from corporate_kb.harness_integration import CatalogSource
from corporate_kb.mcp.http_server import create_http_app
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.usage import UsageTracker
from gigacode_graph.models import Evidence as GraphEvidence
from gigacode_graph.models import GraphEdge, GraphNode, GraphSnapshot
from gigacode_graph.store import JsonGraphStore


def published_graph() -> GraphSnapshot:
    return GraphSnapshot(
        generated_at=datetime(2024, 6, 3, tzinfo=UTC), snapshot_id="scan-71",
        nodes=[
            GraphNode(id="service:orders", type="Service", label="Orders", service_id="orders"),
            GraphNode(id="service:ledger", type="Service", label="Ledger", service_id="ledger"),
            GraphNode(id="bank", type="ExternalSystem", label="Bank"),
            GraphNode(id="repo", type="Repository", label="Monorepo"),
        ],
        edges=[
            GraphEdge(id="edge", type="DEPENDS_ON", source="service:orders",
                      target="service:ledger", status="confirmed", metadata={"protocol": "HTTP"},
                      evidence_ids=["code-line"]),
            GraphEdge(id="obsolete", type="DEPENDS_ON", source="service:orders", target="bank",
                      status="rejected"),
        ],
        evidence=[
            GraphEvidence(id="code-line", repository="orders", commit="a1b2", file="Ledger.java",
                          line=12, snippet="ledger.check(order);", extractor="java"),
        ],
    )


async def test_real_graph_search_read_reload_and_acl(settings_factory: Any) -> None:
    settings, service = _service(settings_factory)
    usage = UsageTracker()
    catalog = RagCatalog(settings, service, KnowledgeTools(service, usage=usage), usage)
    store = JsonGraphStore(settings.graph_store_path)
    owner = Principal("user")
    allowed = True

    async def access(_principal: Principal) -> bool:
        return True

    async def graph_access(_principal: Principal) -> bool:
        return allowed

    source = CatalogSource(catalog, access, graph_access)
    scope = {"index_id": GRAPH_INDEX, "principal": owner, "cancel": Event()}
    assert {i["id"] for i in await source.indexes(owner)} == {"default", GRAPH_INDEX}
    assert not await source.search(query="Сколько сервисов?", limit=3, **scope)
    with pytest.raises(LookupError):
        await source.read(document_id=INVENTORY, **scope)
    artifact_only = GraphSnapshot(nodes=[GraphNode(id="repo", type="Repository", label="Repo")])
    store.save(artifact_only)
    assert not await source.search(query="Сколько сервисов?", limit=3, **scope)
    artifact_only.snapshot_id = "explicit-zero"
    store.save(artifact_only)
    zero = await source.read(document_id=INVENTORY, **scope)
    assert zero.service_inventory and zero.service_inventory.service_count == 0
    graph = published_graph()
    store.save(graph)
    [inventory] = await source.search(query="Сколько сервисов?", limit=3, **scope)
    assert inventory.document_id == INVENTORY
    assert inventory.service_inventory and inventory.service_inventory.service_count == 2
    assert json.loads(inventory.text)["generated_at"].startswith("2024-06-03")
    assert inventory.revision == "scan-71"
    _, *services = await source.search(query="Перечисли сервисы", limit=3, **scope)
    assert {e.title for e in services} == {"Сервис Orders", "Сервис Ledger"}
    [match] = await source.search(query="От чего зависит Orders?", limit=1, **scope)
    details = await source.read(document_id=match.id, **scope)
    edges = json.loads(details.text)["dependencies"]
    assert [(e["from"], e["to"], e["protocol"], e["status"]) for e in edges] == [
        ("Orders", "Ledger", "HTTP", "confirmed"),
    ]
    [document_id] = edges[0]["evidence_document_ids"]
    proof = await source.read(document_id=document_id, **scope)
    assert proof.revision == "a1b2" and "Ledger.java:12" in proof.title
    assert json.loads(proof.text)["snippet"] == "ledger.check(order);"
    graph.analysis_mode = "partial"
    store.save(graph)
    partial = await source.read(document_id=INVENTORY, **scope)
    assert partial.service_inventory and partial.service_inventory.analysis_mode == "partial"
    allowed = False
    assert {i["id"] for i in await source.indexes(owner)} == {"default"}
    for operation in (
        source.read(document_id=INVENTORY, **scope), source.search(query="Orders", limit=1, **scope)
    ):
        with pytest.raises(PermissionError):
            await operation

    check_results = iter([True, False])

    async def revoke_during_read(_principal: Principal) -> bool:
        return next(check_results)

    source = CatalogSource(catalog, access, revoke_during_read)
    with pytest.raises(PermissionError):
        await source.read(document_id=INVENTORY, **scope)


async def test_host_uses_dashboard_graph_grant_for_inventory(settings_factory: Any) -> None:
    settings, service = _service(settings_factory, harness_enabled=True)
    JsonGraphStore(settings.graph_store_path).save(published_graph())
    app: Any = create_http_app(service, settings)
    reader = {"authorization": f"Bearer {TOKEN}"}
    admin = {"x-kb-admin-password": ADMIN}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost",
        ) as client,
    ):
        for headers, visible in [({**reader, **admin}, True), (reader, False), (admin, True)]:
            overview = await client.get("/admin/api/graph/overview", headers=headers)
            assert (overview.status_code == 200) is visible
            status = await client.get("/harness/api/status", headers=headers)
            assert status.status_code == 200
            assert (GRAPH_INDEX in {i["id"] for i in status.json()["indexes"]}) is visible
