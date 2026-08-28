from __future__ import annotations

from pathlib import Path
from typing import Any

from corporate_kb.gigacode_runner import GigaCodeJsonResult
from corporate_kb.graph_verifier import GraphGigaCodeVerifier
from gigacode_graph.config import GraphSettings
from gigacode_graph.models import Evidence, GraphEdge, GraphNode, GraphSnapshot
from service_map import RepositoryInput, ServiceMapBuilder, ServiceMapBuildResult


class FakeGigaCodeRunner:
    def run_json(self, **_arguments: Any) -> GigaCodeJsonResult:
        return GigaCodeJsonResult(
            payload={
                "edge_updates": [
                    {
                        "candidate_id": "dependency:orders:payment",
                        "decision": "retarget",
                        "target_service_id": "payment",
                        "confidence": "HIGH",
                        "reason": "The configured client URL matches PaymentController.",
                        "evidence": [
                            {
                                "file": "src/PaymentController.kt",
                                "line": 2,
                                "symbol": "PaymentController.cancel",
                            }
                        ],
                    }
                ],
                "analyzed_files": ["src/PaymentController.kt"],
                "warnings": [],
            },
            analyzed_files=("src/PaymentController.kt",),
            session_id="verify-session",
            model="fake-model",
            duration_ms=12,
            usage={"total_tokens": 100},
        )


class FakeDiscoveryRunner:
    def run_json(self, **_arguments: Any) -> GigaCodeJsonResult:
        return GigaCodeJsonResult(
            payload={
                "edge_updates": [],
                "discovered_edges": [
                    {
                        "source_service_id": "orders",
                        "target_service_id": "payment",
                        "target_entrypoint_id": "entrypoint:payment:refund",
                        "protocol": "HTTP",
                        "operation": "POST /payments/refund",
                        "confidence": "HIGH",
                        "reason": "Custom client wrapper posts to the refund endpoint.",
                        "evidence": [
                            {
                                "file": "src/CustomClient.kt",
                                "line": 1,
                                "symbol": "CustomClient.refund",
                            }
                        ],
                    }
                ],
                "analyzed_files": ["src/CustomClient.kt"],
                "warnings": [],
            },
            analyzed_files=("src/CustomClient.kt",),
            session_id="discovery-session",
            model="fake-model",
            duration_ms=8,
            usage={"total_tokens": 80},
        )


class InvalidPayloadRunner:
    def run_json(self, **_arguments: Any) -> GigaCodeJsonResult:
        return GigaCodeJsonResult(
            payload={
                "edge_updates": "not-an-array",
                "discovered_edges": [],
                "analyzed_files": [],
                "warnings": [],
            },
            analyzed_files=(),
            session_id="invalid-session",
            model="fake-model",
            duration_ms=3,
            usage={},
        )


def _build_result(repository: Path) -> tuple[ServiceMapBuildResult, RepositoryInput]:
    repository_input = RepositoryInput(
        path=repository,
        name="Payments repository",
        source_url="https://example.test/payments.git",
        commit="abc123",
    )
    service_metadata = {
        "path": str(repository.resolve()),
        "repository_path": str(repository.resolve()),
        "repository": "Payments repository",
        "module_path": ".",
        "module_state": "active",
        "build_system": "gradle",
    }
    graph = GraphSnapshot(
        nodes=[
            GraphNode(
                id="service:orders",
                type="Service",
                label="Orders",
                service_id="orders",
                metadata={**service_metadata, "aliases": ["orders-service"]},
            ),
            GraphNode(
                id="service:payment",
                type="Service",
                label="Payment",
                service_id="payment",
                metadata={**service_metadata, "aliases": ["payment-service"]},
            ),
            GraphNode(
                id="external:payment-api",
                type="ExternalSystem",
                label="${payment.url}",
                metadata={"target_hint": "${payment.url}"},
            ),
            GraphNode(
                id="entrypoint:payment:refund",
                type="EntryPoint",
                label="HTTP POST /payments/refund",
                service_id="payment",
                metadata={"trigger_type": "HTTP", "operation": "POST /payments/refund"},
                evidence_ids=["target:refund:evidence"],
            ),
        ],
        edges=[
            GraphEdge(
                id="dependency:orders:payment",
                source="service:orders",
                target="external:payment-api",
                type="DEPENDS_ON",
                confidence="UNRESOLVED",
                status="unresolved",
                metadata={"protocol": "HTTP", "operation": "POST /payments/cancel"},
                evidence_ids=["static:evidence"],
            )
        ],
        evidence=[
            Evidence(
                id="static:evidence",
                repository="Payments repository",
                commit="abc123",
                file="src/OrdersClient.kt",
                line=1,
                snippet="paymentClient.cancel()",
                extractor="tree-sitter-kotlin",
                confidence="LOW",
            ),
            Evidence(
                id="target:refund:evidence",
                repository="Payments repository",
                commit="abc123",
                file="src/PaymentController.kt",
                line=2,
                snippet="fun refund() = Unit",
                extractor="tree-sitter-kotlin",
                confidence="HIGH",
            ),
        ],
    )
    result = ServiceMapBuilder(GraphSettings()).from_graph(graph, [repository_input])
    return result, repository_input


