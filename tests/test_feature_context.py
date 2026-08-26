from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from corporate_kb.feature_context import FeatureContextPlanner
from service_map.models import (
    ServiceDependency,
    ServiceMapEvidence,
    ServiceMapSnapshot,
    ServiceRecord,
)


class FakeTools:
    def __init__(self, repository_id: str, service_id: str) -> None:
        self.repository_id = repository_id
        self.service_id = service_id

    def list_documents(self, *, limit: int = 50, **_filters: Any) -> dict[str, Any]:
        assert limit == 200
        return {
            "documents": [
                {
                    "document_id": f"doc-{self.service_id}",
                    "source_path": (
                        f"repositories/{self.repository_id}/openspec/current.md"
                    ),
                    "metadata": {"service": self.service_id},
                }
            ]
        }

    def search(self, *, query: str, top_k: int = 3, **_filters: Any) -> dict[str, Any]:
        return {
            "query": query,
            "result_count": 1,
            "results": [
                {
                    "document_id": f"doc-{self.service_id}",
                    "chunk_id": f"chunk-{self.service_id}",
                    "title": f"{self.service_id} SSOT",
                    "source_path": (
                        f"repositories/{self.repository_id}/openspec/current.md"
                    ),
                    "citation": f"{self.service_id} SSOT",
                    "excerpt": f"Rules owned by {self.service_id}",
                    "score": 0.9,
                }
            ][:top_k],
        }


class FakeCatalog:
    def __init__(self) -> None:
        self.snapshot = ServiceMapSnapshot(
            generated_at=datetime(2026, 8, 20, tzinfo=UTC),
            services=[
                ServiceRecord(
                    id="orders",
                    name="Orders",
                    aliases=["orders-service"],
                    repository="Orders repository",
                    repository_path="/repos/orders",
                    repository_root="/repos/orders",
                    source_url="https://example.test/orders.git",
                ),
                ServiceRecord(
                    id="inventory",
                    name="Inventory",
                    aliases=["inventory-service"],
                    repository="Inventory repository",
                    repository_path="/repos/inventory",
                    repository_root="/repos/inventory",
                    source_url="https://example.test/inventory.git",
                ),
            ],
            dependencies=[
                ServiceDependency(
                    id="dependency:orders:inventory",
                    source_service_id="orders",
                    target_service_id="inventory",
                    target_hint="inventory-service",
                    protocol="HTTP",
                    operation="GET /stock/{sku}",
                    confidence="HIGH",
                    resolved=True,
                    evidence_ids=["evidence:call"],
                ),
                ServiceDependency(
                    id="dependency:orders:audit",
                    source_service_id="orders",
                    target_hint="audit-api",
                    protocol="HTTP",
                    operation="POST /audit",
                    confidence="UNRESOLVED",
                    resolved=False,
                    evidence_ids=["evidence:audit"],
                ),
            ],
            evidence=[
                ServiceMapEvidence(
                    id="evidence:call",
                    repository="Orders repository",
                    file="src/main/java/OrderController.java",
                    line=42,
                    snippet="inventoryClient.stock(sku)",
                    extractor="tree-sitter-java",
                ),
                ServiceMapEvidence(
                    id="evidence:trigger",
                    repository="Orders repository",
                    file="src/main/java/OrderController.java",
                    line=30,
                    snippet='@PostMapping("/orders")',
                    extractor="tree-sitter-java",
                ),
            ],
        )
        self._tools = {
            "orders-index": FakeTools("orders-repo", "orders"),
            "inventory-index": FakeTools("inventory-repo", "inventory"),
        }

    def service_map(self) -> dict[str, Any]:
        return self.snapshot.model_dump(mode="json")

    def payload(self) -> dict[str, Any]:
        return {
            "repositories": [
                {
                    "id": "orders-repo",
                    "name": "Orders repository",
                    "checkout_path": "/repos/orders",
                    "index_id": "orders-index",
                },
                {
                    "id": "inventory-repo",
                    "name": "Inventory repository",
                    "checkout_path": "/repos/inventory",
                    "index_id": "inventory-index",
                },
            ],
            "indexes": [
                {"id": "orders-index", "name": "Orders knowledge"},
                {"id": "inventory-index", "name": "Inventory knowledge"},
            ],
        }

    def graph_search(self, query: str, *, limit: int) -> dict[str, Any]:
        return {"query": query, "limit": limit, "results": []}

    def graph_business_operations(self, service: str, *, limit: int) -> dict[str, Any]:
        if service != "orders":
            return {"operations": [], "related_nodes": [], "edges": []}
        return {
            "operations": [
                {
                    "id": "operation:orders:create",
                    "type": "BusinessOperation",
                    "label": "Create order",
                    "metadata": {
                        "trigger_type": "HTTP",
                        "trigger": "POST /orders",
                        "handler": "OrderController#create",
                    },
                    "evidence_ids": ["evidence:trigger"],
                }
            ],
            "related_nodes": [
                {
                    "id": "exitpoint:orders:stock",
                    "type": "ExitPoint",
                    "label": "HTTP GET /stock/{sku}",
                    "metadata": {"operation": "GET /stock/{sku}"},
                    "evidence_ids": ["evidence:call"],
                }
            ],
            "edges": [
                {
                    "source": "operation:orders:create",
                    "target": "exitpoint:orders:stock",
                    "type": "EXITS_VIA",
                    "evidence_ids": ["evidence:call"],
                }
            ],
        }

    def tools_for(self, index_id: str) -> Any:
        return self._tools[index_id]


