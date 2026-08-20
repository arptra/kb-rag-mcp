"""Persistent registry for RAG indexes and Git/OpenSpec knowledge sources."""

from __future__ import annotations

import hashlib
import inspect
import logging
import os
import re
import shutil
import tempfile
import threading
import traceback
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
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
    AnalysisArchive,
    JsonServiceMapStore,
    RepositoryInput,
    ServiceMapBuildCancelled,
    ServiceMapBuilder,
    ServiceMapBuildResult,
    ServiceMapProcessRunner,
    ServiceMapSnapshot,
)
from service_map.layout import RepositoryLayoutAnalyzer
from service_map.models import ServiceRecord

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
    openspec_paths: list[str] = Field(default_factory=list)
    commit: str | None = None
    document_count: int = 0
    synced_at: datetime = Field(default_factory=_now)


class CatalogJob(CatalogModel):
    id: str
    type: Literal["index", "repository", "graph", "service", "cleanup"]
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
    ] = "queued"
    index_id: str | None = None
    target_id: str | None = None
    message: str = "Queued"
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    log_path: str | None = None


class ServiceExclusion(CatalogModel):
    repository_id: str
    module_path: str
    service_id: str
    created_at: datetime = Field(default_factory=_now)


class CatalogState(CatalogModel):
    schema_version: int = 1
    indexes: list[RagIndex] = Field(default_factory=list)
    repositories: list[RepositorySource] = Field(default_factory=list)
    service_exclusions: list[ServiceExclusion] = Field(default_factory=list)
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
        self._analysis_lock = threading.Lock()
        self._index_work_locks: dict[str, threading.Lock] = {}
        self._state = self._load()
        self._services: dict[str, KnowledgeService] = {"default": default_service}
        self._tools: dict[str, KnowledgeTools] = {"default": default_tools}
        self._jobs: dict[str, CatalogJob] = {item.id: item for item in self._state.jobs}
        self._job_cancellations: dict[str, threading.Event] = {}
        self._graph_store = JsonGraphStore(settings.graph_store_path)
        self._service_map_store = JsonServiceMapStore(settings.service_map_path)
        self._analysis_archive = AnalysisArchive(
            settings.analysis_archive_dir,
            settings.ssot_skill_path,
        )
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
            "analysis": self._analysis_archive.overview(),
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

    def start_service_analysis(self, service_id: str) -> CatalogJob:
        service, repository = self._service_context(service_id)
        job = self._new_job(
            "service",
            index_id=repository.index_id,
            target_id=service.id,
            message=f"Service analysis queued: {service.name}",
        )
        threading.Thread(
            target=self._run_service_analysis_job,
            args=(job.id, service.id, service.name, repository.index_id),
            daemon=True,
        ).start()
        return job

    def start_repository_delete(self, repository_id: str) -> CatalogJob:
        repository = self._repository(repository_id)
        job = self._new_job(
            "cleanup",
            index_id=repository.index_id,
            target_id=repository.id,
            message=f"Repository deletion queued: {repository.name}",
        )
        threading.Thread(
            target=self._run_repository_delete_job,
            args=(job.id, repository.id, repository.index_id),
            daemon=True,
        ).start()
        return job

    def start_service_delete(self, service_id: str) -> CatalogJob:
        service, repository = self._service_context(service_id)
        job = self._new_job(
            "cleanup",
            index_id=repository.index_id,
            target_id=service.id,
            message=f"Service deletion queued: {service.name}",
        )
        threading.Thread(
            target=self._run_service_delete_job,
            args=(job.id, service.id, repository.id, service.module_path),
            daemon=True,
        ).start()
        return job

    def job_log(self, job_id: str) -> dict[str, str]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        path = self._job_log_path(job_id)
        return {
            "job_id": job_id,
            "status": job.status,
            "log": path.read_text(encoding="utf-8") if path.is_file() else "",
        }

    def create_ssot_bundle(self, service_id: str) -> dict[str, str]:
        self._service_context(service_id)
        return self._analysis_archive.create_bundle(service_id)

    def ssot_bundle_path(self, bundle_id: str) -> Path:
        return self._analysis_archive.bundle_path(bundle_id)

    def import_ssot(self, *, service_id: str, index_id: str, content: str) -> dict[str, Any]:
        service, _repository = self._service_context(service_id)
        record = self._record(index_id)
        clean_content = content.strip()
        if len(clean_content) < 100:
            raise ValueError("SSOT document must contain at least 100 characters")
        if len(clean_content.encode("utf-8")) > self.settings.admin_max_upload_bytes:
            raise ValueError("SSOT document exceeds the configured upload limit")
        knowledge_root = Path(record.knowledge_dir).resolve()
        destination = (knowledge_root / "ssot" / f"{_slug(service.id)}.md").resolve()
        if not destination.is_relative_to(knowledge_root):
            raise ValueError("Invalid SSOT destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".md.tmp")
        temporary.write_text(clean_content + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        job = self.start_index_build(index_id)
        return {
            "service_id": service.id,
            "index_id": index_id,
            "path": str(destination),
            "job": job.model_dump(mode="json"),
        }

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
        self._append_job_log(job_id, f"{replacement.status}: {replacement.message}")
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

    def graph_search(self, query: str, *, limit: int = 50) -> dict[str, Any]:
        return self._graph_service.search(query, limit=limit)

    def graph_business_operations(
        self,
        service: str,
        *,
        limit: int = 500,
    ) -> dict[str, Any]:
        return self._graph_service.business_operations(service, limit=limit)

    def service_map_overview(self) -> dict[str, object]:
        return self._service_map_store.load().overview()

    def service_map(self) -> dict[str, Any]:
        return self._service_map_store.load().model_dump(mode="json")

    def _run_index_job(self, job_id: str, index_id: str) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            with self._cancellable_lock(self._index_work_lock(index_id), cancel_event):
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
            with self._cancellable_lock(self._index_work_lock(index_id), cancel_event):
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
        graph_settings = self._graph_settings()
        try:
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
            openspecs = self._find_openspecs(checkout, cancel_event=cancel_event)
            repository_id = self._repository_id(name, git_url, ref, index_id)
            document_count = self._sync_openspec(
                sources=openspecs,
                checkout=checkout,
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
                openspec_path=str(openspecs[0]) if openspecs else None,
                openspec_paths=[str(path) for path in openspecs],
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
            self._update_job(job_id, message="Building service graph")
            self._build_graph(cancel_event=cancel_event, job_id=job_id)
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="completed",
                message=(
                    f"Imported {document_count} OpenSpec documents and refreshed the system graph"
                ),
                completed_at=_now(),
            )
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            raise
        except Exception:
            self._update_index(
                index_id,
                status="error",
                error="Repository import failed; open the job log for the full traceback",
                updated_at=_now(),
            )
            raise

    def _run_graph_job(self, job_id: str) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
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
        snapshot = self._build_graph(
            cancel_event=cancel_event,
            job_id=job_id,
            force_all=True,
        )
        self._raise_if_cancelled(cancel_event)
        partial = self._is_partial_analysis(snapshot)
        self._update_job(
            job_id,
            status="completed",
            message=(
                f"Published partial map with {len(snapshot.nodes)} nodes"
                if partial
                else f"Built {len(snapshot.nodes)} nodes and {len(snapshot.edges)} edges"
            ),
            completed_at=_now(),
        )

    def _run_service_analysis_job(
        self,
        job_id: str,
        service_id: str,
        service_name: str,
        index_id: str,
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="running",
                message=f"Reanalyzing service: {service_name}",
                started_at=_now(),
            )
            snapshot = self._build_graph(
                cancel_event=cancel_event,
                job_id=job_id,
                force_service_ids={service_id},
            )
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="completed",
                message=f"Service analysis completed: {service_name}",
                completed_at=_now(),
            )
            self._append_job_log(
                job_id,
                (f"Snapshot contains {len(snapshot.nodes)} nodes and {len(snapshot.edges)} edges"),
            )
        except (CatalogJobCancelled, RepositoryOperationCancelled, ServiceMapBuildCancelled):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, None, f"Service analysis failed: {service_id}", exc)
        finally:
            self._release_cancel_event(job_id)

    def _run_repository_delete_job(
        self,
        job_id: str,
        repository_id: str,
        index_id: str,
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            with self._cancellable_lock(self._index_work_lock(index_id), cancel_event):
                self._raise_if_cancelled(cancel_event)
                repository = self._repository(repository_id)
                self._update_job(
                    job_id,
                    status="running",
                    message=f"Deleting repository: {repository.name}",
                    started_at=_now(),
                )
                self._delete_repository_documents(repository, job_id=job_id)
                with self._lock:
                    self._state.repositories = [
                        item for item in self._state.repositories if item.id != repository.id
                    ]
                    self._state.service_exclusions = [
                        item
                        for item in self._state.service_exclusions
                        if item.repository_id != repository.id
                    ]
                    self._refresh_source_counts_locked()
                    self._save_locked()
                self._update_job(job_id, message="Rebuilding service map after deletion")
                self._build_graph(cancel_event=cancel_event, job_id=job_id)
                self._update_job(job_id, message="Rebuilding RAG index after deletion")
                self._update_index(index_id, status="indexing", error=None)
                IndexBuildProcessRunner(
                    self.service_for(index_id).settings,
                    timeout_seconds=self.settings.index_build_timeout_seconds,
                ).build(cancel=cancel_event)
                stats = self.service_for(index_id).reload_cached_index()
                self._update_index(
                    index_id,
                    status="ready",
                    document_count=stats.document_count,
                    chunk_count=stats.chunk_count,
                    updated_at=_now(),
                    error=None,
                )
                self._delete_managed_checkout_if_unused(repository)
                self._update_job(
                    job_id,
                    status="completed",
                    message=f"Repository deleted: {repository.name}",
                    completed_at=_now(),
                )
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, index_id, "Repository deletion failed", exc)
        finally:
            self._release_cancel_event(job_id)

    def _run_service_delete_job(
        self,
        job_id: str,
        service_id: str,
        repository_id: str,
        module_path: str,
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        try:
            self._raise_if_cancelled(cancel_event)
            self._update_job(
                job_id,
                status="running",
                message=f"Removing service from map: {service_id}",
                started_at=_now(),
            )
            exclusion = ServiceExclusion(
                repository_id=repository_id,
                module_path=module_path,
                service_id=service_id,
            )
            with self._lock:
                self._state.service_exclusions = [
                    item
                    for item in self._state.service_exclusions
                    if not (item.repository_id == repository_id and item.module_path == module_path)
                ]
                self._state.service_exclusions.append(exclusion)
                self._save_locked()
            self._build_graph(cancel_event=cancel_event, job_id=job_id)
            self._update_job(
                job_id,
                status="completed",
                message=f"Service removed from map: {service_id}",
                completed_at=_now(),
            )
        except (CatalogJobCancelled, RepositoryOperationCancelled, ServiceMapBuildCancelled):
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            self._fail_background_job(job_id, None, "Service deletion failed", exc)
        finally:
            self._release_cancel_event(job_id)

    def _build_graph(
        self,
        *,
        cancel_event: threading.Event | None = None,
        job_id: str | None = None,
        force_service_ids: set[str] | None = None,
        force_all: bool = False,
    ) -> GraphSnapshot:
        if job_id is not None and self._analysis_lock.locked():
            self._append_job_log(job_id, "Waiting for another graph analysis to finish")
        with self._cancellable_lock(self._analysis_lock, cancel_event):
            return self._build_graph_unlocked(
                cancel_event=cancel_event,
                job_id=job_id,
                force_service_ids=force_service_ids,
                force_all=force_all,
            )

    def _build_graph_unlocked(
        self,
        *,
        cancel_event: threading.Event | None,
        job_id: str | None,
        force_service_ids: set[str] | None,
        force_all: bool,
    ) -> GraphSnapshot:
        with self._lock:
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
        if job_id is not None:
            self._append_job_log(job_id, f"Analyzing {len(repositories)} connected repositories")
        inputs = self._repository_inputs(repositories)
        runner = ServiceMapProcessRunner(
            self._graph_settings(),
            timeout_seconds=self.settings.repository_analysis_timeout_seconds,
        )
        build_options: dict[str, Any] = {}
        build_parameters = inspect.signature(runner.build).parameters
        if "force_service_ids" in build_parameters:
            build_options["force_service_ids"] = force_service_ids
        if "force_all" in build_parameters:
            build_options["force_all"] = force_all
        result = runner.build(
            inputs,
            cancel=cancel_event,
            progress=(
                (lambda message: self._append_job_log(job_id, message))
                if job_id is not None
                else None
            ),
            checkpoint=self._publish_analysis_checkpoint,
            **build_options,
        )
        self._service_map_store.save(result.service_map)
        self._graph_store.save(result.graph)
        manifest = self._analysis_archive.record(
            result.service_map,
            result.graph,
            job_id=job_id,
            repository_count=len(repositories),
        )
        if job_id is not None:
            if result.partial:
                self._append_job_log(
                    job_id,
                    "Analysis is partial; the latest discovered services were published",
                )
            self._append_job_log(job_id, f"Analysis archived at {manifest['path']}")
        return result.graph

    def _index_work_lock(self, index_id: str) -> threading.Lock:
        with self._lock:
            lock = self._index_work_locks.get(index_id)
            if lock is None:
                lock = threading.Lock()
                self._index_work_locks[index_id] = lock
            return lock

    @contextmanager
    def _cancellable_lock(
        self,
        lock: threading.Lock,
        cancel_event: threading.Event | None,
    ) -> Iterator[None]:
        while not lock.acquire(timeout=0.1):
            self._raise_if_cancelled(cancel_event)
        try:
            self._raise_if_cancelled(cancel_event)
            yield
        finally:
            lock.release()

    def _publish_analysis_checkpoint(self, result: ServiceMapBuildResult) -> None:
        """Expose discovered services while deeper source parsing is still running."""
        self._service_map_store.save(result.service_map)
        self._graph_store.save(result.graph)

    @staticmethod
    def _is_partial_analysis(snapshot: GraphSnapshot) -> bool:
        return any(
            issue.message.startswith("Partial analysis checkpoint:") for issue in snapshot.issues
        )

    def _repository_inputs(self, repositories: list[RepositorySource]) -> list[RepositoryInput]:
        exclusions: dict[str, list[str]] = {}
        with self._lock:
            for item in self._state.service_exclusions:
                exclusions.setdefault(item.repository_id, []).append(item.module_path)
        return [
            RepositoryInput(
                path=Path(item.checkout_path),
                name=item.name,
                source_url=item.git_url,
                commit=item.commit,
                excluded_module_paths=tuple(sorted(exclusions.get(item.id, []))),
            )
            for item in repositories
        ]

    def _find_openspecs(
        self,
        checkout: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[Path]:
        if cancel_event is not None:
            self._raise_if_cancelled(cancel_event)
        layout = RepositoryLayoutAnalyzer().discover(checkout)
        if cancel_event is not None:
            self._raise_if_cancelled(cancel_event)
        return list(layout.openspec_roots)

    def _sync_openspec(
        self,
        *,
        sources: list[Path],
        checkout: Path,
        destination: Path,
        cancel_event: threading.Event | None = None,
    ) -> int:
        checkout = checkout.resolve()
        files: list[tuple[Path, Path]] = []
        for source_value in sources:
            source = source_value.resolve()
            if not source.is_relative_to(checkout):
                raise ValueError(f"OpenSpec root is outside repository checkout: {source}")
            module_prefix = (
                Path() if source.parent == checkout else source.parent.relative_to(checkout)
            )
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
                        files.append((path, module_prefix / path.relative_to(source)))
        if len(files) > self.settings.repository_max_files:
            raise ValueError(
                f"OpenSpec contains {len(files)} documents; maximum is "
                f"{self.settings.repository_max_files}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
        try:
            copied: set[Path] = set()
            for path, relative in sorted(files, key=lambda item: str(item[1])):
                if cancel_event is not None:
                    self._raise_if_cancelled(cancel_event)
                if relative in copied:
                    raise ValueError(f"OpenSpec document path collision: {relative}")
                copied.add(relative)
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

    def _repository(self, repository_id: str) -> RepositorySource:
        with self._lock:
            repository = next(
                (item for item in self._state.repositories if item.id == repository_id),
                None,
            )
        if repository is None:
            raise KeyError(f"Unknown repository: {repository_id}")
        return repository.model_copy(deep=True)

    def _service_context(self, service_id: str) -> tuple[ServiceRecord, RepositorySource]:
        service = next(
            (item for item in self._service_map_store.load().services if item.id == service_id),
            None,
        )
        if service is None:
            raise KeyError(f"Unknown service: {service_id}")
        with self._lock:
            repository = next(
                (
                    item
                    for item in self._state.repositories
                    if str(Path(item.checkout_path).resolve()) == service.repository_root
                ),
                None,
            )
            if repository is None:
                repository = next(
                    (item for item in self._state.repositories if item.name == service.repository),
                    None,
                )
        if repository is None:
            raise KeyError(f"No connected repository owns service: {service_id}")
        return service.model_copy(deep=True), repository.model_copy(deep=True)

    def _delete_repository_documents(
        self,
        repository: RepositorySource,
        *,
        job_id: str,
    ) -> None:
        knowledge_root = Path(self._record(repository.index_id).knowledge_dir).resolve()
        target = (knowledge_root / "repositories" / repository.id).resolve()
        if not target.is_relative_to(knowledge_root):
            raise RuntimeError("Refusing to delete repository documents outside knowledge root")
        if target.exists():
            self._append_job_log(job_id, f"Deleting indexed documents: {target}")
            shutil.rmtree(target)

    def _delete_managed_checkout_if_unused(self, repository: RepositorySource) -> None:
        checkout = Path(repository.checkout_path).resolve()
        with self._lock:
            still_used = any(
                Path(item.checkout_path).resolve() == checkout for item in self._state.repositories
            )
        cache_root = self.settings.repository_cache_dir.resolve()
        marker = checkout / ".gigacode-graph-source.json"
        if still_used or not checkout.is_relative_to(cache_root) or not marker.is_file():
            return
        shutil.rmtree(checkout)

    def _new_job(
        self,
        job_type: Literal["index", "repository", "graph", "service", "cleanup"],
        *,
        index_id: str | None = None,
        target_id: str | None = None,
        message: str,
    ) -> CatalogJob:
        job_id = f"{int(_now().timestamp())}-{uuid.uuid4().hex[:8]}"
        job = CatalogJob(
            id=job_id,
            type=job_type,
            index_id=index_id,
            target_id=target_id,
            message=message,
            log_path=str(self._job_log_path(job_id)),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._job_cancellations[job.id] = threading.Event()
            self._save_locked()
        self._append_job_log(job.id, f"{job.status}: {job.message}")
        return job.model_copy(deep=True)

    def _cancel_event(self, job_id: str) -> threading.Event:
        with self._lock:
            return self._job_cancellations.setdefault(job_id, threading.Event())

    @staticmethod
    def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
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
        parts = []
        if "status" in values:
            parts.append(str(values["status"]))
        if "message" in values:
            parts.append(str(values["message"]))
        if values.get("error"):
            parts.append(f"error={values['error']}")
        if parts:
            self._append_job_log(job_id, ": ".join(parts))

    def _fail_background_job(
        self,
        job_id: str,
        index_id: str | None,
        message: str,
        exc: Exception,
    ) -> None:
        logger.exception("Background catalog job %s failed", job_id, exc_info=exc)
        error = str(exc) or type(exc).__name__
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._append_job_log(job_id, details.rstrip())
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

    def _job_log_path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9-]+", job_id):
            raise ValueError("Invalid job id")
        return self.settings.job_logs_dir / f"{job_id}.log"

    def _append_job_log(self, job_id: str, message: str) -> None:
        path = self._job_log_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = _now().isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{timestamp}] {message}\n")

    def _recover_interrupted_jobs(self) -> None:
        interrupted = [
            job for job in self._jobs.values() if job.status in {"queued", "running", "cancelling"}
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
            self._append_job_log(
                job.id,
                "failed: interrupted by server restart before the operation completed",
            )
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
            module_cache_path=self.settings.repository_cache_dir.parent / "module-analysis",
            ingestion_path=self.settings.repository_cache_dir.parent / "graph-ingestion.json",
            git_timeout_seconds=self.settings.repository_git_timeout_seconds,
        ).resolved()

    @staticmethod
    def _repository_id(name: str, git_url: str, ref: str | None, index_id: str) -> str:
        digest = hashlib.sha256(f"{git_url}\x1f{ref or ''}\x1f{index_id}".encode()).hexdigest()[:10]
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
