"""Human-friendly local CLI for indexing and retrieval checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from corporate_kb.config import Settings
from corporate_kb.evaluation.evaluator import Evaluator
from corporate_kb.models import Document, SearchFilters, SearchResult
from corporate_kb.service import KnowledgeService, configure_logging, create_service

app = typer.Typer(no_args_is_help=True, help="Локальная корпоративная база знаний.")
DEFAULT_QUESTIONS_PATH = Path("evaluation/questions.json")


def _service() -> KnowledgeService:
    settings = Settings().resolved()
    configure_logging(settings.log_level)
    return create_service(settings)


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _document_summary(document: Document) -> dict[str, Any]:
    return {
        "document_id": document.document_id,
        "title": document.title,
        "source_path": document.source_path,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "source_url": document.source_url,
        "content_hash": document.content_hash,
        "metadata": document.metadata,
        "loaded_at": document.loaded_at.isoformat(),
    }


@app.command()
def index(
    force: bool = typer.Option(False, "--force", help="Перестроить даже валидный кэш."),
) -> None:
    """Построить индекс и записать кэш."""
    stats = _service().build_index(force=force)
    typer.echo(
        f"Индекс готов: documents={stats.document_count}, chunks={stats.chunk_count}, "
        f"provider={stats.embedding_provider}, cache={stats.loaded_from_cache}"
    )


@app.command()
def search(
    query: str = typer.Argument(..., help="Поисковый запрос."),
    top_k: int = typer.Option(5, "--top-k", min=1, max=20),
    min_score: float | None = typer.Option(None, "--min-score"),
    service: str | None = typer.Option(None, "--service"),
    domain: str | None = typer.Option(None, "--domain"),
    document_type: str | None = typer.Option(None, "--document-type"),
    status: str | None = typer.Option("current", "--status"),
    authority: str | None = typer.Option(None, "--authority"),
    source_type: str | None = typer.Option(None, "--source-type"),
    json_output: bool = typer.Option(False, "--json", help="Вывести только JSON в stdout."),
) -> None:
    """Найти релевантные структурные чанки."""
    kb = _service()
    results = kb.search(
        query,
        top_k=top_k,
        min_score=min_score,
        filters=SearchFilters(
            service=service,
            domain=domain,
            document_type=document_type,
            status=status,
            authority=authority,
            source_type=source_type,
        ),
    )
    payload = [result.model_dump(mode="json") for result in results]
    if json_output:
        typer.echo(_json_dump(payload))
        return
    if not results:
        typer.echo("Ничего не найдено.")
        return
    for result in results:
        _print_result(result)


def _print_result(result: SearchResult) -> None:
    preview = " ".join(result.text.split())[:320]
    typer.echo(f"[{result.rank}] score={result.score:.4f} — {result.title}")
    typer.echo(f"    section: {result.heading_path}")
    typer.echo(f"    source:  {result.source_path}")
    if result.source_url:
        typer.echo(f"    url:     {result.source_url}")
    typer.echo(f"    metadata: {_json_dump(result.metadata)}")
    typer.echo(f"    {preview}")


@app.command()
def documents(
    service: str | None = typer.Option(None, "--service"),
    domain: str | None = typer.Option(None, "--domain"),
    document_type: str | None = typer.Option(None, "--document-type"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Показать проиндексированные документы без embeddings."""
    items = _service().list_documents(
        filters=SearchFilters(
            service=service,
            domain=domain,
            document_type=document_type,
            status=status,
        ),
        limit=limit,
    )
    payload = [_document_summary(item) for item in items]
    if json_output:
        typer.echo(_json_dump(payload))
        return
    for item in payload:
        typer.echo(f"{item['title']} — {item['source_path']} ({item['document_id']})")


@app.command()
def stats(json_output: bool = typer.Option(False, "--json")) -> None:
    """Показать состояние индекса и итоговые абсолютные пути."""
    kb = _service()
    payload = kb.stats().model_dump(mode="json")
    payload["knowledge_directory"] = str(kb.settings.knowledge_dir)
    payload["cache_directory"] = str(kb.settings.cache_dir)
    if json_output:
        typer.echo(_json_dump(payload))
        return
    for key, value in payload.items():
        typer.echo(f"{key}: {value}")


@app.command(name="eval")
def evaluate(
    top_k: int = typer.Option(5, "--top-k", min=1, max=20),
    questions: Annotated[Path, typer.Option("--questions")] = DEFAULT_QUESTIONS_PATH,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Рассчитать Hit@K на подготовленном наборе вопросов."""
    report = Evaluator(_service(), questions).evaluate(top_k=top_k)
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(f"Вопросов: {report.question_count}; top_k={report.top_k}")
    for metric, value in report.metrics.items():
        label = metric.replace("hit_at_", "Hit@")
        typer.echo(f"{label}: {value:.1f}%")
    for item in report.results:
        typer.echo(f"\n{item.question}")
        typer.echo(f"  retrieved: {', '.join(item.retrieved_source_paths)}")
        typer.echo(f"  hits: {item.hits}")


def main() -> None:
    """Console script boundary with practical errors and no stdout traceback."""
    try:
        app()
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    main()