def test_graph_verifier_retargets_only_validated_candidates_and_records_artifact(
    tmp_path,
) -> None:
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "OrdersClient.kt").write_text(
        "paymentClient.cancel()\n",
        encoding="utf-8",
    )
    (repository / "src" / "PaymentController.kt").write_text(
        "class PaymentController {\n  fun cancel() = Unit\n}\n",
        encoding="utf-8",
    )
    result, repository_input = _build_result(repository)
    verifier = GraphGigaCodeVerifier(
        FakeGigaCodeRunner(),  # type: ignore[arg-type]
        GraphSettings(),
        tmp_path / "analysis",
    )

    verified, summary = verifier.verify(
        result,
        [repository_input],
        verify_all=True,
    )

    edge = next(item for item in verified.graph.edges if item.id == "dependency:orders:payment")
    assert edge.target == "service:payment"
    assert edge.confidence == "HIGH"
    assert edge.status == "confirmed"
    assert edge.origin == "static+gigacode"
    assert any(item.extractor == "gigacode-verifier" for item in verified.graph.evidence)
    dependency = next(
        item for item in verified.service_map.dependencies if item.id == edge.id
    )
    assert dependency.target_service_id == "payment"
    assert dependency.resolved is True
    assert summary["retargeted"] == 1
    assert Path(summary["artifact"]).is_file()


def test_graph_verifier_discovers_missing_edge_only_with_two_sided_evidence(tmp_path) -> None:
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "OrdersClient.kt").write_text(
        "paymentClient.cancel()\n",
        encoding="utf-8",
    )
    (repository / "src" / "CustomClient.kt").write_text(
        "customHttp.post(\"/payments/refund\")\n",
        encoding="utf-8",
    )
    (repository / "src" / "PaymentController.kt").write_text(
        "class PaymentController {\n  fun refund() = Unit\n}\n",
        encoding="utf-8",
    )
    result, repository_input = _build_result(repository)
    verifier = GraphGigaCodeVerifier(
        FakeDiscoveryRunner(),  # type: ignore[arg-type]
        GraphSettings(),
        tmp_path / "analysis",
    )

    verified, summary = verifier.verify(result, [repository_input], verify_all=True)

    discovered = next(
        edge
        for edge in verified.graph.edges
        if edge.type == "DEPENDS_ON"
        and edge.source == "service:orders"
        and edge.target == "service:payment"
        and edge.metadata.get("matcher") == "gigacode-discovery"
    )
    assert discovered.status == "confirmed"
    assert discovered.origin == "gigacode"
    assert len(discovered.evidence_ids) == 2
    assert summary["discovered"] == 1


def test_graph_verifier_preserves_static_graph_when_gigacode_validation_fails(
    tmp_path,
) -> None:
    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "OrdersClient.kt").write_text(
        "paymentClient.cancel()\n",
        encoding="utf-8",
    )
    result, repository_input = _build_result(repository)
    progress: list[str] = []
    verifier = GraphGigaCodeVerifier(
        InvalidPayloadRunner(),  # type: ignore[arg-type]
        GraphSettings(),
        tmp_path / "analysis",
    )

    verified, summary = verifier.verify(
        result,
        [repository_input],
        verify_all=True,
        progress=progress.append,
    )

    edge = next(item for item in verified.graph.edges if item.id == "dependency:orders:payment")
    assert edge.target == "external:payment-api"
    assert edge.origin == "static"
    assert summary["processed"] == 1
    assert summary["failed"] == 1
    assert summary["unresolved"] == 1
    assert summary["fallback"] == "static-graph"
    assert summary["runs"][0]["status"] == "failed"
    assert "ValidationError" in summary["warnings"][0]
    assert Path(summary["artifact"]).is_file()
    assert any("static_graph_preserved=true" in message for message in progress)
