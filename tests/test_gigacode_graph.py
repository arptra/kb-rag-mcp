from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from typer.testing import CliRunner

from gigacode_graph.cli import app as graph_cli
from gigacode_graph.config import GraphSettings
from gigacode_graph.http_server import create_http_app
from gigacode_graph.mcp_server import create_mcp_server
from gigacode_graph.models import GraphEdge, GraphNode, GraphSnapshot
from gigacode_graph.scanner import RepositoryScanner, merge_and_relink_snapshots
from gigacode_graph.service import GraphService
from gigacode_graph.sources import RepositoryOperationCancelled, RepositorySourceManager
from gigacode_graph.store import JsonGraphStore


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _indexed_graph(tmp_path: Path) -> tuple[GraphService, GraphSettings]:
    order = tmp_path / "order-repository"
    inventory = tmp_path / "inventory-repository"
    _write(
        order,
        "src/main/resources/application.properties",
        "spring.application.name=order-service\n",
    )
    _write(
        order,
        "src/main/java/example/InventoryClient.java",
        """
package example;
@FeignClient(name = "inventory-service")
public interface InventoryClient {
  @GetMapping("/api/inventory/{sku}")
  StockResponse stock(String sku);
  @PostMapping("/api/inventory/reserve")
  void reserve(String sku);
}
""",
    )
    _write(
        order,
        "src/main/java/example/OrderController.java",
        """
package example;
@RestController
@RequestMapping("/orders")
public class OrderController {
  private final OrderService orderService;
  @PostMapping
  public Order create(OrderRequest request) {
    return orderService.create(request);
  }
}
""",
    )
    _write(
        order,
        "src/main/java/example/OrderService.java",
        """
package example;
@Service
public class OrderService {
  private final OrderRepository orderRepository;
  private final InventoryClient inventoryClient;
  private final KafkaTemplate<String, String> kafkaTemplate;
  public Order create(OrderRequest request) {
    if (request.amount() <= 0) {
      throw new InvalidOrderException();
    }
    inventoryClient.stock(request.sku());
    Order saved = orderRepository.save(new Order());
    kafkaTemplate.send("order.created", saved.toString());
    return saved;
  }
}
""",
    )
    _write(
        order,
        "src/main/java/example/Order.java",
        """
package example;
@Entity
@Table(name = "orders")
public class Order {
  @Id
  @Column(name = "order_id")
  private Long id;
  @Column(name = "amount")
  private BigDecimal amount;
}
""",
    )
    _write(
        order,
        "src/main/java/example/OrderRepository.java",
        """
package example;
public interface OrderRepository extends JpaRepository<Order, Long> {
}
""",
    )
    _write(
        order,
        "src/main/resources/db/migration/V1__orders.sql",
        "create table orders (order_id bigint primary key, amount decimal);\n",
    )
    _write(
        inventory,
        "src/main/resources/application.yml",
        "spring:\n  application:\n    name: inventory-service\n",
    )
    _write(
        inventory,
        "src/main/java/example/InventoryController.java",
        """
package example;
@RestController
@RequestMapping("/api/inventory")
public class InventoryController {
  @GetMapping("/{sku}")
  public StockResponse stock(String sku) { return new StockResponse(); }
  @PostMapping("/reserve")
  public void reserve(String sku) { }
  @KafkaListener(topics = "order.created")
  public void reserve(String event) { }
}
""",
    )
    settings = GraphSettings(store_path=tmp_path / "graph.json").resolved(tmp_path)
    snapshot = RepositoryScanner(settings).scan([order, inventory])
    store = JsonGraphStore(settings.store_path)
    store.save(snapshot)
    return GraphService(store), settings


def test_scanner_extracts_cross_repo_business_and_database_graph(tmp_path: Path) -> None:
    service, _settings = _indexed_graph(tmp_path)
    snapshot = service.snapshot
    service_ids = {node.id for node in snapshot.nodes if node.type == "Service"}
    assert service_ids == {"service:order-service", "service:inventory-service"}

    dependencies = [
        edge
        for edge in snapshot.edges
        if edge.type == "DEPENDS_ON"
        and edge.source == "service:order-service"
        and edge.target == "service:inventory-service"
    ]
    assert {edge.metadata["protocol"] for edge in dependencies} == {"HTTP", "KAFKA"}
    assert any(node.type == "BusinessOperation" for node in snapshot.nodes)
    assert any(
        node.type == "ExitPoint" and node.metadata.get("protocol") == "HTTP"
        for node in snapshot.nodes
    )
    assert any(
        node.type == "ExitPoint" and node.metadata.get("protocol") == "KAFKA"
        for node in snapshot.nodes
    )
    assert any(
        node.type == "BusinessRule" and "amount() <= 0" in node.label for node in snapshot.nodes
    )
    assert any(node.id == "table:order-service:orders" for node in snapshot.nodes)
    assert any(node.label == "order_id" and node.type == "Column" for node in snapshot.nodes)
    assert any(
        edge.type == "WRITES" and edge.target == "table:order-service:orders"
        for edge in snapshot.edges
    )
    assert snapshot.evidence
    assert all(item.file and item.line >= 1 for item in snapshot.evidence)


