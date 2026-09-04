# ruff: noqa: RUF001
"""CLI for indexing repositories and querying the graph outside MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gigacode_graph.algorithms import (
    GraphBuildContext,
    GraphBuildRequest,
    get_graph_algorithm,
    registry,
)
from gigacode_graph.config import GraphSettings
from gigacode_graph.lab.models import GraphLabCase, load_yaml_model
from gigacode_graph.lab.repair import (
    apply_repair,
    prepare_repair,
    promote_algorithm,
    run_repair,
)
from gigacode_graph.lab.runner import GraphLabRunner
from gigacode_graph.lab.validation import (
    compare_graphs,
    explain_edge,
    explain_missing,
    validate_graph,
)
from gigacode_graph.scanner import ScanTarget
from gigacode_graph.service import GraphService
from gigacode_graph.sources import RepositorySourceManager, RepositorySpec
from gigacode_graph.store import JsonGraphStore

app = typer.Typer(
    no_args_is_help=True,
    help="Индексатор и read-only граф Java/Spring сервисов для GigaCode.",
)
algorithm_app = typer.Typer(no_args_is_help=True, help="Версии и реализации алгоритмов графа.")
debug_app = typer.Typer(no_args_is_help=True, help="Воспроизводимые graph-lab прогоны и ремонт.")
app.add_typer(algorithm_app, name="algorithm")
app.add_typer(debug_app, name="debug")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _settings(store: Path | None = None) -> GraphSettings:
    settings = GraphSettings().resolved()
    if store is not None:
        graph_path = store.resolve()
        settings = settings.model_copy(
            update={
                "store_path": graph_path,
                "repository_cache_path": graph_path.parent / "repositories",
                "ingestion_path": graph_path.parent / "ingestion.json",
            }
        )
    return settings


def _service(store: Path | None = None) -> GraphService:
    settings = _settings(store)
    return GraphService(JsonGraphStore(settings.store_path))


def _manifest_repositories(path: Path) -> list[RepositorySpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("repositories"), list):
        raise ValueError("Manifest must contain a repositories array")
    repositories: list[RepositorySpec] = []
    for item in payload["repositories"]:
        value = None
        ref = None
        if isinstance(item, dict):
            value = item.get("url") or item.get("path") or item.get("source")
            ref = item.get("ref")
            if ref is not None and not isinstance(ref, str):
                raise ValueError("Repository ref must be a string")
        else:
            value = item
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "Each manifest repository must be a string or an object with path/url/source"
            )
        repositories.append(RepositorySpec(source=value, ref=ref, base_directory=path.parent))
    return repositories


def _ingest(
    repositories: list[str] | None,
    *,
    manifest: Path | None,
    store: Path | None,
    ref: str | None,
    refresh: bool,
    algorithm: str,
) -> tuple[GraphSettings, dict[str, Any]]:
    specs = [RepositorySpec(source=value, ref=ref) for value in repositories or []]
    if manifest is not None:
        specs.extend(_manifest_repositories(manifest.resolve()))
    if not specs:
        raise ValueError("Pass Git URLs, repository paths, or --manifest")
    settings = _settings(store).model_copy(update={"builder_algorithm": algorithm})
    source_manager = RepositorySourceManager(settings)
    paths, records = source_manager.materialize(specs, refresh=refresh)
    implementation = get_graph_algorithm(algorithm)
    snapshot = implementation.build(
        GraphBuildRequest(targets=tuple(ScanTarget(path=path) for path in paths)),
        GraphBuildContext(settings=settings),
    ).graph
    JsonGraphStore(settings.store_path).save(snapshot)
    source_manager.save_manifest(records, snapshot)
    payload = snapshot.stats()
    payload["store_path"] = str(settings.store_path)
    payload["ingestion_path"] = str(settings.ingestion_path)
    payload["repository_cache_path"] = str(settings.repository_cache_path)
    payload["repositories"] = [record.model_dump(mode="json") for record in records]
    return settings, payload


@app.command()
def index(
    repositories: Annotated[
        list[str] | None,
        typer.Argument(help="Git URL или локальные checkout-ы; сборка и код не исполняются."),
    ] = None,
    manifest: Annotated[
        Path | None,
        typer.Option("--manifest", help="JSON-файл с массивом repositories."),
    ] = None,
    store: Annotated[
        Path | None,
        typer.Option("--store", help="Путь к versioned graph.json."),
    ] = None,
    ref: Annotated[
        str | None,
        typer.Option("--ref", help="Branch, tag или commit для переданных Git URL."),
    ] = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh/--no-refresh", help="Обновить уже клонированные Git URL."),
    ] = True,
    algorithm: Annotated[
        str,
        typer.Option("--algorithm", help="ID реализации из algorithm list."),
    ] = "static-v2",
) -> None:
    """Скачать Git URL и собрать готовые graph/ingestion artifacts."""
    _settings_used, payload = _ingest(
        repositories,
        manifest=manifest,
        store=store,
        ref=ref,
        refresh=refresh,
        algorithm=algorithm,
    )
    typer.echo(_dump(payload))


@app.command()
def up(
    repositories: Annotated[
        list[str] | None,
        typer.Argument(help="Git URL или локальные checkout-ы."),
    ] = None,
    manifest: Annotated[Path | None, typer.Option("--manifest")] = None,
    store: Annotated[Path | None, typer.Option("--store")] = None,
    ref: Annotated[str | None, typer.Option("--ref")] = None,
    refresh: Annotated[bool, typer.Option("--refresh/--no-refresh")] = True,
    host: Annotated[str | None, typer.Option("--host")] = None,
    port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
    algorithm: Annotated[str, typer.Option("--algorithm")] = "static-v2",
) -> None:
    """Скачать repositories, построить граф и сразу поднять UI + MCP."""
    settings, payload = _ingest(
        repositories,
        manifest=manifest,
        store=store,
        ref=ref,
        refresh=refresh,
        algorithm=algorithm,
    )
    updates: dict[str, Any] = {}
    if host is not None:
        updates["http_host"] = host
    if port is not None:
        updates["http_port"] = port
    if updates:
        settings = settings.model_copy(update=updates)
    typer.echo(_dump(payload))
    typer.echo(f"UI: http://{settings.http_host}:{settings.http_port}/graph", err=True)
    typer.echo(
        f"MCP: http://{settings.http_host}:{settings.http_port}{settings.mcp_path}",
        err=True,
    )
    from gigacode_graph.http_server import run_http_server

    run_http_server(settings)


@app.command()
def stats(
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Показать состав текущего графа и проблемы извлечения."""
    typer.echo(_dump(_service(store).overview()))


