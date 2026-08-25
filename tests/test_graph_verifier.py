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
            )
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