def test_graph_queries_return_service_dossier_and_evidence(tmp_path: Path) -> None:
    service, _settings = _indexed_graph(tmp_path)
    overview = service.overview()
    assert overview["resolved_service_dependency_count"] == 2
    assert overview["external_dependency_count"] == 0
    assert overview["unresolved_dependency_count"] == 0
    assert overview["isolated_service_count"] == 0
    assert overview["exitpoint_count"] >= 3
    view = service.graph(view="services")
    assert {node["id"] for node in view["nodes"]} == {
        "service:order-service",
        "service:inventory-service",
    }
    http_links = [edge for edge in view["edges"] if edge["metadata"].get("protocol") == "HTTP"]
    assert len(http_links) == 1
    assert http_links[0]["metadata"]["operation_count"] == 2
    assert len(http_links[0]["metadata"]["edge_ids"]) == 2
    dossier = service.service_details("order-service")
    assert dossier["business"]["operation_count"] == 1
    assert {item["type"] for item in dossier["dependencies"]["nodes"]} == {"Service"}
    assert {item["type"] for item in dossier["business"]["related_nodes"]} >= {
        "EntryPoint",
        "ExitPoint",
        "CodeSymbol",
        "BusinessRule",
    }
    assert any(
        item["id"] == "table:order-service:orders" for item in dossier["data_model"]["nodes"]
    )
    result = service.search("amount", service="order-service")
    assert {item["type"] for item in result["results"]} >= {"BusinessRule", "Column"}


def test_service_view_disambiguates_same_label_modules(tmp_path: Path) -> None:
    store = JsonGraphStore(tmp_path / "graph.json")
    store.save(
        GraphSnapshot(
            nodes=[
                GraphNode(
                    id="service:sample-java",
                    type="Service",
                    label="sample-service",
                    service_id="sample-java",
                    metadata={"module_path": "sample-java"},
                ),
                GraphNode(
                    id="service:sample-kotlin",
                    type="Service",
                    label="sample-service",
                    service_id="sample-kotlin",
                    metadata={"module_path": "sample-kotlin"},
                ),
            ]
        )
    )

    payload = GraphService(store).graph(view="services")

    assert {node["label"] for node in payload["nodes"]} == {
        "sample-service · sample-java",
        "sample-service · sample-kotlin",
    }


def test_merge_disambiguates_duplicate_service_ids_from_incremental_snapshots() -> None:
    snapshots = []
    for repository in ("orders-repository", "payments-repository"):
        snapshots.append(
            GraphSnapshot(
                nodes=[
                    GraphNode(
                        id="service:application",
                        type="Service",
                        label=repository,
                        service_id="application",
                        metadata={
                            "repository": repository,
                            "repository_path": f"/checkouts/{repository}",
                            "module_path": ".",
                            "aliases": ["application", repository],
                        },
                    ),
                    GraphNode(
                        id="symbol:application:Main#run",
                        type="CodeSymbol",
                        label="Main#run",
                        service_id="application",
                    ),
                ],
                edges=[
                    GraphEdge(
                        id=f"edge:{repository}",
                        source="service:application",
                        target="symbol:application:Main#run",
                        type="IMPLEMENTS",
                    )
                ],
            )
        )

    merged = merge_and_relink_snapshots(snapshots)

    services = [node for node in merged.nodes if node.type == "Service"]
    assert len(services) == 2
    assert len({node.id for node in services}) == 2
    assert {node.label for node in services} == {
        "orders-repository",
        "payments-repository",
    }
    assert all(node.metadata["base_service_id"] == "application" for node in services)
    assert len([node for node in merged.nodes if node.type == "CodeSymbol"]) == 2
    assert len([edge for edge in merged.edges if edge.type == "IMPLEMENTS"]) == 2


