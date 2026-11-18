from pathlib import Path

import pytest

from gigacode_graph.config import GraphSettings
from service_map import RepositoryInput, ServiceMapBuilder, ServiceMapProcessRunner


def _repository(root: Path) -> Path:
    source = root / "src/main/java/example/Controller.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        '@RestController\npublic class Controller {\n'
        '  @GetMapping("/current") public String get() { return "ok"; }\n}\n',
        encoding="utf-8",
    )
    resources = root / "src/main/resources"
    resources.mkdir(parents=True)
    (resources / "application.properties").write_text(
        "spring.application.name=shared-service\n", encoding="utf-8"
    )
    return root


def test_discovery_uses_current_service_id_rules_without_parsing_or_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repositories = [
        RepositoryInput(path=_repository(tmp_path / name), name=name)
        for name in ("orders", "payments")
    ]
    settings = GraphSettings(store_path=tmp_path / "graph.json").resolved(tmp_path)
    expected = ServiceMapBuilder(settings).build(repositories)
    builder = ServiceMapBuilder(settings)
    original = builder._run_algorithm
    algorithm_calls: list[bool] = []

    def discover_only(targets, *, discovery_only, progress):
        algorithm_calls.append(discovery_only)
        assert discovery_only, "Discovery must not invoke source analysis"
        return original(targets, discovery_only=discovery_only, progress=progress)

    monkeypatch.setattr(builder, "_run_algorithm", discover_only)
    checkpoints = []
    result = builder.build(
        repositories,
        discovery_only=True,
        checkpoint=checkpoints.append,
    )

    assert algorithm_calls == [True]
    assert checkpoints == []
    assert not result.partial
    assert {service.id for service in result.service_map.services} == {
        service.id for service in expected.service_map.services
    }
    assert len(result.service_map.services) == 2
    assert all(service.id.startswith("shared-service--") for service in result.service_map.services)
    assert all(not service.entrypoints for service in result.service_map.services)
    assert result.graph.algorithm == expected.graph.algorithm
    assert any("Duplicate discovered service id" in issue.message for issue in result.graph.issues)
    assert not settings.store_path.exists()


def test_process_discovery_leaves_persisted_graph_unchanged(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "service")
    settings = GraphSettings(store_path=tmp_path / "graph.json").resolved(tmp_path)
    settings.store_path.write_text("existing published graph", encoding="utf-8")
    checkpoints = []

    result = ServiceMapProcessRunner(settings, timeout_seconds=15).build(
        [RepositoryInput(path=repository, name="Service")],
        discovery_only=True,
        checkpoint=checkpoints.append,
    )

    assert not result.partial
    assert [service.id for service in result.service_map.services] == ["shared-service"]
    assert result.service_map.services[0].entrypoints == []
    assert checkpoints == []
    assert settings.store_path.read_text(encoding="utf-8") == "existing published graph"