@app.command()
def services(
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Вернуть service-level граф, удобный и человеку, и GigaCode CLI."""
    typer.echo(_dump(_service(store).graph(view="services")))


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Текст, идентификатор или имя таблицы/операции.")],
    service: Annotated[str | None, typer.Option("--service")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Искать по всем типам узлов графа."""
    typer.echo(_dump(_service(store).search(query, service=service, limit=limit)))


@app.command(name="show")
def show_service(
    service: Annotated[str, typer.Argument(help="ID, alias или label сервиса.")],
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Показать полное evidence-backed досье сервиса."""
    typer.echo(_dump(_service(store).service_details(service)))


@app.command()
def dependencies(
    service: Annotated[str, typer.Argument(help="ID, alias или label сервиса.")],
    direction: Annotated[
        str,
        typer.Option("--direction", help="outgoing, incoming или both."),
    ] = "outgoing",
    depth: Annotated[int, typer.Option("--depth", min=1, max=10)] = 1,
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Пройти по межсервисным зависимостям."""
    typer.echo(_dump(_service(store).dependencies(service, direction=direction, depth=depth)))


@app.command()
def business(
    service: Annotated[str, typer.Argument(help="ID, alias или label сервиса.")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=1_000)] = 100,
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Показать операции, триггеры и извлечённые условные бизнес-правила."""
    typer.echo(_dump(_service(store).business_operations(service, limit=limit)))


@app.command(name="data-model")
def data_model(
    service: Annotated[str | None, typer.Option("--service")] = None,
    table: Annotated[str | None, typer.Option("--table")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=5_000)] = 500,
    store: Annotated[Path | None, typer.Option("--store")] = None,
) -> None:
    """Показать сущности, таблицы, колонки, миграции и READS/WRITES."""
    typer.echo(_dump(_service(store).data_model(service=service, table=table, limit=limit)))


@algorithm_app.command(name="list")
def algorithm_list() -> None:
    """Показать встроенные и установленные через entry point алгоритмы."""
    typer.echo(_dump({"algorithms": [item.as_dict() for item in registry.descriptors()]}))


@algorithm_app.command(name="contract")
def algorithm_contract() -> None:
    """Показать точку расширения для отдельного Python-пакета."""
    typer.echo(
        _dump(
            {
                "python_contract": "gigacode_graph.algorithms.GraphBuildAlgorithm",
                "optional_base_class": "gigacode_graph.algorithms.BaseGraphBuildAlgorithm",
                "entry_point_group": "corporate_kb.graph_algorithms",
                "required_members": ["descriptor", "build(request, context)"],
                "reference": "gigacode_graph.algorithms.static_v2.StaticV2Algorithm",
            }
        )
    )


@algorithm_app.command(name="promote")
def algorithm_promote(
    algorithm: Annotated[str, typer.Argument(help="ID установленного алгоритма.")],
    version: Annotated[str, typer.Option("--version")],
    evidence_run: Annotated[Path, typer.Option("--evidence-run")],
    stage: Annotated[
        str,
        typer.Option("--stage", help="experimental, candidate, production или retired."),
    ] = "candidate",
    registry_path: Annotated[
        Path,
        typer.Option("--registry", help="Версионируемый ALGORITHMS.yaml."),
    ] = Path("graph-lab/ALGORITHMS.yaml"),
) -> None:
    """Повысить версию только при наличии успешного validation run."""
    if stage not in {"experimental", "candidate", "production", "retired"}:
        raise ValueError("stage must be experimental, candidate, production or retired")
    updated = promote_algorithm(
        registry_path,
        algorithm_id=algorithm,
        version=version,
        stage=stage,
        evidence_run=evidence_run,
    )
    typer.echo(updated.model_dump_json(indent=2))


@debug_app.command(name="run")
def debug_run(
    case: Annotated[Path, typer.Argument(help="CASE.yaml с repositories и expectations.")],
    lab_root: Annotated[Path, typer.Option("--lab-root")] = Path("graph-lab"),
    mode: Annotated[str | None, typer.Option("--mode", help="static или gigacode.")] = None,
    algorithm: Annotated[str | None, typer.Option("--algorithm")] = None,
    keep_checkouts: Annotated[bool, typer.Option("--keep-checkouts")] = False,
) -> None:
    """Запустить изолированный анализ и собрать полный debug bundle."""
    if mode is not None and mode not in {"static", "gigacode"}:
        raise ValueError("mode must be static or gigacode")
    run_directory = GraphLabRunner(lab_root).run(
        case,
        mode=mode,  # type: ignore[arg-type]
        algorithm=algorithm,
        cleanup=not keep_checkouts,
    )
    payload = json.loads((run_directory / "run.json").read_text(encoding="utf-8"))
    typer.echo(_dump({"run_directory": str(run_directory), "run": payload}))
    if payload.get("status") != "passed":
        raise typer.Exit(code=2)


@debug_app.command(name="validate")
def debug_validate(
    graph: Annotated[Path, typer.Argument(help="graph.json для проверки.")],
    case: Annotated[Path | None, typer.Option("--case")] = None,
) -> None:
    """Проверить ID, ссылки, evidence и ожидания CASE.yaml без перестройки."""
    snapshot = JsonGraphStore(graph.resolve()).load()
    expectations = load_yaml_model(case.resolve(), GraphLabCase) if case else None
    result = validate_graph(snapshot, expectations)
    typer.echo(_dump(result))
    if result["status"] != "passed":
        raise typer.Exit(code=2)


@debug_app.command(name="explain-edge")
def debug_explain_edge(
    graph: Annotated[Path, typer.Argument()],
    edge_id: Annotated[str, typer.Argument()],
) -> None:
    """Show origin, confidence, matcher and source evidence for one edge."""
    typer.echo(_dump(explain_edge(JsonGraphStore(graph.resolve()).load(), edge_id)))


@debug_app.command(name="explain-missing")
def debug_explain_missing(
    graph: Annotated[Path, typer.Argument()],
    source: Annotated[str, typer.Option("--source")],
    target: Annotated[str | None, typer.Option("--target")] = None,
    protocol: Annotated[str | None, typer.Option("--protocol")] = None,
    operation: Annotated[str | None, typer.Option("--operation")] = None,
) -> None:
    """Объяснить, на каком шаге могла потеряться ожидаемая связь."""
    typer.echo(
        _dump(
            explain_missing(
                JsonGraphStore(graph.resolve()).load(),
                source=source,
                target=target,
                protocol=protocol,
                operation=operation,
            )
        )
    )


@debug_app.command(name="compare")
def debug_compare(
    before: Annotated[Path, typer.Argument()],
    after: Annotated[Path, typer.Argument()],
) -> None:
    """Compare graph IDs and, for run directories, wall time and memory."""
    before_path = before.resolve()
    after_path = after.resolve()
    before_graph = before_path / "final-graph.json" if before_path.is_dir() else before_path
    after_graph = after_path / "final-graph.json" if after_path.is_dir() else after_path
    comparison = compare_graphs(
        JsonGraphStore(before_graph).load(),
        JsonGraphStore(after_graph).load(),
    )
    if before_path.is_dir() and after_path.is_dir():
        before_run = json.loads((before_path / "run.json").read_text(encoding="utf-8"))
        after_run = json.loads((after_path / "run.json").read_text(encoding="utf-8"))
        before_elapsed = before_run.get("elapsed_seconds")
        after_elapsed = after_run.get("elapsed_seconds")
        comparison["performance"] = {
            "before_elapsed_seconds": before_elapsed,
            "after_elapsed_seconds": after_elapsed,
            "elapsed_delta_seconds": (
                round(float(after_elapsed) - float(before_elapsed), 6)
                if isinstance(before_elapsed, int | float)
                and isinstance(after_elapsed, int | float)
                else None
            ),
            "before_peak_process_memory_mb": before_run.get("peak_process_memory_mb"),
            "after_peak_process_memory_mb": after_run.get("peak_process_memory_mb"),
        }
    typer.echo(
        _dump(comparison)
    )


@debug_app.command(name="replay")
def debug_replay(
    run_directory: Annotated[Path, typer.Argument()],
    lab_root: Annotated[Path, typer.Option("--lab-root")] = Path("graph-lab"),
    keep_checkouts: Annotated[bool, typer.Option("--keep-checkouts")] = False,
) -> None:
    """Replay a static run at recorded commits and write its delta."""
    replayed = GraphLabRunner(lab_root).replay(
        run_directory,
        cleanup=not keep_checkouts,
    )
    payload = json.loads((replayed / "run.json").read_text(encoding="utf-8"))
    typer.echo(_dump({"run_directory": str(replayed), "run": payload}))
    if payload.get("status") != "passed":
        raise typer.Exit(code=2)


@debug_app.command(name="prepare-repair")
def debug_prepare_repair(
    run_directory: Annotated[Path, typer.Argument()],
    lab_root: Annotated[Path, typer.Option("--lab-root")] = Path("graph-lab"),
) -> None:
    """Создать TASK.md/failures.json; модель пока не запускается."""
    task = prepare_repair(run_directory, lab_root)
    typer.echo(_dump({"task_directory": str(task)}))


@debug_app.command(name="repair")
def debug_repair(
    task_directory: Annotated[Path, typer.Argument()],
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Разрешить изменения только в отдельном worktree."),
    ] = False,
    command: Annotated[str, typer.Option("--command")] = "gigacode",
    timeout_seconds: Annotated[
        int,
        typer.Option("--timeout", min=60, max=7200),
    ] = 1800,
) -> None:
    """Дать GigaCode исправить analyzer в worktree и получить проверенный patch."""
    if not allow_write:
        raise ValueError("repair requires explicit --allow-write")
    typer.echo(
        _dump(
            run_repair(
                task_directory,
                Path.cwd(),
                command=command,
                timeout_seconds=timeout_seconds,
            )
        )
    )


@debug_app.command(name="apply-repair")
def debug_apply_repair(
    patch: Annotated[Path, typer.Argument()],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Явно применить уже проверенный patch к текущей ветке."),
    ] = False,
) -> None:
    """Проверить git apply --check и применить patch; commit/push не выполняются."""
    if not yes:
        raise ValueError("apply-repair requires explicit --yes")
    apply_repair(patch, Path.cwd())
    typer.echo(_dump({"status": "applied", "patch": str(patch.resolve())}))


def main() -> None:
    try:
        app()
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