def test_merge_preserves_gigacode_dependency_decisions() -> None:
    snapshot = GraphSnapshot(
        nodes=[
            GraphNode(
                id="service:orders",
                type="Service",
                label="orders",
                service_id="orders",
                metadata={"aliases": ["orders"]},
            ),
            GraphNode(
                id="service:payments",
                type="Service",
                label="payments",
                service_id="payments",
                metadata={"aliases": ["payments"]},
            ),
            GraphNode(
                id="exitpoint:cancel",
                type="ExitPoint",
                label="HTTP POST /payments/cancel",
                service_id="orders",
                metadata={
                    "protocol": "HTTP",
                    "operation": "POST /payments/cancel",
                    "target_hint": "unknown-payments",
                },
            ),
            GraphNode(
                id="exitpoint:legacy",
                type="ExitPoint",
                label="HTTP GET /legacy",
                service_id="orders",
                metadata={
                    "protocol": "HTTP",
                    "operation": "GET /legacy",
                    "target_hint": "legacy-api",
                },
            ),
        ],
        edges=[
            GraphEdge(
                id="edge:exit:cancel",
                source="service:orders",
                target="exitpoint:cancel",
                type="EXITS_VIA",
            ),
            GraphEdge(
                id="edge:exit:legacy",
                source="service:orders",
                target="exitpoint:legacy",
                type="EXITS_VIA",
            ),
            GraphEdge(
                id="verified-retarget",
                source="service:orders",
                target="service:payments",
                type="DEPENDS_ON",
                label="HTTP POST /payments/cancel",
                confidence="HIGH",
                status="confirmed",
                origin="static+gigacode",
                metadata={
                    "protocol": "HTTP",
                    "operation": "POST /payments/cancel",
                    "verification_status": "retarget",
                },
            ),
            GraphEdge(
                id="verified-reject",
                source="service:orders",
                target="external:legacy-api",
                type="DEPENDS_ON",
                label="HTTP GET /legacy",
                confidence="LOW",
                status="rejected",
                origin="static+gigacode",
                metadata={
                    "protocol": "HTTP",
                    "operation": "GET /legacy",
                    "verification_status": "rejected",
                },
            ),
        ],
    )

    merged = merge_and_relink_snapshots([snapshot])
    service_dependencies = [
        edge
        for edge in merged.edges
        if edge.type == "DEPENDS_ON" and edge.source == "service:orders"
    ]

    retargeted = next(
        edge
        for edge in service_dependencies
        if edge.metadata.get("operation") == "POST /payments/cancel"
    )
    rejected = next(
        edge for edge in service_dependencies if edge.metadata.get("operation") == "GET /legacy"
    )
    assert retargeted.target == "service:payments"
    assert retargeted.origin == "static+gigacode"
    assert retargeted.status == "confirmed"
    assert rejected.status == "rejected"
    assert rejected.origin == "static+gigacode"