def test_feature_context_joins_calls_triggers_and_service_rag_routes() -> None:
    result = FeatureContextPlanner(FakeCatalog()).build(
        feature="Create an order and reserve stock",
        start_service="orders-service",
        max_hops=1,
        top_k_per_service=1,
    )

    assert result["status"] == "ready"
    assert result["root_services"] == ["orders"]
    assert {item["id"] for item in result["services"]} == {"orders", "inventory"}
    routes = {item["id"]: item["rag"] for item in result["services"]}
    assert routes["orders"]["index_id"] == "orders-index"
    assert routes["inventory"]["index_id"] == "inventory-index"
    assert routes["orders"]["results"][0]["service_id"] == "orders"

    resolved = next(item for item in result["calls"] if item["resolved"])
    assert resolved["caller_service_id"] == "orders"
    assert resolved["callee_service_id"] == "inventory"
    assert resolved["api"] == {"protocol": "HTTP", "operation": "GET /stock/{sku}"}
    invocation = resolved["invocation_contexts"][0]
    assert invocation["trigger"] == "POST /orders"
    assert invocation["handler"] == "OrderController#create"
    assert {item["id"] for item in result["evidence"]} == {
        "evidence:call",
        "evidence:trigger",
    }
    assert result["unresolved_targets"][0]["target_hint"] == "audit-api"


def test_feature_context_requires_explicit_service_when_discovery_has_no_evidence() -> None:
    catalog = FakeCatalog()
    catalog._tools = {}

    result = FeatureContextPlanner(catalog).build(feature="completely unrelated capability")

    assert result["status"] == "needs_service"
    assert {item["id"] for item in result["candidate_services"]} == {
        "orders",
        "inventory",
    }


def test_system_graph_route_never_queries_rag_and_returns_explicit_next_calls() -> None:
    catalog = FakeCatalog()
    catalog._tools = {}

    result = FeatureContextPlanner(catalog).graph_route(
        feature="Create an order and reserve stock",
        start_service="orders-service",
        max_hops=1,
    )

    assert result["status"] == "ready"
    assert result["rag_queried"] is False
    assert {item["id"] for item in result["services"]} == {"orders", "inventory"}
    assert all("rag" not in item for item in result["services"])
    assert {item["arguments"]["index_id"] for item in result["next_calls"]} == {
        "orders-index",
        "inventory-index",
    }
    assert {item["tool"] for item in result["next_calls"]} == {"kb_search_index"}
