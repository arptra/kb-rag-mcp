from __future__ import annotations

import threading
from pathlib import Path

import pytest

from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphNode, GraphSnapshot
from gigacode_graph.store import JsonGraphStore
from service_map import (
    JsonServiceMapStore,
    RepositoryInput,
    ServiceMapBuildCancelled,
    ServiceMapBuilder,
    ServiceMapBuildTimedOut,
    ServiceMapProcessRunner,
)
from service_map.models import ServiceMapSnapshot, ServiceRecord


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


def test_service_map_discovers_maven_modules_and_keeps_empty_modules(tmp_path: Path) -> None:
    repository = tmp_path / "commerce-platform"
    _write(
        repository,
        "pom.xml",
        """
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>commerce-platform</artifactId><version>1</version>
  <packaging>pom</packaging>
  <modules><module>orders</module><module>payments</module><module>empty-module</module></modules>
</project>
""",
    )
    for module in ("orders", "payments", "empty-module"):
        _write(
            repository,
            f"{module}/pom.xml",
            f"""
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId><artifactId>{module}</artifactId><version>1</version>
</project>
""",
        )
    _write(
        repository,
        "orders/src/main/resources/application.properties",
        "spring.application.name=orders-service\n",
    )
    _write(
        repository,
        "orders/src/main/java/example/StatusController.java",
        """
package example;
@RestController
@RequestMapping("/orders")
public class StatusController {
  @GetMapping("/status")
  public String status() { return "ok"; }
}
""",
    )
    _write(
        repository,
        "payments/src/main/java/example/StatusController.java",
        """
package example;
@RestController
@RequestMapping("/payments")
public class StatusController {
  @GetMapping("/status")
  public String status() { return "ok"; }
}
""",
    )

    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    result = ServiceMapBuilder(settings).build(
        [RepositoryInput(path=repository, name="Commerce platform")]
    )

    services = {item.id: item for item in result.service_map.services}
    assert set(services) == {"orders-service", "payments", "empty-module"}
    assert services["orders-service"].module_path == "orders"
    assert services["payments"].module_path == "payments"
    assert services["empty-module"].module_path == "empty-module"
    assert services["empty-module"].module_state == "empty"
    assert services["empty-module"].entrypoints == []
    assert services["orders-service"].entrypoints[0].operation == "GET /orders/status"
    assert services["payments"].entrypoints[0].operation == "GET /payments/status"


def test_service_map_discovers_gradle_modules_without_running_gradle(tmp_path: Path) -> None:
    repository = tmp_path / "gradle-platform"
    _write(repository, "settings.gradle.kts", 'include(":api", ":empty")\n')
    _write(repository, "api/build.gradle.kts", "plugins { java }\n")
    (repository / "empty").mkdir(parents=True)
    _write(
        repository,
        "api/src/main/java/example/ApiController.java",
        """
package example;
@RestController
public class ApiController {
  @GetMapping("/health")
  public String health() { return "ok"; }
}
""",
    )

    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    result = ServiceMapBuilder(settings).build(
        [RepositoryInput(path=repository, name="Gradle platform")]
    )

    services = {item.module_path: item for item in result.service_map.services}
    assert set(services) == {"api", "empty"}
    assert services["api"].build_system == "gradle"
    assert services["api"].module_state == "active"
    assert services["empty"].module_state == "empty"


def test_service_map_process_honours_cancellation_before_start(tmp_path: Path) -> None:
    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(ServiceMapBuildCancelled):
        ServiceMapProcessRunner(settings, timeout_seconds=10).build([], cancel=cancel)