def test_http_contract_matches_unresolved_client_to_unique_endpoint(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "target"
    _write(caller, "src/main/resources/application.properties", "spring.application.name=caller\n")
    _write(
        caller,
        "src/main/java/example/TargetClient.java",
        """
package example;
@FeignClient(name = "${clients.unknown}")
public interface TargetClient {
  @PostMapping("/api/payments/{id}/cancel")
  void cancel(String id);
}
""",
    )
    _write(
        caller,
        "src/main/java/example/CallerService.java",
        """
package example;
@Service
public class CallerService {
  public void cancel(String id) {
    restTemplate.postForObject("http://target/api/payments/{id}/cancel", id, Void.class);
  }
}
""",
    )
    _write(target, "src/main/resources/application.properties", "spring.application.name=target\n")
    _write(
        target,
        "src/main/java/example/PaymentController.java",
        """
package example;
@RestController
@RequestMapping("/api/payments")
public class PaymentController {
  @PostMapping("/{paymentId}/cancel")
  public void cancel(String paymentId) { }
}
""",
    )

    snapshot = RepositoryScanner(GraphSettings()).scan([caller, target])
    dependency = next(
        edge
        for edge in snapshot.edges
        if edge.type == "DEPENDS_ON"
        and edge.source == "service:caller"
        and edge.target == "service:target"
    )
    assert dependency.confidence == "MEDIUM"
    assert dependency.metadata["matcher"] in {"http-contract", "alias+contract"}
    assert len(dependency.evidence_ids) >= 2
    assert any(item.extractor == "spring-rest-template" for item in snapshot.evidence)

    merged = merge_and_relink_snapshots(
        [
            RepositoryScanner(GraphSettings()).scan([caller]),
            RepositoryScanner(GraphSettings()).scan([target]),
        ]
    )
    merged_dependency = next(
        edge
        for edge in merged.edges
        if edge.type == "DEPENDS_ON"
        and edge.source == "service:caller"
        and edge.target == "service:target"
    )
    assert merged_dependency.metadata["matcher"] in {"http-contract", "alias+contract"}
    assert len(merged_dependency.evidence_ids) >= 2


def test_http_exchange_and_configured_kafka_contracts_are_linked(tmp_path: Path) -> None:
    caller = tmp_path / "caller"
    target = tmp_path / "target"
    _write(
        caller,
        "src/main/resources/application.properties",
        "spring.application.name=caller\ntopics.orders=orders.created\n",
    )
    _write(
        caller,
        "src/main/java/example/OrdersApi.java",
        """
package example;
@HttpExchange("/api/orders")
public interface OrdersApi {
  @GetExchange("/{id}")
  String get(String id);
}
""",
    )
    _write(
        caller,
        "src/main/java/example/Publisher.java",
        """
package example;
@Service
public class Publisher {
  public void publish(String value) {
    kafkaTemplate.send("${topics.orders}", value);
  }
}
""",
    )
    _write(
        target,
        "src/main/resources/application.properties",
        "spring.application.name=target\ntopics.orders=orders.created\n",
    )
    _write(
        target,
        "src/main/java/example/OrdersController.java",
        """
package example;
@RestController
@RequestMapping("/api/orders")
public class OrdersController {
  @GetMapping("/{orderId}")
  public String get(String orderId) { return orderId; }
  @KafkaListener(topics = "${topics.orders}")
  public void consume(String event) { }
}
""",
    )

    merged = merge_and_relink_snapshots(
        [
            RepositoryScanner(GraphSettings()).scan([caller]),
            RepositoryScanner(GraphSettings()).scan([target]),
        ]
    )

    protocols = {
        edge.metadata.get("protocol")
        for edge in merged.edges
        if edge.type == "DEPENDS_ON"
        and edge.source == "service:caller"
        and edge.target == "service:target"
    }
    assert protocols == {"HTTP", "KAFKA"}
    assert any(item.extractor == "spring-http-interface" for item in merged.evidence)
    assert any(node.type == "Event" and node.label == "orders.created" for node in merged.nodes)


@pytest.mark.asyncio
async def test_mcp_and_http_expose_the_same_read_only_graph(tmp_path: Path) -> None:
    service, settings = _indexed_graph(tmp_path)
    async with Client(create_mcp_server(service)) as client:
        listed = await client.list_tools()
        assert {item.name for item in listed} == {
            "code_graph_overview",
            "code_graph_search",
            "code_graph_service",
            "code_graph_dependencies",
            "code_graph_business_operations",
            "code_graph_data_model",
            "code_graph_evidence",
        }
        result = await client.call_tool(
            "code_graph_dependencies",
            {"service": "order-service", "direction": "outgoing", "depth": 1},
        )
        assert result.is_error is False
        assert any(edge["target"] == "service:inventory-service" for edge in result.data["edges"])

    app = create_http_app(service, settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client,
    ):
        assert (await client.get("/health")).status_code == 200
        page = await client.get("/graph")
        assert page.status_code == 200
        assert "GigaCode Repository Graph" in page.text
        graph = await client.get("/api/graph", params={"view": "services"})
        assert graph.status_code == 200
        assert len(graph.json()["nodes"]) == 2


def test_external_http_bind_requires_strong_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="BEARER_TOKEN"):
        GraphSettings(http_host="0.0.0.0", store_path=tmp_path / "x.json").validate_http_security()
    with pytest.raises(ValueError, match="at least 32"):
        GraphSettings(bearer_token="short", store_path=tmp_path / "x.json").validate_http_security()


def test_git_timeout_terminates_the_complete_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingGit:
        def __init__(self) -> None:
            self.args = ["git", "fetch"]
            self.pid = 12345
            self.returncode = -15

        def communicate(self, timeout: int) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(self.args, timeout)

        def poll(self) -> None:
            return None

    process = HangingGit()
    popen_options: dict[str, object] = {}

    def fake_popen(*_args: object, **kwargs: object) -> HangingGit:
        popen_options.update(kwargs)
        return process

    terminated: list[object] = []
    monkeypatch.setattr("gigacode_graph.sources.subprocess.Popen", fake_popen)
    monotonic_values = iter([0.0, 11.0])
    monkeypatch.setattr(
        "gigacode_graph.sources.time.monotonic",
        lambda: next(monotonic_values),
    )
    monkeypatch.setattr(
        RepositorySourceManager,
        "_terminate_process_tree",
        staticmethod(terminated.append),
    )
    settings = GraphSettings(
        store_path=tmp_path / "graph.json",
        git_timeout_seconds=10,
    ).resolved(tmp_path)

    with pytest.raises(RuntimeError, match="timed out"):
        RepositorySourceManager(settings)._git(
            ["fetch"],
            cwd=tmp_path,
            source="https://git.example.test/service.git",
        )

    assert terminated == [process]
    assert popen_options["start_new_session"] is True
    environment = popen_options["env"]
    assert isinstance(environment, dict)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GCM_INTERACTIVE"] == "Never"


