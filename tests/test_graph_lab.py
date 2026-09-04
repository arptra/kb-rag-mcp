from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from gigacode_graph.algorithms import (
    BaseGraphBuildAlgorithm,
    GraphAlgorithmDescriptor,
    GraphBuildContext,
    GraphBuildRequest,
    GraphBuildResult,
    registry,
)
from gigacode_graph.cli import app
from gigacode_graph.config import GraphSettings
from gigacode_graph.lab.models import GraphLabCase, load_yaml_model
from gigacode_graph.lab.repair import prepare_repair, promote_algorithm
from gigacode_graph.lab.runner import GraphLabRunner
from gigacode_graph.lab.validation import compare_graphs, explain_edge, validate_graph
from gigacode_graph.models import Evidence, GraphEdge, GraphNode, GraphSnapshot
from gigacode_graph.store import JsonGraphStore
from service_map import RepositoryInput, ServiceMapBuilder


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _spring_repositories(root: Path) -> tuple[Path, Path]:
    orders = root / "orders"
    payments = root / "payments"
    _write(
        orders,
        "src/main/resources/application.properties",
        "spring.application.name=order-service\n",
    )
    _write(
        orders,
        "src/main/java/example/OrderController.java",
        """
package example;
@RestController
@RequestMapping("/orders")
public class OrderController {
  @PostMapping public void create() {}
}
""",
    )
    _write(
        orders,
        "src/main/java/example/PaymentClient.java",
        """
package example;
@FeignClient(name = "payment-service")
public interface PaymentClient {
  @PostMapping("/payments") void pay();
}
""",
    )
    _write(
        payments,
        "src/main/resources/application.properties",
        "spring.application.name=payment-service\n",
    )
    _write(
        payments,
        "src/main/java/example/PaymentController.java",
        """
package example;
@RestController
@RequestMapping("/payments")
public class PaymentController {
  @PostMapping public void pay() {}
}
""",
    )
    return orders, payments


class _RecordingAlgorithm(BaseGraphBuildAlgorithm):
    def __init__(self) -> None:
        self.calls: list[GraphBuildRequest] = []

    @property
    def descriptor(self) -> GraphAlgorithmDescriptor:
        return GraphAlgorithmDescriptor(
            id="recording-test",
            version="1.2.3",
            description="test implementation",
            cache_namespace="recording-test-v1",
        )

    def build(
        self,
        request: GraphBuildRequest,
        context: GraphBuildContext,
    ) -> GraphBuildResult:
        self.calls.append(request)
        target = request.targets[0]
        service_id = target.service_id or "recorded"
        graph = GraphSnapshot(
            algorithm=self.descriptor.as_dict(),
            nodes=[
                GraphNode(
                    id=f"service:{service_id}",
                    type="Service",
                    label=target.display_name or service_id,
                    service_id=service_id,
                    metadata={
                        "path": str(target.path.resolve()),
                        "repository_path": str(
                            (target.repository_path or target.path).resolve()
                        ),
                        "module_path": target.module_path,
                        "module_state": target.module_state,
                        "build_system": target.build_system,
                    },
                )
            ],
        )
        return GraphBuildResult(graph=graph, descriptor=self.descriptor)


def test_service_map_executes_injected_algorithm_contract(tmp_path: Path) -> None:
    repository = tmp_path / "service"
    _write(
        repository,
        "src/main/resources/application.properties",
        "spring.application.name=contract-service\n",
    )
    algorithm = _RecordingAlgorithm()
    settings = GraphSettings(module_cache_path=tmp_path / "modules")

    result = ServiceMapBuilder(settings, algorithm=algorithm).build(
        [RepositoryInput(path=repository, name="Contract service")],
        force_all=True,
    )

    assert algorithm.calls
    assert result.graph.algorithm["id"] == "recording-test"
    assert result.graph.algorithm["version"] == "1.2.3"
    assert [item.id for item in result.service_map.services] == ["contract-service"]
    assert result.service_map.algorithm == result.graph.algorithm


