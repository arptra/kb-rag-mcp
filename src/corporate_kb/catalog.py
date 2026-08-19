"""Persistent registry for RAG indexes and Git/OpenSpec knowledge sources."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from corporate_kb.config import Settings
from corporate_kb.index_runner import IndexBuildCancelled, IndexBuildProcessRunner
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeIndexMissingError, KnowledgeService
from corporate_kb.usage import UsageTracker
from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphSnapshot
from gigacode_graph.service import GraphService
from gigacode_graph.sources import (
    RepositoryOperationCancelled,
    RepositorySourceManager,
    RepositorySpec,
)
from gigacode_graph.store import JsonGraphStore
from service_map import (
    JsonServiceMapStore,
    RepositoryInput,
    ServiceMapBuildCancelled,
    ServiceMapBuilder,
    ServiceMapProcessRunner,
    ServiceMapSnapshot,
)

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SUPPORTED_DOCUMENT_SUFFIXES = {".md", ".markdown", ".html", ".htm", ".txt"}
logger = logging.getLogger(__name__)


class CatalogJobCancelled(RuntimeError):
    """Internal control-flow exception for cooperative job cancellation."""


def _now() -> datetime:
    return datetime.now(UTC)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized[:48] or "index"


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RagIndex(CatalogModel):
    id: str
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=1000)
    kind: Literal["default", "managed"] = "managed"
    knowledge_dir: str
    cache_dir: str
    status: Literal["empty", "ready", "indexing", "error"] = "empty"
    document_count: int = 0
    chunk_count: int = 0
    source_count: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    error: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _ID_PATTERN.fullmatch(value):
            raise ValueError("Index id must contain lowercase letters, digits and hyphens")
        return value


class RepositorySource(CatalogModel):
    id: str
    name: str = Field(min_length=2, max_length=100)
    git_url: str
    ref: str | None = None
    index_id: str
    checkout_path: str
    openspec_path: str | None = None
    commit: str | None = None
    document_count: int = 0
    synced_at: datetime = Field(default_factory=_now)


class CatalogJob(CatalogModel):
    id: str
    type: Literal["index", "repository", "graph"]
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
    ] = "queued"
    index_id: str | None = None
    message: str = "Queued"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class CatalogState(CatalogModel):
    schema_version: int = 1
    indexes: list[RagIndex] = Field(default_factory=list)
    repositories: list[RepositorySource] = Field(default_factory=list)
    jobs: list[CatalogJob] = Field(default_factory=list)


class RagCatalog:
    """Own index runtimes and safely materialize OpenSpec sources from Git."""

    def __init__(
        self,
        settings: Settings,
        default_service: KnowledgeService,
        default_tools: KnowledgeTools,
        usage: UsageTracker,
    ) -> None:
        self.settings = settings
        self._default_service = default_service
        self._usage = usage
        self._lock = threading.RLock()
        self._work_lock = threading.Lock()
        self._state = self._load()
        self._services: dict[str, KnowledgeService] = {"default": default_service}
        self._tools: dict[str, KnowledgeTools] = {"default": default_tools}
        self._jobs: dict[str, CatalogJob] = {item.id: item for item in self._state.jobs}
        self._job_cancellations: dict[str, threading.Event] = {}
        self._graph_store = JsonGraphStore(settings.graph_store_path)
        self._service_map_store = JsonServiceMapStore(settings.service_map_path)
        if not settings.graph_store_path.is_file():
            self._graph_store.save(GraphSnapshot())
        self._graph_service = GraphService(self._graph_store)
        if not settings.service_map_path.is_file():
            try:
                repositories = self._repository_inputs(self._state.repositories)
                existing_graph = self._graph_store.load()
                result = ServiceMapBuilder(self._graph_settings()).from_graph(
                    existing_graph,
                    repositories,
                )
                self._service_map_store.save(result.service_map)
            except Exception:
                logger.exception("Could not initialize the service map from the stored graph")
                self._service_map_store.save(ServiceMapSnapshot())
        self._ensure_default_index()
        self._load_managed_services()
        self._recover_interrupted_jobs()

    def payload(self) -> dict[str, Any]:
        with self._lock:
            indexes = [item.model_copy(deep=True) for item in self._state.indexes]
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
            jobs = [item.model_copy(deep=True) for item in self._jobs.values()]
        repositories.sort(key=lambda item: item.synced_at, reverse=True)
        jobs.sort(key=lambda item: item.id, reverse=True)
        return {
            "index_count": len(indexes),
            "repository_count": len(repositories),
            "indexes": [item.model_dump(mode="json") for item in indexes],
            "repositories": [item.model_dump(mode="json") for item in repositories],
            "jobs": [item.model_dump(mode="json") for item in jobs[:30]],
        }

    def create_index(self, *, name: str, description: str = "") -> RagIndex:
        clean_name = name.strip()
        if len(clean_name) < 2:
            raise ValueError("Index name must contain at least two characters")
        digest = uuid.uuid4().hex[:8]
        index_id = f"{_slug(clean_name)}-{digest}"
        root = (self.settings.managed_indexes_dir / index_id).resolve()
        record = RagIndex(
            id=index_id,
            name=clean_name,
            description=description.strip(),
            knowledge_dir=str(root / "knowledge"),
            cache_dir=str(root / "cache"),
        )
        Path(record.knowledge_dir).mkdir(parents=True, exist_ok=False)
        service = self._create_service(record)
        stats = service.build_index(force=True)
        record.status = "ready"
        record.document_count = stats.document_count
        record.chunk_count = stats.chunk_count
        record.updated_at = _now()
        with self._lock:
            self._state.indexes.append(record)
            self._services[index_id] = service
            self._tools[index_id] = KnowledgeTools(service, usage=self._usage)
            self._save_locked()
        return record.model_copy(deep=True)

    def has_index(self, index_id: str) -> bool:
        with self._lock:
            return any(item.id == index_id for item in self._state.indexes)

    def tools_for(self, index_id: str) -> KnowledgeTools:
        with self._lock:
            tools = self._tools.get(index_id)
        if tools is None:
            raise KeyError(f"Unknown RAG index: {index_id}")
        return tools

    def service_for(self, index_id: str) -> KnowledgeService:
        with self._lock:
            service = self._services.get(index_id)
        if service is None:
            raise KeyError(f"Unknown RAG index: {index_id}")
        return service

    def start_index_build(self, index_id: str) -> CatalogJob:
        self._record(index_id)
        job = self._new_job("index", index_id=index_id, message="Index rebuild queued")
        threading.Thread(
            target=self._run_index_job,
            args=(job.id, index_id),
            daemon=True,
        ).start()
        return job

    def start_repository_ingestion(
        self,
        *,
        name: str,
        git_url: str,
        index_id: str | None,
        index_name: str | None = None,
        ref: str | None = None,
    ) -> CatalogJob:
        clean_name = name.strip()
        if len(clean_name) < 2:
            raise ValueError("Repository name must contain at least two characters")
        clean_url = git_url.strip()
        if not clean_url:
            raise ValueError("Git URL must not be empty")
        target_id = index_id
        if target_id is None:
            created = self.create_index(name=index_name or clean_name)
            target_id = created.id
        self._record(target_id)
        job = self._new_job(
            "repository",
            index_id=target_id,
            message="Repository import queued",
        )
        threading.Thread(
            target=self._run_repository_job,
            args=(job.id, clean_name, clean_url, ref.strip() if ref else None, target_id),
            daemon=True,
        ).start()
        return job

    def start_graph_build(self) -> CatalogJob:
        job = self._new_job("graph", message="Graph rebuild queued")
        threading.Thread(target=self._run_graph_job, args=(job.id,), daemon=True).start()
        return job

    def cancel_job(self, job_id: str) -> CatalogJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"Unknown job: {job_id}")
            if job.status in {"completed", "failed", "cancelled"}:
                return job.model_copy(deep=True)
            cancel_event = self._job_cancellations.setdefault(job_id, threading.Event())
            cancel_event.set()
            if job.status == "queued":
                replacement = job.model_copy(
                    update={
                        "status": "cancelled",
                        "message": "Cancelled before execution",
                        "completed_at": _now(),
                        "error": None,
                    }
                )
            else:
                replacement = job.model_copy(
                    update={"status": "cancelling", "message": "Cancellation requested"}
                )
            self._jobs[job_id] = replacement
            self._save_locked()
            return replacement.model_copy(deep=True)

    def graph_overview(self) -> dict[str, Any]:
        return self._graph_service.overview()

    def graph(self, *, view: str, service: str | None, depth: int, limit: int) -> dict[str, Any]:
        return self._graph_service.graph(
            view=view,
            service=service,
            depth=depth,
            limit=limit,
        )

    def graph_evidence(self, evidence_ids: list[str]) -> dict[str, Any]:
        return self._graph_service.evidence(evidence_ids)

    def service_map_overview(self) -> dict[str, object]:
        return self._service_map_store.load().overview()

    def service_map(self) -> dict[str, Any]:
        return self._service_map_store.load().model_dump(mode="json")

    def _run_index_job(self, job_id: str, index_id: str) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            with self._work_lock:
                self._raise_if_cancelled(cancel_event)
                self._execute_index_job(job_id, index_id, cancel_event)
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, index_id, "Index build failed", exc)
        finally:
            self._release_cancel_event(job_id)

    def _execute_index_job(
        self,
        job_id: str,
        index_id: str,
        cancel_event: threading.Event,
    ) -> None:
        self._update_job(job_id, status="running", message="Building embeddings", started_at=_now())
        self._update_index(index_id, status="indexing", error=None)
        try:
            self._raise_if_cancelled(cancel_event)
            service = self.service_for(index_id)
            IndexBuildProcessRunner(
                service.settings,
                timeout_seconds=self.settings.index_build_timeout_seconds,
            ).build(cancel=cancel_event)
            stats = service.reload_cached_index()
            self._update_index(
                index_id,
                status="ready",
                document_count=stats.document_count,
                chunk_count=stats.chunk_count,
                updated_at=_now(),
                error=None,
            )
            self._update_job(
                job_id,
                status="completed",
                message=f"Indexed {stats.document_count} documents",
                completed_at=_now(),
            )
        except (CatalogJobCancelled, IndexBuildCancelled):
            raise
        except Exception as exc:
            self._update_index(index_id, status="error", error=str(exc), updated_at=_now())
            self._update_job(
                job_id,
                status="failed",
                message="Index build failed",
                error=str(exc),
                completed_at=_now(),
            )

    def _run_repository_job(
        self,
        job_id: str,
        name: str,
        git_url: str,
        ref: str | None,
        index_id: str,
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            with self._work_lock:
                self._raise_if_cancelled(cancel_event)
                self._execute_repository_job(
                    job_id,
                    name,
                    git_url,
                    ref,
                    index_id,
                    cancel_event,
                )
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, index_id, "Repository import failed", exc)
        finally:
            self._release_cancel_event(job_id)

    def _execute_repository_job(
        self,
        job_id: str,
        name: str,
        git_url: str,
        ref: str | None,
        index_id: str,
        cancel_event: threading.Event,
    ) -> None:
        self._update_job(job_id, status="running", message="Cloning repository", started_at=_now())
        self._update_index(index_id, status="indexing", error=None)
        try:
            graph_settings = self._graph_settings()
            manager = RepositorySourceManager(graph_settings)
            paths, records = manager.materialize(
                [RepositorySpec(source=git_url, ref=ref)],
                refresh=True,
                cancel_event=cancel_event,
            )
            self._raise_if_cancelled(cancel_event)
            checkout = paths[0]
            ingestion = records[0]
            self._update_job(job_id, message="Reading OpenSpec and source interfaces")
            openspec = self._find_openspec(checkout, cancel_event=cancel_event)
            repository_id = self._repository_id(name, git_url, ref, index_id)
            document_count = self._sync_openspec(
                source=openspec,
                destination=Path(self._record(index_id).knowledge_dir)
                / "repositories"
                / repository_id
                / "openspec",
                cancel_event=cancel_event,
            )
            self._raise_if_cancelled(cancel_event)
            repository = RepositorySource(
                id=repository_id,
                name=name,
                git_url=git_url,
                ref=ref,
                index_id=index_id,
                checkout_path=str(checkout),
                openspec_path=str(openspec) if openspec else None,
                commit=ingestion.commit,
                document_count=document_count,
            )
            with self._lock:
                self._state.repositories = [
                    item for item in self._state.repositories if item.id != repository.id
                ]
                self._state.repositories.append(repository)
                self._refresh_source_counts_locked()
                self._save_locked()
            self._update_job(job_id, message=f"Building RAG index from {document_count} documents")
            self._raise_if_cancelled(cancel_event)
            service = self.service_for(index_id)
            IndexBuildProcessRunner(
                service.settings,
                timeout_seconds=self.settings.index_build_timeout_seconds,
            ).build(cancel=cancel_event)
            stats = service.reload_cached_index()
            self._update_index(
                index_id,
                status="ready",
                document_count=stats.document_count,
                chunk_count=stats.chunk_count,
                updated_at=_now(),
                error=None,
            )
            graph_note = ""
            try:
                self._update_job(job_id, message="Building service graph")
                self._build_graph(cancel_event=cancel_event)
                graph_note = " and refreshed the system graph"
            except (CatalogJobCancelled, ServiceMapBuildCancelled):
                raise
            except Exception as graph_exc:
                graph_note = f"; graph refresh needs attention: {graph_exc}"
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="completed",
                message=f"Imported {document_count} OpenSpec documents{graph_note}",
                completed_at=_now(),
            )
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            raise
        except Exception as exc:
            self._update_index(index_id, status="error", error=str(exc), updated_at=_now())
            self._update_job(
                job_id,
                status="failed",
                message="Repository import failed",
                error=str(exc),
                completed_at=_now(),
            )

    def _run_graph_job(self, job_id: str) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            with self._work_lock:
                self._raise_if_cancelled(cancel_event)
                self._execute_graph_job(job_id, cancel_event)
        except (CatalogJobCancelled, RepositoryOperationCancelled, ServiceMapBuildCancelled):
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            self._fail_background_job(job_id, None, "Graph build failed", exc)
        finally:
            self._release_cancel_event(job_id)

    def _execute_graph_job(self, job_id: str, cancel_event: threading.Event) -> None:
        self._update_job(
            job_id,
            status="running",
            message="Analyzing repositories",
            started_at=_now(),
        )
        try:
            snapshot = self._build_graph(cancel_event=cancel_event)
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="completed",
                message=f"Built {len(snapshot.nodes)} nodes and {len(snapshot.edges)} edges",
                completed_at=_now(),
            )
        except (CatalogJobCancelled, ServiceMapBuildCancelled):
            raise
        except Exception as exc:
            self._update_job(
                job_id,
                status="failed",
                message="Graph build failed",
                error=str(exc),
                completed_at=_now(),
            )

    def _build_graph(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> GraphSnapshot:
        with self._lock:
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
        inputs = self._repository_inputs(repositories)
        result = ServiceMapProcessRunner(
            self._graph_settings(),
            timeout_seconds=self.settings.repository_analysis_timeout_seconds,
        ).build(inputs, cancel=cancel_event)
        self._service_map_store.save(result.service_map)
        self._graph_store.save(result.graph)
        return result.graph

    @staticmethod
    def _repository_inputs(repositories: list[RepositorySource]) -> list[RepositoryInput]:
        return [
            RepositoryInput(
                path=Path(item.checkout_path),
                name=item.name,
                source_url=item.git_url,
                commit=item.commit,
            )
            for item in repositories
        ]

    def _find_openspec(
        self,
        checkout: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> Path | None:
        if cancel_event is not None:
            self._raise_if_cancelled(cancel_event)
        direct = checkout / "openspec"
        if direct.is_dir() and not direct.is_symlink():
            return direct.resolve()
        matches: list[Path] = []
        for current, directories, _files in os.walk(checkout, followlinks=False):
            if cancel_event is not None:
                self._raise_if_cancelled(cancel_event)
            root = Path(current)
            directories[:] = [
                item
                for item in directories
                if not item.startswith(".")
                and item not in {"node_modules", "vendor", "build", "dist", "target"}
                and not (root / item).is_symlink()
            ]
            for directory in directories:
                if directory.lower() == "openspec":
                    matches.append((root / directory).resolve())
            if matches:
                break
        if not matches:
            return None
        return sorted(matches, key=lambda item: (len(item.parts), str(item)))[0]

    def _sync_openspec(
        self,
        *,
        source: Path | None,
        destination: Path,
        cancel_event: threading.Event | None = None,
    ) -> int:
        source = source.resolve() if source else None
        files: list[Path] = []
        if source is not None:
            for current, directories, names in os.walk(source, followlinks=False):
                if cancel_event is not None:
                    self._raise_if_cancelled(cancel_event)
                root = Path(current)
                directories[:] = [
                    item
                    for item in directories
                    if not item.startswith(".") and not (root / item).is_symlink()
                ]
                for name in names:
                    path = root / name
                    if (
                        not name.startswith(".")
                        and path.is_file()
                        and not path.is_symlink()
                        and path.suffix.lower() in _SUPPORTED_DOCUMENT_SUFFIXES
                        and path.resolve().is_relative_to(source)
                    ):
                        files.append(path)
        if len(files) > self.settings.repository_max_files:
            raise ValueError(
                f"OpenSpec contains {len(files)} documents; maximum is "
                f"{self.settings.repository_max_files}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        try:
            for path in files:
                if cancel_event is not None:
                    self._raise_if_cancelled(cancel_event)
                assert source is not None
                relative = path.relative_to(source)
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            if destination.exists():
                shutil.rmtree(destination)
            temporary.replace(destination)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return len(files)

    def _ensure_default_index(self) -> None:
        try:
            stats = self._default_service.stats()
            status: Literal["empty", "ready", "indexing", "error"] = "ready"
        except RuntimeError:
            stats = None
            status = "empty"
        with self._lock:
            current = next((item for item in self._state.indexes if item.id == "default"), None)
            source_count = sum(item.index_id == "default" for item in self._state.repositories)
            replacement = RagIndex(
                id="default",
                name="Corporate knowledge",
                description="Primary knowledge index configured by KB_KNOWLEDGE_DIR.",
                kind="default",
                knowledge_dir=str(self.settings.knowledge_dir),
                cache_dir=str(self.settings.cache_dir),
                status=status,
                document_count=stats.document_count if stats else 0,
                chunk_count=stats.chunk_count if stats else 0,
                source_count=source_count,
                created_at=current.created_at if current else _now(),
                updated_at=stats.indexed_at if stats else _now(),
            )
            self._state.indexes = [
                replacement,
                *(item for item in self._state.indexes if item.id != "default"),
            ]

    def _load_managed_services(self) -> None:
        for record in self._state.indexes:
            if record.id == "default":
                continue
            service = self._create_service(record)
            try:
                service.load_cached_index()
            except KnowledgeIndexMissingError:
                record.status = "empty"
            self._services[record.id] = service
            self._tools[record.id] = KnowledgeTools(service, usage=self._usage)

    def _create_service(self, record: RagIndex) -> KnowledgeService:
        settings = self.settings.model_copy(
            update={
                "knowledge_dir": Path(record.knowledge_dir),
                "cache_dir": Path(record.cache_dir),
                "auto_index": False,
            }
        )
        return KnowledgeService(settings, provider=self._default_service.provider)

    def _record(self, index_id: str) -> RagIndex:
        with self._lock:
            record = next((item for item in self._state.indexes if item.id == index_id), None)
        if record is None:
            raise KeyError(f"Unknown RAG index: {index_id}")
        return record

    def _new_job(
        self,
        job_type: Literal["index", "repository", "graph"],
        *,
        index_id: str | None = None,
        message: str,
    ) -> CatalogJob:
        job = CatalogJob(
            id=f"{int(_now().timestamp())}-{uuid.uuid4().hex[:8]}",
            type=job_type,
            index_id=index_id,
            message=message,
        )
        with self._lock:
            self._jobs[job.id] = job
            self._job_cancellations[job.id] = threading.Event()
            self._save_locked()
        return job.model_copy(deep=True)

    def _cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._job_cancellations.setdefault(job_id, threading.Event())

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise CatalogJobCancelled("Operation was cancelled")

    def _release_cancel_event(self, job_id: str) -> None:
        with self._lock:
            self._job_cancellations.pop(job_id, None)

    def _finish_cancelled_job(self, job_id: str, index_id: str | None) -> None:
        if index_id is not None:
            try:
                stats = self.service_for(index_id).stats()
                self._update_index(
                    index_id,
                    status="ready",
                    document_count=stats.document_count,
                    chunk_count=stats.chunk_count,
                    error=None,
                    updated_at=_now(),
                )
            except Exception:
                self._update_index(
                    index_id,
                    status="empty",
                    error=None,
                    updated_at=_now(),
                )
        self._update_job(
            job_id,
            status="cancelled",
            message="Operation cancelled",
            error=None,
            completed_at=_now(),
        )

    def _update_job(self, job_id: str, **values: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            self._jobs[job_id] = job.model_copy(update=values)
            self._save_locked()

    def _fail_background_job(
        self,
        job_id: str,
        index_id: str | None,
        message: str,
        exc: Exception,
    ) -> None:
        logger.exception("Background catalog job %s failed", job_id, exc_info=exc)
        error = str(exc) or type(exc).__name__
        try:
            if index_id is not None:
                self._update_index(index_id, status="error", error=error, updated_at=_now())
            self._update_job(
                job_id,
                status="failed",
                message=message,
                error=error,
                completed_at=_now(),
            )
        except Exception:
            logger.exception("Could not persist failure for catalog job %s", job_id)

    def _recover_interrupted_jobs(self) -> None:
        interrupted = [
            job
            for job in self._jobs.values()
            if job.status in {"queued", "running", "cancelling"}
        ]
        if not interrupted:
            return
        completed_at = _now()
        for job in interrupted:
            self._jobs[job.id] = job.model_copy(
                update={
                    "status": "failed",
                    "message": "Interrupted by server restart",
                    "error": "The server restarted before this operation completed; run it again.",
                    "completed_at": completed_at,
                }
            )
            if job.index_id is not None:
                record = next(
                    (item for item in self._state.indexes if item.id == job.index_id),
                    None,
                )
                if record is not None and record.status == "indexing":
                    replacement = record.model_copy(
                        update={
                            "status": "error",
                            "error": "Indexing was interrupted by a server restart",
                            "updated_at": completed_at,
                        }
                    )
                    self._state.indexes = [
                        replacement if item.id == record.id else item
                        for item in self._state.indexes
                    ]
        with self._lock:
            self._save_locked()

    def _update_index(self, index_id: str, **values: Any) -> None:
        with self._lock:
            record = self._record(index_id)
            replacement = record.model_copy(update=values)
            self._state.indexes = [
                replacement if item.id == index_id else item for item in self._state.indexes
            ]
            self._save_locked()

    def _refresh_source_counts_locked(self) -> None:
        counts: dict[str, int] = {}
        for source in self._state.repositories:
            counts[source.index_id] = counts.get(source.index_id, 0) + 1
        self._state.indexes = [
            item.model_copy(update={"source_count": counts.get(item.id, 0)})
            for item in self._state.indexes
        ]

    def _graph_settings(self) -> GraphSettings:
        return GraphSettings(
            store_path=self.settings.graph_store_path,
            repository_cache_path=self.settings.repository_cache_dir,
            ingestion_path=self.settings.repository_cache_dir.parent / "graph-ingestion.json",
            git_timeout_seconds=self.settings.repository_git_timeout_seconds,
        ).resolved()

    @staticmethod
    def _repository_id(name: str, git_url: str, ref: str | None, index_id: str) -> str:
        digest = hashlib.sha256(
            f"{git_url}\x1f{ref or ''}\x1f{index_id}".encode()
        ).hexdigest()[:10]
        return f"{_slug(name)}-{digest}"

    def _load(self) -> CatalogState:
        path = self.settings.index_catalog_path
        if not path.is_file():
            return CatalogState()
        try:
            return CatalogState.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(f"RAG index catalog is invalid: {exc}") from exc

    def _save_locked(self) -> None:
        path = self.settings.index_catalog_path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._state.jobs = sorted(self._jobs.values(), key=lambda item: item.id)[-100:]
        payload = self._state.model_dump_json(indent=2).encode("utf-8")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