def test_git_cancellation_terminates_the_complete_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_event = threading.Event()

    class HangingGit:
        def __init__(self) -> None:
            self.args = ["git", "fetch"]
            self.pid = 12345
            self.returncode = -15

        def communicate(self, timeout: float) -> tuple[str, str]:
            cancel_event.set()
            raise subprocess.TimeoutExpired(self.args, timeout)

        def poll(self) -> None:
            return None

    process = HangingGit()
    terminated: list[object] = []
    monkeypatch.setattr(
        "gigacode_graph.sources.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        RepositorySourceManager,
        "_terminate_process_tree",
        staticmethod(terminated.append),
    )
    settings = GraphSettings(
        store_path=tmp_path / "graph.json",
        git_timeout_seconds=10,
    ).resolved(tmp_path)
    manager = RepositorySourceManager(settings)
    manager._cancel_event = cancel_event

    with pytest.raises(RepositoryOperationCancelled):
        manager._git(
            ["fetch"],
            cwd=tmp_path,
            source="https://git.example.test/service.git",
        )

    assert terminated == [process]


def test_cli_git_url_clones_updates_and_reloads_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote = tmp_path / "remote-order-service"
    remote.mkdir()
    _git(remote, "init", "--quiet")
    _git(remote, "config", "user.email", "graph-test@example.test")
    _git(remote, "config", "user.name", "Graph Test")
    _write(
        remote,
        "src/main/resources/application.properties",
        "spring.application.name=remote-order-service\n",
    )
    _write(
        remote,
        "src/main/java/example/OrderController.java",
        """
package example;
@RestController
@RequestMapping("/orders")
public class OrderController {
  @GetMapping("/{id}")
  public Order get(String id) { return new Order(); }
}
""",
    )
    first_commit = _commit(remote, "initial service")
    store_path = tmp_path / "artifacts" / "graph.json"
    runner = CliRunner()

    first = runner.invoke(
        graph_cli,
        ["index", remote.as_uri(), "--store", str(store_path)],
    )
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.output)
    assert first_payload["repositories"][0]["action"] == "cloned"
    assert first_payload["repositories"][0]["commit"] == first_commit
    ingestion_path = store_path.parent / "ingestion.json"
    ingestion = json.loads(ingestion_path.read_text(encoding="utf-8"))
    assert ingestion["repositories"][0]["source"] == remote.as_uri()
    checkout = Path(ingestion["repositories"][0]["checkout_path"])
    assert checkout.is_dir()
    assert checkout.is_relative_to(store_path.parent / "repositories")

    live_service = GraphService(JsonGraphStore(store_path))
    initial_nodes = live_service.overview()["node_count"]
    _write(
        remote,
        "src/main/java/example/Order.java",
        """
package example;
@Entity
@Table(name = "orders")
public class Order {
  @Id
  @Column(name = "order_id")
  private Long id;
}
""",
    )
    second_commit = _commit(remote, "add data model")

    second = runner.invoke(
        graph_cli,
        ["index", remote.as_uri(), "--store", str(store_path)],
    )
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    assert second_payload["repositories"][0]["action"] == "updated"
    assert second_payload["repositories"][0]["commit"] == second_commit
    assert live_service.overview()["node_count"] > initial_nodes
    assert any(
        node.id == "table:remote-order-service:orders" for node in live_service.snapshot.nodes
    )

    served: list[GraphSettings] = []
    monkeypatch.setattr(
        "gigacode_graph.http_server.run_http_server",
        lambda settings: served.append(settings),
    )
    up = runner.invoke(
        graph_cli,
        ["up", remote.as_uri(), "--store", str(store_path), "--no-refresh"],
    )
    assert up.exit_code == 0, up.output
    assert '"action": "reused"' in up.output
    assert served[0].store_path == store_path