def test_graph_validation_and_explanation_include_evidence() -> None:
    evidence = Evidence(
        id="evidence:1",
        repository="orders",
        file="Client.java",
        line=7,
        snippet="client.pay()",
        extractor="test",
    )
    source = GraphNode(
        id="service:orders",
        type="Service",
        label="Orders",
        service_id="orders",
    )
    target = GraphNode(
        id="service:payments",
        type="Service",
        label="Payments",
        service_id="payments",
    )
    edge = GraphEdge(
        id="edge:orders-payments",
        source=source.id,
        target=target.id,
        type="DEPENDS_ON",
        label="POST /payments",
        metadata={"protocol": "HTTP", "operation": "POST /payments", "matcher": "test"},
        evidence_ids=[evidence.id],
    )
    graph = GraphSnapshot(nodes=[source, target], edges=[edge], evidence=[evidence])

    validation = validate_graph(graph)
    explanation = explain_edge(graph, edge.id)
    comparison = compare_graphs(GraphSnapshot(), graph)

    assert validation["status"] == "passed"
    assert explanation["decision"]["matcher"] == "test"
    assert explanation["evidence"][0]["file"] == "Client.java"
    assert comparison["edges"]["added"] == [edge.id]


def test_graph_lab_run_writes_replayable_static_bundle(tmp_path: Path) -> None:
    orders, payments = _spring_repositories(tmp_path)
    case_path = tmp_path / "CASE.yaml"
    case_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "id": "orders-payments",
                "description": "Known HTTP dependency",
                "algorithm": "static-v2",
                "mode": "static",
                "repositories": [
                    {"name": "orders", "source": str(orders)},
                    {"name": "payments", "source": str(payments)},
                ],
                "expectations": {
                    "counts": {
                        "min_services": 2,
                        "min_entrypoints": 2,
                        "min_exitpoints": 1,
                        "min_dependencies": 1,
                    },
                    "required_edges": [
                        {
                            "source": "order-service",
                            "target": "payment-service",
                            "protocol": "HTTP",
                            "operation_contains": "/payments",
                        }
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    run = GraphLabRunner(tmp_path / "graph-lab").run(case_path)

    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    validation = json.loads((run / "validation.json").read_text(encoding="utf-8"))
    graph = JsonGraphStore(run / "final-graph.json").load()
    replay_case = load_yaml_model(run / "replay-case.yaml", GraphLabCase)
    assert manifest["status"] == "passed"
    assert validation["status"] == "passed"
    assert graph.algorithm["id"] == "static-v2"
    assert graph.snapshot_id
    assert len(replay_case.repositories) == 2
    assert orders.is_dir() and payments.is_dir()
    for name in (
        "events.jsonl",
        "repositories.json",
        "static-graph.json",
        "candidates.json",
        "static-validation.json",
        "final-graph.json",
        "validation.json",
        "report.md",
    ):
        assert (run / name).is_file()


def test_algorithm_cli_lists_real_registry() -> None:
    response = CliRunner().invoke(app, ["algorithm", "list"])

    assert response.exit_code == 0
    payload = json.loads(response.stdout)
    assert any(item["id"] == "static-v2" for item in payload["algorithms"])
    assert registry.create("static-v2").descriptor.version == "2.0.0"


def test_repair_task_and_promotion_require_machine_readable_run(tmp_path: Path) -> None:
    run = tmp_path / "runs" / "passed-run"
    run.mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "passed-run",
                "status": "passed",
                "algorithm": registry.create("static-v2").descriptor.as_dict(),
                "validation": {"failure_count": 0},
            }
        ),
        encoding="utf-8",
    )
    (run / "validation.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "failure_count": 0,
                "warning_count": 0,
                "failures": [],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    task = prepare_repair(run, tmp_path / "graph-lab")

    task_payload = json.loads((task / "task.json").read_text(encoding="utf-8"))
    assert task_payload["rules"]["automatic_apply"] is False
    assert task_payload["experiment_path"].startswith("graph-lab/experiments/")
    assert (task / "TASK.md").is_file()
    registry_path = tmp_path / "graph-lab" / "ALGORITHMS.yaml"
    promoted = promote_algorithm(
        registry_path,
        algorithm_id="static-v2",
        version="2.0.0",
        stage="candidate",
        evidence_run=run,
    )
    assert promoted.algorithms[0].evidence_runs == [str(run.resolve())]
    assert load_yaml_model(registry_path, type(promoted)) == promoted
