from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from typer.testing import CliRunner

from gigacode_graph.cli import app as graph_cli
from gigacode_graph.config import GraphSettings
from gigacode_graph.http_server import create_http_app
from gigacode_graph.mcp_server import create_mcp_server
from gigacode_graph.scanner import RepositoryScanner
from gigacode_graph.service import GraphService
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
    view = service.graph(view="services")
    assert {node["id"] for node in view["nodes"]} == {
        "service:order-service",
        "service:inventory-service",
    }
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