def test_service_map_process_streams_repository_and_module_progress(tmp_path: Path) -> None:
    repository = tmp_path / "observable-service"
    _write(
        repository,
        "src/main/java/example/StatusController.java",
        """
@RestController
public class StatusController {
  @GetMapping("/status")
  public String status() { return "ok"; }
}
""",
    )
    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    events: list[str] = []
    checkpoints = []

    result = ServiceMapProcessRunner(settings, timeout_seconds=10).build(
        [RepositoryInput(path=repository, name="Observable service")],
        progress=events.append,
        checkpoint=checkpoints.append,
    )

    assert result.service_map.services
    assert checkpoints
    assert checkpoints[0].partial is True
    assert checkpoints[0].service_map.services[0].id == "observable-service"
    assert any("Layout ready: Observable service" in event for event in events)
    assert any("Java files found:" in event and "files=1" in event for event in events)
    assert any("Snapshot ready:" in event for event in events)
    assert events[-1] == "Worker completed successfully"


def test_service_map_process_has_a_hard_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HangingProcess:
        exitcode = None

        def __init__(self) -> None:
            self.alive = True
            self.started = False
            self.terminated = False

        def start(self) -> None:
            self.started = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

        def kill(self) -> None:
            self.alive = False

    process = HangingProcess()

    class FakeContext:
        @staticmethod
        def Process(**_kwargs: object) -> HangingProcess:
            return process

    monkeypatch.setattr(
        "service_map.runner.multiprocessing.get_context",
        lambda _method: FakeContext(),
    )
    monotonic_values = iter([0.0, 11.0])
    monkeypatch.setattr(
        "service_map.runner.time.monotonic",
        lambda: next(monotonic_values),
    )
    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)

    with pytest.raises(ServiceMapBuildTimedOut, match="10 seconds"):
        ServiceMapProcessRunner(settings, timeout_seconds=10).build(
            [RepositoryInput(path=tmp_path, name="slow-service")]
        )

    assert process.started is True
    assert process.terminated is True


def test_service_map_process_returns_latest_checkpoint_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CheckpointThenHangProcess:
        exitcode = None

        def __init__(self, args: tuple[object, ...]) -> None:
            self.args = args
            self.alive = True

        def start(self) -> None:
            graph_path = self.args[2]
            service_map_path = self.args[3]
            checkpoint_path = self.args[6]
            assert isinstance(graph_path, Path)
            assert isinstance(service_map_path, Path)
            assert isinstance(checkpoint_path, Path)
            JsonGraphStore(graph_path).save(
                GraphSnapshot(
                    nodes=[
                        GraphNode(
                            id="service:slow-service",
                            type="Service",
                            label="slow-service",
                            service_id="slow-service",
                        )
                    ]
                )
            )
            JsonServiceMapStore(service_map_path).save(
                ServiceMapSnapshot(
                    services=[
                        ServiceRecord(
                            id="slow-service",
                            name="slow-service",
                            repository="Slow repository",
                            repository_path=str(tmp_path),
                        )
                    ]
                )
            )
            checkpoint_path.write_text("ready", encoding="utf-8")

        def is_alive(self) -> bool:
            return self.alive

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def terminate(self) -> None:
            self.alive = False

        def kill(self) -> None:
            self.alive = False

    class FakeContext:
        @staticmethod
        def Process(**kwargs: object) -> CheckpointThenHangProcess:
            args = kwargs["args"]
            assert isinstance(args, tuple)
            return CheckpointThenHangProcess(args)

    monkeypatch.setattr(
        "service_map.runner.multiprocessing.get_context",
        lambda _method: FakeContext(),
    )
    monotonic_values = iter([0.0, 11.0])
    monkeypatch.setattr(
        "service_map.runner.time.monotonic",
        lambda: next(monotonic_values),
    )
    settings = GraphSettings(store_path=tmp_path / "system-graph.json").resolved(tmp_path)
    checkpoints = []

    result = ServiceMapProcessRunner(settings, timeout_seconds=10).build(
        [RepositoryInput(path=tmp_path, name="Slow repository")],
        checkpoint=checkpoints.append,
    )

    assert result.partial is True
    assert result.service_map.services[0].id == "slow-service"
    assert "time limit" in result.service_map.issues[-1].message
    assert checkpoints[-1].partial is True
