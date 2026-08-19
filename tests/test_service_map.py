from __future__ import annotations

from pathlib import Path

from gigacode_graph.config import GraphSettings
from service_map import JsonServiceMapStore, RepositoryInput, ServiceMapBuilder


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_service_map_finds_interfaces_and_resolves_known_services_without_ssot(
    tmp_path: Path,
) -> None:
    orders = tmp_path / "orders-repository"
    payments = tmp_path / "payments-repository"
    _write(
        orders,
        "src/main/resources/application.properties",
        "spring.application.name=orders-service\n",
    )
    _write(
        orders,
        "src/main/java/example/OrderController.java",
        """
package example;
@RestController
@RequestMapping("/orders")
public class OrderController {
  @PostMapping
  public Order create(OrderRequest request) { return new Order(); }
}
""",
    )
    _write(
        orders,
        "src/main/java/example/PaymentsClient.java",
        """
package example;
@FeignClient(name = "payments-service")
public interface PaymentsClient {
  @PostMapping("/payments")
  Payment pay(PaymentRequest request);
}
""",
    )
    _write(
        orders,
        "src/main/java/example/LedgerClient.java",
        """
package example;
@FeignClient(name = "ledger-service")
public interface LedgerClient {
  @PostMapping("/entries")
  void record(Entry entry);
}
""",
    )
    _write(
        payments,
        "src/main/resources/application.properties",
        "spring.application.name=payments-service\n",
    )
    _write(
        payments,
        "src/main/java/example/PaymentsController.java",
        """
package example;
@RestController
@RequestMapping("/payments")
public class PaymentsController {
  @PostMapping
  public Payment pay(PaymentRequest request) { return new Payment(); }
}
""",
    )

    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    result = ServiceMapBuilder(settings).build(
        [
            RepositoryInput(
                path=orders,
                name="Orders API",
                source_url="https://git.example.test/orders.git",
                commit="orders-commit",
            ),
            RepositoryInput(path=payments, name="Payments API"),
        ]
    )

    snapshot = result.service_map
    assert {item.id for item in snapshot.services} == {
        "orders-service",
        "payments-service",
    }
    order_service = next(item for item in snapshot.services if item.id == "orders-service")
    assert order_service.name == "Orders API"
    assert order_service.repository == "Orders API"
    assert order_service.source_url == "https://git.example.test/orders.git"
    assert order_service.commit == "orders-commit"
    assert {(item.kind, item.operation) for item in order_service.entrypoints} == {
        ("HTTP", "POST /orders")
    }
    assert {item.target_hint for item in order_service.outbound_interfaces} == {
        "payments-service",
        "ledger-service",
    }
    assert any(
        item.source_service_id == "orders-service"
        and item.target_service_id == "payments-service"
        and item.resolved
        for item in snapshot.dependencies
    )
    assert any(
        item.source_service_id == "orders-service"
        and item.target_hint == "ledger-service"
        and not item.resolved
        for item in snapshot.dependencies
    )
    assert snapshot.evidence
    assert snapshot.overview()["unresolved_dependency_count"] == 1

    store = JsonServiceMapStore(tmp_path / "artifacts" / "service_map.json")
    store.save(snapshot)
    restored = store.load()
    assert restored == snapshot
    assert store.path.is_file()


def test_empty_service_map_is_a_valid_file_snapshot(tmp_path: Path) -> None:
    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    result = ServiceMapBuilder(settings).build([])
    store = JsonServiceMapStore(tmp_path / "service_map.json")
    store.save(result.service_map)

    assert store.load().overview()["service_count"] == 0
    assert result.graph.nodes == []
