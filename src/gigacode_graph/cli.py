# ruff: noqa: RUF001
"""CLI for indexing repositories and querying the graph outside MCP."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from gigacode_graph.config import GraphSettings
from gigacode_graph.scanner import RepositoryScanner
from gigacode_graph.service import GraphService
from gigacode_graph.sources import RepositorySourceManager, RepositorySpec
from gigacode_graph.store import JsonGraphStore

app = typer.Typer(
    no_args_is_help=True,
    help="Индексатор и read-only граф Java/Spring сервисов для GigaCode.",
)


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
) -> tuple[GraphSettings, dict[str, Any]]:
    specs = [RepositorySpec(source=value, ref=ref) for value in repositories or []]
    if manifest is not None:
        specs.extend(_manifest_repositories(manifest.resolve()))
    if not specs:
        raise ValueError("Pass Git URLs, repository paths, or --manifest")
    settings = _settings(store)
    source_manager = RepositorySourceManager(settings)
    paths, records = source_manager.materialize(specs, refresh=refresh)
    snapshot = RepositoryScanner(settings).scan(paths)
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
) -> None:
    """Скачать Git URL и собрать готовые graph/ingestion artifacts."""
    _settings_used, payload = _ingest(
        repositories,
        manifest=manifest,
        store=store,
        ref=ref,
        refresh=refresh,
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
) -> None:
    """Скачать repositories, построить граф и сразу поднять UI + MCP."""
    settings, payload = _ingest(
        repositories,
        manifest=manifest,
        store=store,
        ref=ref,
        refresh=refresh,
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


def main() -> None:
    try:
        app()
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
