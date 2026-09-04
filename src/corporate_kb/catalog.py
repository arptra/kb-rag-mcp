"""Persistent registry for RAG indexes and Git/OpenSpec knowledge sources."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import traceback
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from corporate_kb.config import Settings
from corporate_kb.gigacode_runner import GigaCodeCancelled, GigaCodeRunner
from corporate_kb.graph_verifier import GraphGigaCodeVerifier
from corporate_kb.index_runner import IndexBuildCancelled, IndexBuildProcessRunner
from corporate_kb.loaders.filesystem import SUPPORTED_DOCUMENT_SUFFIXES
from corporate_kb.mcp.tools import KnowledgeTools
from corporate_kb.service import KnowledgeIndexMissingError, KnowledgeService
from corporate_kb.usage import UsageTracker
from gigacode_graph.algorithms import get_graph_algorithm, registry
from gigacode_graph.config import GraphSettings
from gigacode_graph.models import GraphSnapshot
from gigacode_graph.scanner import merge_and_relink_snapshots
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
    finalize_snapshot,
)
from service_map.models import ServiceRecord

_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SSOT_IGNORED_DIRECTORIES = {
    ".git",
    ".gradle",
    ".idea",
    ".mvn",
    ".settings",
    "build",
    "dist",
    "generated",
    "node_modules",
    "out",
    "target",
    "vendor",
}
_SSOT_READABLE_SUFFIXES = {
    ".conf",
    ".gradle",
    ".graphql",
    ".gql",
    ".java",
    ".json",
    ".kt",
    ".kts",
    ".md",
    ".properties",
    ".proto",
    ".sql",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SSOT_READABLE_NAMES = {
    "dockerfile",
    "jenkinsfile",
    "makefile",
    "pom.xml",
    "settings.gradle",
    "settings.gradle.kts",
}
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
    checkout_state: Literal["available", "removed", "external"] = "available"
    checkout_removed_at: datetime | None = None
    documentation_path: str | None = None
    openspec_path: str | None = None
    openspec_paths: list[str] = Field(default_factory=list)
    commit: str | None = None
    document_count: int = 0
    synced_at: datetime = Field(default_factory=_now)


class RepositoryBatchItem(CatalogModel):
    name: str = Field(min_length=2, max_length=100)
    git_url: str = Field(min_length=1)
    ref: str | None = None
    index_id: str


class CatalogJob(CatalogModel):
    id: str
    type: Literal["index", "repository", "graph", "service", "ssot", "cleanup"]
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
    result: dict[str, Any] | None = None


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
        self._all_services_refresh_lock = threading.Lock()
        self._index_work_locks: dict[str, threading.Lock] = {}
        self._repository_import_reservations: set[str] = set()
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
        self._gigacode = GigaCodeRunner(settings)
        self._graph_verifier = GraphGigaCodeVerifier(
            self._gigacode,
            self._graph_settings(),
            settings.analysis_archive_dir,
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
            "ssot_generation": self._ssot_workflow_status(),
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
        generation_mode: Literal["static", "gigacode"] = "static",
        validate_gigacode: bool = True,
    ) -> CatalogJob:
        clean_name = name.strip()
        if len(clean_name) < 2:
            raise ValueError("Repository name must contain at least two characters")
        clean_url = git_url.strip()
        if not clean_url:
            raise ValueError("Git URL must not be empty")
        source_key = self._repository_source_key(clean_url)
        with self._lock:
            existing = self._repository_for_source_key_locked(source_key)
            if existing is not None:
                raise ValueError(
                    f"Git-репозиторий уже подключён: {existing.name}"
                )
            if source_key in self._repository_import_reservations:
                raise ValueError("Этот Git-репозиторий уже подключается")
            self._repository_import_reservations.add(source_key)

        try:
            if generation_mode == "gigacode" and validate_gigacode:
                gigacode_status = self._gigacode.status(refresh=True)
                if not gigacode_status["available"]:
                    raise RuntimeError(str(gigacode_status["error"]))
            target_id = index_id
            if target_id is None:
                created = self.create_index(name=index_name or clean_name)
                target_id = created.id
            self._record(target_id)
            clean_ref = ref.strip() if ref else None
            repository_id = self._repository_id(clean_name, clean_url, clean_ref, target_id)
            job = self._new_job(
                "repository",
                index_id=target_id,
                target_id=repository_id,
                message=(
                    "Repository import with GigaCode queued"
                    if generation_mode == "gigacode"
                    else "Repository import queued"
                ),
            )
            threading.Thread(
                target=self._run_repository_job,
                args=(job.id, clean_name, clean_url, clean_ref, target_id, generation_mode),
                daemon=True,
            ).start()
            return job
        except Exception:
            self._release_repository_import_reservation(clean_url)
            raise

    def start_repository_batch_ingestion(
        self,
        *,
        repositories: list[RepositoryBatchItem],
        worker_count: int = 4,
        generation_mode: Literal["static", "gigacode"] = "static",
        validate_gigacode: bool = True,
    ) -> CatalogJob:
        if not repositories:
            raise ValueError("Repository batch must contain at least one repository")
        if len(repositories) > 1000:
            raise ValueError("Repository batch must contain at most 1000 repositories")
        if not 1 <= worker_count <= 16:
            raise ValueError("worker_count must be between 1 and 16")
        if generation_mode == "gigacode" and validate_gigacode:
            gigacode_status = self._gigacode.status(refresh=True)
            if not gigacode_status["available"]:
                raise RuntimeError(str(gigacode_status["error"]))

        cleaned: list[RepositoryBatchItem] = []
        for item in repositories:
            name = item.name.strip()
            git_url = item.git_url.strip()
            ref = item.ref.strip() if item.ref else None
            if len(name) < 2:
                raise ValueError("Repository name must contain at least two characters")
            if not git_url:
                raise ValueError("Git URL must not be empty")
            self._record(item.index_id)
            cleaned.append(
                RepositoryBatchItem(
                    name=name,
                    git_url=git_url,
                    ref=ref,
                    index_id=item.index_id,
                )
            )

        scheduled: list[RepositoryBatchItem] = []
        skipped_items: list[dict[str, Any]] = []
        reserved_source_keys: set[str] = set()
        with self._lock:
            existing_by_key = {
                self._repository_source_key(repository.git_url): repository
                for repository in self._state.repositories
            }
            accepted_source_keys: set[str] = set()
            for position, item in enumerate(cleaned, start=1):
                source_key = self._repository_source_key(item.git_url)
                existing = existing_by_key.get(source_key)
                reason: str | None = None
                detail: str | None = None
                if existing is not None:
                    reason = "already_connected"
                    detail = f"Уже подключён: {existing.name}"
                elif source_key in accepted_source_keys:
                    reason = "duplicate_in_batch"
                    detail = "Повторяется в CSV"
                elif source_key in self._repository_import_reservations:
                    reason = "import_in_progress"
                    detail = "Репозиторий уже подключается"

                if reason is not None:
                    skipped_items.append(
                        {
                            "position": position,
                            "name": item.name,
                            "git_url": item.git_url,
                            "ref": item.ref,
                            "index_id": item.index_id,
                            "status": "skipped",
                            "reason": reason,
                            "detail": detail,
                            "existing_repository_id": existing.id if existing else None,
                        }
                    )
                    continue

                accepted_source_keys.add(source_key)
                reserved_source_keys.add(source_key)
                self._repository_import_reservations.add(source_key)
                scheduled.append(item)

        requested_count = len(cleaned)
        index_ids = sorted({item.index_id for item in scheduled})
        actual_worker_count = min(worker_count, len(scheduled))
        try:
            job = self._new_job(
                "repository",
                index_id=index_ids[0] if len(index_ids) == 1 else None,
                target_id=f"batch-{uuid.uuid4().hex[:8]}",
                message=(
                    f"Repository batch queued: {len(scheduled)} to scan, "
                    f"{len(skipped_items)} skipped, {actual_worker_count} workers"
                ),
            )
            initial_result = {
                "phase": "queued" if scheduled else "skipped",
                "generation_mode": generation_mode,
                "repository_count": requested_count,
                "scheduled_count": len(scheduled),
                "skipped_count": len(skipped_items),
                "skipped_items": skipped_items,
                "worker_count": actual_worker_count,
                "index_ids": index_ids,
                "completed_count": 0,
                "failed_count": 0,
            }
            if not scheduled:
                self._update_job(
                    job.id,
                    status="completed",
                    message=(
                        f"All {requested_count} repositories skipped; "
                        "nothing was cloned or scanned"
                    ),
                    completed_at=_now(),
                    result=initial_result,
                )
                self._release_cancel_event(job.id)
                return self.job_status(job.id)

            self._update_job(job.id, result=initial_result)
            threading.Thread(
                target=self._run_repository_batch_job,
                args=(
                    job.id,
                    tuple(scheduled),
                    actual_worker_count,
                    generation_mode,
                    requested_count,
                    tuple(skipped_items),
                ),
                daemon=True,
            ).start()
            return self.job_status(job.id)
        except Exception:
            with self._lock:
                self._repository_import_reservations.difference_update(
                    reserved_source_keys
                )
            raise

    def gigacode_status(self, *, refresh: bool = False) -> dict[str, Any]:
        """Expose a safe availability snapshot for batch repository scheduling."""
        return self._gigacode.status(refresh=refresh)

    def start_repository_refresh(self, repository_id: str) -> CatalogJob:
        """Refresh Git/OpenSpec, rebuild the RAG index, and reanalyze the source map."""
        repository = self._repository(repository_id)
        job = self._new_job(
            "repository",
            index_id=repository.index_id,
            target_id=repository.id,
            message=f"Repository refresh queued: {repository.name}",
        )
        threading.Thread(
            target=self._run_repository_job,
            args=(
                job.id,
                repository.name,
                repository.git_url,
                repository.ref,
                repository.index_id,
                "static",
            ),
            daemon=True,
        ).start()
        return job

    def start_graph_build(
        self,
        *,
        generation_mode: Literal["static", "gigacode"] = "static",
        verify_all: bool = False,
        algorithm: str | None = None,
    ) -> CatalogJob:
        selected_algorithm = algorithm or self._graph_settings().builder_algorithm
        get_graph_algorithm(selected_algorithm)
        if generation_mode == "gigacode":
            gigacode_status = self._gigacode.status(refresh=True)
            if not gigacode_status["available"]:
                raise RuntimeError(str(gigacode_status["error"]))
        job = self._new_job(
            "graph",
            message=(
                "Precise graph rebuild queued: static + GigaCode"
                if generation_mode == "gigacode"
                else "Static graph rebuild queued"
            ),
        )
        threading.Thread(
            target=self._run_graph_job,
            args=(job.id, generation_mode, verify_all, selected_algorithm),
            daemon=True,
        ).start()
        return job

    def start_service_analysis(
        self,
        service_id: str,
        *,
        generation_mode: Literal["static", "gigacode"] = "static",
    ) -> CatalogJob:
        service, repository = self._service_context(service_id)
        if generation_mode == "gigacode":
            gigacode_status = self._gigacode.status(refresh=True)
            if not gigacode_status["available"]:
                raise RuntimeError(str(gigacode_status["error"]))
        job = self._new_job(
            "service",
            index_id=repository.index_id,
            target_id=service.id,
            message=(
                f"GigaCode service analysis queued: {service.name}"
                if generation_mode == "gigacode"
                else f"Static service analysis queued: {service.name}"
            ),
        )
        threading.Thread(
            target=self._run_service_analysis_job,
            args=(
                job.id,
                service.id,
                service.name,
                repository.index_id,
                generation_mode,
            ),
            daemon=True,
        ).start()
        return job

    def start_all_services_ssot_refresh(self) -> CatalogJob:
        """Refresh every connected service, preferring OpenSpec over source parsing."""
        service_map = self._service_map_store.load()
        if not service_map.services:
            raise RuntimeError("No discovered services are available to refresh")
        with self._lock:
            active = next(
                (
                    item
                    for item in self._jobs.values()
                    if item.type == "ssot"
                    and item.target_id == "all-services"
                    and item.status in {"queued", "running", "cancelling"}
                ),
                None,
            )
        if active is not None:
            raise RuntimeError(f"All-services SSOT refresh is already running: {active.id}")
        gigacode_status = self._gigacode.status(refresh=True)
        generation_mode: Literal["static", "gigacode"] = (
            "gigacode" if gigacode_status["available"] else "static"
        )
        job = self._new_job(
            "ssot",
            target_id="all-services",
            message=(
                "All-services SSOT refresh queued with GigaCode"
                if generation_mode == "gigacode"
                else "All-services OpenSpec/static refresh queued"
            ),
        )
        if generation_mode == "static" and gigacode_status.get("error"):
            self._append_job_log(
                job.id,
                f"GigaCode unavailable; using static fallback: {gigacode_status['error']}",
            )
        threading.Thread(
            target=self._run_all_services_ssot_refresh_job,
            args=(job.id, generation_mode),
            daemon=True,
        ).start()
        return job

    def ssot_generation_options(self) -> dict[str, Any]:
        with self._lock:
            indexes = [item.model_copy(deep=True) for item in self._state.indexes]
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
        service_map = self._service_map_store.load()
        services_by_repository: dict[str, list[dict[str, str]]] = {}
        for service in service_map.services:
            try:
                _mapped, repository = self._service_context(service.id)
            except KeyError:
                continue
            services_by_repository.setdefault(repository.id, []).append(
                {
                    "id": service.id,
                    "name": service.name,
                    "module_path": service.module_path,
                    "module_state": service.module_state,
                }
            )
        return {
            "status": "selection_required",
            "workflow": self._ssot_workflow_status(),
            "indexes": [
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "document_count": item.document_count,
                    "source_count": item.source_count,
                    "status": item.status,
                }
                for item in indexes
            ],
            "repositories": [
                {
                    "id": item.id,
                    "name": item.name,
                    "git_url": item.git_url,
                    "ref": item.ref,
                    "index_id": item.index_id,
                    "commit": item.commit,
                    "checkout_ready": Path(item.checkout_path).is_dir(),
                    "services": sorted(
                        services_by_repository.get(item.id, []),
                        key=lambda service: service["id"],
                    ),
                }
                for item in repositories
            ],
            "selection": {
                "index_required": True,
                "repository_required": True,
                "cloned_repository_count": len(repositories),
                "clone_if_missing": {
                    "action": "clone",
                    "required_arguments": ["index_id", "repository_name", "git_url"],
                    "optional_arguments": ["ref"],
                },
                "all_services_arguments": {
                    "action": "prepare",
                    "all_services": True,
                },
            },
            "next": (
                "Choose an index and one or more repositories, then call this tool with "
                "action='prepare'. Use all_services=true to process every cloned repository. "
                "If repositories is empty, call action='clone' with index_id, repository_name "
                "and git_url first."
            ),
        }

    def _ssot_workflow_status(self) -> dict[str, Any]:
        return {
            "configured": True,
            "mode": "client-agent-or-server-gigacode",
            "server_llm_required": False,
            "provider": "calling-client-or-gigacode",
            "generation_modes": ["client", "gigacode"],
            "gigacode": self._gigacode.status(),
            "output_pattern": "<index.knowledge_dir>/ssot/generated/<service-id>.md",
            "local_output_pattern": "<client-temp>/corporate-kb-ssot/<session-id>/<service-id>.md",
            "actions": ["options", "clone", "prepare", "status", "context", "read_file", "submit"],
        }

    def ssot_generation_request(
        self,
        *,
        action: str = "options",
        index_id: str | None = None,
        repository_ids: list[str] | None = None,
        service_ids: list[str] | None = None,
        all_services: bool = False,
        refresh_analysis: bool = True,
        generation_mode: Literal["client", "gigacode"] = "client",
        job_id: str | None = None,
        repository_name: str | None = None,
        git_url: str | None = None,
        ref: str | None = None,
        service_id: str | None = None,
        repository_id: str | None = None,
        file_path: str | None = None,
        offset: int = 0,
        max_chars: int = 20_000,
        content: str | None = None,
        finalize: bool = True,
    ) -> dict[str, Any]:
        normalized_action = action.strip().lower() or "options"
        if normalized_action == "options" and job_id:
            normalized_action = "status"
        if normalized_action == "options" and index_id is not None:
            normalized_action = "prepare"
            if not repository_ids and not service_ids:
                all_services = True
        if normalized_action == "options":
            return self.ssot_generation_options()
        if normalized_action == "clone":
            if index_id is None:
                raise ValueError("action='clone' requires index_id")
            if not repository_name or not git_url:
                raise ValueError("action='clone' requires repository_name and git_url")
            job = self.start_repository_ingestion(
                name=repository_name,
                git_url=git_url,
                index_id=index_id,
                ref=ref,
            )
            return self._queued_ssot_response(job, next_action="status")
        if normalized_action == "prepare":
            if index_id is None:
                raise ValueError("action='prepare' requires index_id")
            selected_repositories = self._normalize_selection(repository_ids)
            selected_services = self._normalize_selection(service_ids)
            if not all_services and not selected_repositories and not selected_services:
                raise ValueError("Select repository_ids or service_ids, or set all_services=true")
            job = self.start_system_ssot_generation(
                index_id=index_id,
                repository_ids=list(selected_repositories),
                service_ids=list(selected_services),
                all_services=all_services,
                refresh_analysis=refresh_analysis,
                generation_mode=generation_mode,
            )
            return self._queued_ssot_response(job, next_action="status")
        if normalized_action == "status":
            if not job_id:
                raise ValueError("action='status' requires job_id")
            return self._workflow_job_status(job_id)
        if normalized_action == "context":
            if not job_id or not service_id:
                raise ValueError("action='context' requires job_id and service_id")
            return self._ssot_target_context(job_id, service_id)
        if normalized_action == "read_file":
            if not job_id or not repository_id or not file_path:
                raise ValueError("action='read_file' requires job_id, repository_id and file_path")
            return self._ssot_read_file(
                job_id,
                repository_id,
                file_path,
                offset=offset,
                max_chars=max_chars,
            )
        if normalized_action == "submit":
            if not job_id or not service_id or content is None:
                raise ValueError("action='submit' requires job_id, service_id and content")
            return self._submit_client_ssot(
                job_id,
                service_id,
                content,
                finalize=finalize,
            )
        raise ValueError(
            "action must be options, clone, prepare, status, context, read_file, or submit"
        )

    @staticmethod
    def _normalize_selection(values: list[str] | None) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in values or [] if item.strip()))

    @staticmethod
    def _queued_ssot_response(job: CatalogJob, *, next_action: str) -> dict[str, Any]:
        return {
            "status": "queued",
            "job": job.model_dump(mode="json"),
            "poll": {
                "tool": "kb_generate_system_ssot",
                "arguments": {"action": next_action, "job_id": job.id},
            },
        }

    def start_system_ssot_generation(
        self,
        *,
        index_id: str,
        repository_ids: list[str] | None = None,
        service_ids: list[str] | None = None,
        all_services: bool = False,
        refresh_analysis: bool = True,
        generation_mode: Literal["client", "gigacode"] = "client",
    ) -> CatalogJob:
        index = self._record(index_id)
        selected_repositories = self._normalize_selection(repository_ids)
        selected_services = self._normalize_selection(service_ids)
        if len(selected_services) > 500:
            raise ValueError("No more than 500 service_ids can be generated in one job")
        with self._lock:
            available_repository_ids = {item.id for item in self._state.repositories}
        missing_repositories = set(selected_repositories) - available_repository_ids
        if missing_repositories:
            raise KeyError(f"Unknown repositories: {', '.join(sorted(missing_repositories))}")
        if generation_mode == "gigacode":
            gigacode_status = self._gigacode.status(refresh=True)
            if not gigacode_status["available"]:
                raise RuntimeError(str(gigacode_status["error"]))
        job = self._new_job(
            "ssot",
            index_id=index.id,
            target_id=index.id,
            message=(
                f"GigaCode repository analysis queued for index: {index.name}"
                if generation_mode == "gigacode"
                else f"Client SSOT source preparation queued for index: {index.name}"
            ),
        )
        threading.Thread(
            target=self._run_system_ssot_generation_job,
            args=(
                job.id,
                index.id,
                selected_repositories,
                selected_services,
                all_services,
                refresh_analysis,
                generation_mode,
            ),
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

    def job_status(self, job_id: str) -> CatalogJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        return job.model_copy(deep=True)

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

    def jobs_payload(self) -> dict[str, Any]:
        with self._lock:
            jobs = [item.model_copy(deep=True) for item in self._jobs.values()]
        jobs.sort(key=lambda item: item.id, reverse=True)
        log_files = self._job_log_files()
        return {
            "total": len(jobs),
            "active_count": sum(
                item.status in {"queued", "running", "cancelling"} for item in jobs
            ),
            "failed_count": sum(item.status == "failed" for item in jobs),
            "log_file_count": len(log_files),
            "log_bytes": sum(path.stat().st_size for path in log_files),
            "jobs": [item.model_dump(mode="json") for item in jobs],
        }

    def clear_job_history(self) -> dict[str, Any]:
        with self._lock:
            active_jobs = {
                job_id: item
                for job_id, item in self._jobs.items()
                if item.status in {"queued", "running", "cancelling"}
            }
            cleared_job_count = len(self._jobs) - len(active_jobs)
            self._jobs = active_jobs
            self._save_locked()

        deleted_log_files = 0
        deleted_log_bytes = 0
        for path in self._job_log_files():
            try:
                deleted_log_bytes += path.stat().st_size
                path.unlink()
                deleted_log_files += 1
            except FileNotFoundError:
                continue
        return {
            "cleared_job_count": cleared_job_count,
            "deleted_log_files": deleted_log_files,
            "deleted_log_bytes": deleted_log_bytes,
            "active_job_count": len(active_jobs),
        }

    def _workflow_job_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.type not in {"ssot", "repository", "index"}:
            raise KeyError(f"Unknown SSOT workflow job: {job_id}")
        log = self.job_log(job_id)["log"]
        payload = {
            "status": job.status,
            "job": job.model_dump(mode="json"),
            "log_tail": "\n".join(log.splitlines()[-80:]),
        }
        if job.status == "completed" and job.type == "repository":
            payload["next"] = {
                "tool": "kb_generate_system_ssot",
                "arguments": {"action": "options"},
                "instruction": "Select the cloned repository and start action='prepare'.",
            }
        return payload

    def _ssot_session_job(self, job_id: str) -> CatalogJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.type != "ssot" or job.status != "completed" or job.result is None:
            raise KeyError(f"Unknown or incomplete SSOT session: {job_id}")
        if job.result.get("phase") != "awaiting_client_generation":
            raise RuntimeError(f"SSOT session is not accepting client generation: {job_id}")
        return job.model_copy(deep=True)

    def _ssot_target_context(self, job_id: str, service_id: str) -> dict[str, Any]:
        job = self._ssot_session_job(job_id)
        result = job.result or {}
        target = next(
            (item for item in result.get("targets", []) if item.get("id") == service_id),
            None,
        )
        if not isinstance(target, dict):
            raise KeyError(f"SSOT target is not part of session {job_id}: {service_id}")
        repository_id = str(target["repository_id"])
        repository = self._repository(repository_id)
        checkout = Path(repository.checkout_path).resolve()
        manifest = self._ssot_source_manifest(checkout, module_path=str(target["module_path"]))
        analysis = self._ssot_analysis_for_target(target, repository)
        initial_files: list[dict[str, Any]] = []
        remaining = self.settings.ssot_generation_source_chars
        readable = [item for item in manifest["files"] if item["readable"]]
        readable.sort(
            key=lambda item: (
                not bool(item["in_service_module"]),
                self._ssot_source_priority(str(item["path"])),
                item["path"],
            )
        )
        for item in readable[: self.settings.ssot_generation_max_source_files]:
            if remaining <= 0:
                break
            excerpt_limit = min(6_000, remaining)
            excerpt = self._read_checkout_file(checkout, str(item["path"]), 0, excerpt_limit)
            remaining -= len(str(excerpt["content"]))
            initial_files.append(excerpt)
        existing_path = (
            Path(str(self._record(str(result["index_id"])).knowledge_dir)).resolve()
            / "ssot"
            / "generated"
            / f"{_slug(service_id)}.md"
        )
        existing_ssot = None
        if existing_path.is_file():
            existing_ssot = existing_path.read_text(encoding="utf-8")[:20_000]
        return {
            "status": "context_ready",
            "session_id": job_id,
            "target": target,
            "analysis": analysis,
            "source_manifest": manifest,
            "initial_source_files": initial_files,
            "existing_ssot": existing_ssot,
            "generation_instructions": self._ssot_skill_instructions(),
            "read_more": {
                "tool": "kb_generate_system_ssot",
                "arguments": {
                    "action": "read_file",
                    "job_id": job_id,
                    "repository_id": repository_id,
                    "file_path": "<path from source_manifest.files>",
                },
            },
            "finish": (
                "Generate one evidence-backed SSOT Markdown document. Then call "
                "kb_generate_system_ssot with action='submit', job_id, service_id, content and "
                "finalize."
            ),
        }

    def _ssot_read_file(
        self,
        job_id: str,
        repository_id: str,
        file_path: str,
        *,
        offset: int,
        max_chars: int,
    ) -> dict[str, Any]:
        job = self._ssot_session_job(job_id)
        selected = set((job.result or {}).get("repository_ids", []))
        if repository_id not in selected:
            raise PermissionError("Repository is not part of this SSOT session")
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        if not 1_000 <= max_chars <= 100_000:
            raise ValueError("max_chars must be between 1000 and 100000")
        repository = self._repository(repository_id)
        payload = self._read_checkout_file(
            Path(repository.checkout_path).resolve(),
            file_path,
            offset,
            max_chars,
        )
        return {
            "status": "file_ready",
            "session_id": job_id,
            "repository_id": repository_id,
            **payload,
        }

    def _submit_client_ssot(
        self,
        job_id: str,
        service_id: str,
        content: str,
        *,
        finalize: bool,
    ) -> dict[str, Any]:
        job = self._ssot_session_job(job_id)
        result = job.result or {}
        target = next(
            (item for item in result.get("targets", []) if item.get("id") == service_id),
            None,
        )
        if not isinstance(target, dict):
            raise KeyError(f"SSOT target is not part of session {job_id}: {service_id}")
        clean_content = content.strip()
        if len(clean_content) < 100:
            raise ValueError("SSOT document must contain at least 100 characters")
        if len(clean_content.encode("utf-8")) > self.settings.admin_max_upload_bytes:
            raise ValueError("SSOT document exceeds the configured upload limit")
        index_id = str(result["index_id"])
        repository = self._repository(str(target["repository_id"]))
        document = self._client_ssot_document(clean_content, target, repository)
        knowledge_root = Path(self._record(index_id).knowledge_dir).resolve()
        destination = (knowledge_root / "ssot" / "generated" / f"{_slug(service_id)}.md").resolve()
        if not destination.is_relative_to(knowledge_root):
            raise ValueError("Invalid generated SSOT destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".md.tmp")
        temporary.write_text(document, encoding="utf-8")
        os.replace(temporary, destination)

        submitted = set(str(item) for item in result.get("submitted_service_ids", []))
        submitted.add(service_id)
        files = set(str(item) for item in result.get("files", []))
        files.add(destination.relative_to(knowledge_root).as_posix())
        updated_result = {
            **result,
            "submitted_service_ids": sorted(submitted),
            "files": sorted(files),
        }
        self._update_job(job_id, result=updated_result)
        index_job = self.start_index_build(index_id) if finalize else None
        if finalize:
            self._cleanup_repository_checkouts(
                {str(item) for item in result.get("repository_ids", [])},
                job_id=job_id,
            )
        return {
            "status": "indexing" if index_job is not None else "saved",
            "session_id": job_id,
            "service_id": service_id,
            "index_id": index_id,
            "server_path": str(destination),
            "submitted_count": len(submitted),
            "target_count": int(result["target_count"]),
            "remaining_service_ids": sorted(
                str(item["id"])
                for item in result.get("targets", [])
                if str(item["id"]) not in submitted
            ),
            "index_job": index_job.model_dump(mode="json") if index_job is not None else None,
            "next": (
                "Poll the returned index_job with the dashboard jobs API."
                if index_job is not None
                else "Generate and submit the next target; use finalize=true on the last one."
            ),
        }

    def _ssot_source_manifest(self, checkout: Path, *, module_path: str) -> dict[str, Any]:
        files: list[dict[str, Any]] = []
        truncated = False
        module = (checkout / module_path).resolve()
        if not module.is_relative_to(checkout) or not module.is_dir():
            module = checkout
        for current, directories, names in os.walk(checkout, followlinks=False):
            root = Path(current)
            directories[:] = [
                name
                for name in directories
                if name not in _SSOT_IGNORED_DIRECTORIES
                and not name.startswith(".")
                and not (root / name).is_symlink()
            ]
            for name in sorted(names):
                path = root / name
                if name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(checkout).as_posix()
                files.append(
                    {
                        "path": relative,
                        "size_bytes": path.stat().st_size,
                        "readable": (
                            path.suffix.lower() in _SSOT_READABLE_SUFFIXES
                            or name.lower() in _SSOT_READABLE_NAMES
                        ),
                        "in_service_module": path.resolve().is_relative_to(module),
                    }
                )
                if len(files) >= self.settings.repository_max_files:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "repository_root": checkout.name,
            "module_path": module.relative_to(checkout).as_posix() or ".",
            "file_count": len(files),
            "truncated": truncated,
            "files": files,
        }

    def _ssot_analysis_for_target(
        self,
        target: dict[str, Any],
        repository: RepositorySource,
    ) -> dict[str, Any]:
        service_id = str(target["id"])
        if target.get("kind") == "service":
            return AnalysisArchive._service_payload(
                self._service_map_store.load(),
                self._graph_store.load(),
                service_id,
            )
        return {
            "schema_version": 1,
            "service": {
                "id": service_id,
                "name": target["name"],
                "repository": repository.name,
                "module_path": ".",
                "module_state": target["module_state"],
                "commit": repository.commit,
                "entrypoints": [],
                "outbound_interfaces": [],
            },
            "dependencies": [],
            "evidence": [],
            "issues": [
                {
                    "message": (
                        "Static analysis did not discover a complete service module. "
                        "Treat missing functionality as unknown and inspect source files."
                    )
                }
            ],
            "graph_nodes": [],
            "graph_edges": [],
        }

    @staticmethod
    def _read_checkout_file(
        checkout: Path,
        file_path: str,
        offset: int,
        max_chars: int,
    ) -> dict[str, Any]:
        normalized = file_path.strip().replace("\\", "/")
        relative = Path(normalized)
        if not normalized or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("file_path must be a safe repository-relative path")
        path = (checkout / relative).resolve()
        if not path.is_relative_to(checkout) or path.is_symlink() or not path.is_file():
            raise KeyError(f"Unknown repository file: {file_path}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"Repository file is not UTF-8 text: {file_path}") from None
        content = text[offset : offset + max_chars]
        return {
            "path": relative.as_posix(),
            "offset": offset,
            "next_offset": offset + len(content),
            "total_chars": len(text),
            "has_more": offset + len(content) < len(text),
            "content": content,
        }

    def _ssot_skill_instructions(self) -> str:
        parts = [
            "Generate concise SSOT Markdown from static analysis and requested source files. "
            "Describe observed APIs, events, jobs, outbound calls and functionality. Never invent "
            "missing facts; label inference and unknowns and preserve file/evidence references."
        ]
        for relative in (
            Path("SKILL.md"),
            Path("references/analysis-contract.md"),
            Path("assets/ssot-template.md"),
        ):
            path = self.settings.ssot_skill_path / relative
            if path.is_file():
                parts.append(path.read_text(encoding="utf-8")[:20_000])
        return "\n\n".join(parts)

    @staticmethod
    def _ssot_source_priority(path: str) -> int:
        name = Path(path).name.lower()
        markers = (
            "controller",
            "endpoint",
            "api",
            "service",
            "handler",
            "listener",
            "client",
            "application",
        )
        return next((position for position, marker in enumerate(markers) if marker in name), 20)

    @staticmethod
    def _client_ssot_document(
        content: str,
        target: dict[str, Any],
        repository: RepositorySource,
        *,
        generated_by: str = "kb_generate_system_ssot/client-agent",
        authority: str = "client-agent-source-analysis",
    ) -> str:
        if content.startswith("---"):
            return content.rstrip() + "\n"
        frontmatter = {
            "document_type": "ssot",
            "service": target["id"],
            "repository": repository.name,
            "module": target["module_path"],
            "status": "current",
            "review_status": "draft",
            "authority": authority,
            "source_type": "generated",
            "generated_by": generated_by,
            "commit": repository.commit or "unknown",
            "generated_at": _now().isoformat(),
        }
        yaml = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in frontmatter.items()
        )
        return f"---\n{yaml}\n---\n\n{content.rstrip()}\n"

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

    def index_documents(
        self,
        index_id: str,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Return one searchable page of documents from the live serving index."""
        record = self._record(index_id)
        if offset < 0:
            raise ValueError("offset must be zero or greater")
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        documents, total = self.service_for(index_id).browse_documents(
            query=query,
            offset=offset,
            limit=limit,
        )
        items = []
        for document in documents:
            items.append(
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "source_path": document.source_path,
                    "source_type": document.source_type,
                    "source_url": document.source_url,
                    "origin": self._document_origin(document.source_path),
                    "loaded_at": document.loaded_at.isoformat(),
                    "metadata": document.metadata,
                }
            )
        return {
            "index": record.model_dump(mode="json"),
            "query": query.strip(),
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(items) < total,
            "documents": items,
        }

    def index_document(self, index_id: str, document_id: str) -> dict[str, Any]:
        """Return one full normalized document from the selected serving index."""
        record = self._record(index_id)
        if not document_id.strip():
            raise ValueError("document_id must not be empty")
        document = self.service_for(index_id).get_document(document_id)
        encoded = document.content.encode("utf-8")
        return {
            "index": {
                "id": record.id,
                "name": record.name,
            },
            "document_id": document.document_id,
            "title": document.title,
            "source_path": document.source_path,
            "source_type": document.source_type,
            "source_id": document.source_id,
            "source_url": document.source_url,
            "origin": self._document_origin(document.source_path),
            "loaded_at": document.loaded_at.isoformat(),
            "metadata": document.metadata,
            "content": document.content,
            "content_chars": len(document.content),
            "content_bytes": len(encoded),
        }

    def upload_documents(
        self,
        index_id: str,
        *,
        documents: list[dict[str, str]],
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Persist validated text files under one index and queue one rebuild."""
        record = self._record(index_id)
        if not documents:
            raise ValueError("At least one document is required")
        if len(documents) > 50:
            raise ValueError("No more than 50 documents can be uploaded at once")

        root = Path(record.knowledge_dir).resolve()
        prepared: list[tuple[Path, bytes]] = []
        seen: set[Path] = set()
        total_bytes = 0
        for item in documents:
            raw_path = item.get("path")
            content = item.get("content")
            if not isinstance(raw_path, str) or not isinstance(content, str):
                raise ValueError("Each document must contain string path and content fields")
            if not content.strip():
                raise ValueError(f"Document is empty: {raw_path}")
            encoded = content.encode("utf-8")
            if b"\x00" in encoded:
                raise ValueError(f"Binary-looking document is not allowed: {raw_path}")
            if len(encoded) > self.settings.admin_max_upload_bytes:
                raise ValueError(
                    f"Document exceeds KB_ADMIN_MAX_UPLOAD_BYTES="
                    f"{self.settings.admin_max_upload_bytes}: {raw_path}"
                )
            total_bytes += len(encoded)
            if total_bytes > self.settings.admin_max_upload_bytes:
                raise ValueError(
                    "Combined upload exceeds "
                    f"KB_ADMIN_MAX_UPLOAD_BYTES={self.settings.admin_max_upload_bytes}"
                )
            target = self._safe_uploaded_document_path(root, raw_path)
            if target in seen:
                raise ValueError(f"Duplicate document path in upload: {raw_path}")
            if target.exists() and not overwrite:
                raise ValueError(
                    f"Document already exists; enable overwrite to replace it: {raw_path}"
                )
            seen.add(target)
            prepared.append((target, encoded))

        uploaded = []
        for target, encoded in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            uploaded.append(
                {
                    "source_path": target.relative_to(root).as_posix(),
                    "bytes": len(encoded),
                }
            )

        job = self.start_index_build(index_id)
        return {
            "status": "uploaded",
            "index_id": index_id,
            "file_count": len(uploaded),
            "bytes": total_bytes,
            "documents": uploaded,
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

    def graph_algorithms(self) -> dict[str, Any]:
        """Return executable builders; lifecycle YAML never invents UI options."""
        overview = self._graph_service.overview()
        current = overview.get("algorithm")
        return {
            "default_algorithm": self._graph_settings().builder_algorithm,
            "current_algorithm": current if isinstance(current, dict) else {},
            "algorithms": [item.as_dict() for item in registry.descriptors()],
        }

    def graph(
        self,
        *,
        view: str,
        service: str | None,
        depth: int,
        limit: int,
        node_types: list[str] | None = None,
        edge_types: list[str] | None = None,
        confidences: list[str] | None = None,
        connected_only: bool = False,
        include_rejected: bool = False,
    ) -> dict[str, Any]:
        return self._graph_service.graph(
            view=view,
            service=service,
            depth=depth,
            limit=limit,
            node_types=node_types,  # type: ignore[arg-type]
            edge_types=edge_types,  # type: ignore[arg-type]
            confidences=confidences,  # type: ignore[arg-type]
            connected_only=connected_only,
            include_rejected=include_rejected,
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
        generation_mode: Literal["static", "gigacode"],
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
                    generation_mode,
                )
                self._cleanup_managed_repository_checkout(
                    self._repository_id(name, git_url, ref, index_id),
                    job_id=job_id,
                )
                self._update_job(
                    job_id,
                    status="completed",
                    message=(
                        f"Imported repository {name}, completed GigaCode analysis and "
                        "refreshed the system graph"
                        if generation_mode == "gigacode"
                        else f"Imported repository {name} and refreshed the system graph"
                    ),
                    completed_at=_now(),
                )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, index_id, "Repository import failed", exc)
        finally:
            self._cleanup_managed_repository_checkout(
                self._repository_id(name, git_url, ref, index_id),
                job_id=job_id,
            )
            self._release_repository_import_reservation(git_url)
            self._release_cancel_event(job_id)

    def _run_repository_batch_job(
        self,
        job_id: str,
        repositories: tuple[RepositoryBatchItem, ...],
        worker_count: int,
        generation_mode: Literal["static", "gigacode"],
        requested_count: int,
        skipped_items: tuple[dict[str, Any], ...],
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        affected_index_ids = sorted({item.index_id for item in repositories})
        checkout_repository_ids = {
            self._repository_id(item.name, item.git_url, item.ref, item.index_id)
            for item in repositories
        }
        completed_items: list[dict[str, Any]] = []
        failed_items: list[dict[str, Any]] = []
        try:
            with self._cancellable_index_locks(affected_index_ids, cancel_event):
                self._update_job(
                    job_id,
                    status="running",
                    message=(
                        f"Preparing {len(repositories)} repositories with "
                        f"{worker_count} workers"
                    ),
                    started_at=_now(),
                )
                for index_id in affected_index_ids:
                    self._update_index(index_id, status="indexing", error=None)

                with ThreadPoolExecutor(
                    max_workers=worker_count,
                    thread_name_prefix="repository-batch",
                ) as executor:
                    futures = {
                        executor.submit(
                            self._prepare_repository_batch_item,
                            job_id,
                            position,
                            len(repositories),
                            item,
                            cancel_event,
                        ): (position, item)
                        for position, item in enumerate(repositories, start=1)
                    }
                    for future in as_completed(futures):
                        position, item = futures[future]
                        try:
                            repository = future.result()
                        except (
                            CatalogJobCancelled,
                            RepositoryOperationCancelled,
                        ):
                            raise
                        except Exception as exc:
                            error = str(exc) or type(exc).__name__
                            failed_items.append(
                                {
                                    "position": position,
                                    "name": item.name,
                                    "git_url": item.git_url,
                                    "ref": item.ref,
                                    "index_id": item.index_id,
                                    "repository_id": self._repository_id(
                                        item.name,
                                        item.git_url,
                                        item.ref,
                                        item.index_id,
                                    ),
                                    "status": "failed",
                                    "error": error,
                                }
                            )
                            self._append_job_log(
                                job_id,
                                f"Repository preparation failed [{position}/{len(repositories)}]: "
                                f"repository={item.name}; error={type(exc).__name__}: {error}",
                            )
                        else:
                            completed_items.append(
                                {
                                    "position": position,
                                    "name": repository.name,
                                    "git_url": repository.git_url,
                                    "ref": repository.ref,
                                    "index_id": repository.index_id,
                                    "repository_id": repository.id,
                                    "status": "prepared",
                                    "document_count": repository.document_count,
                                    "commit": repository.commit,
                                }
                            )
                        processed_count = len(completed_items) + len(failed_items)
                        self._update_job(
                            job_id,
                            message=(
                                f"Prepared {processed_count}/{len(repositories)} repositories "
                                f"with {worker_count} workers; errors={len(failed_items)}"
                            ),
                            result={
                                "phase": "preparing",
                                "generation_mode": generation_mode,
                                "repository_count": requested_count,
                                "scheduled_count": len(repositories),
                                "skipped_count": len(skipped_items),
                                "skipped_items": list(skipped_items),
                                "worker_count": worker_count,
                                "completed_count": len(completed_items),
                                "failed_count": len(failed_items),
                                "items": sorted(
                                    [*completed_items, *failed_items],
                                    key=lambda value: int(value["position"]),
                                ),
                            },
                        )

                self._raise_if_cancelled(cancel_event)
                if not completed_items:
                    raise RuntimeError(
                        f"All {len(repositories)} repositories failed during preparation"
                    )

                successful_repository_ids = {
                    str(item["repository_id"]) for item in completed_items
                }
                successful_index_ids = sorted(
                    {str(item["index_id"]) for item in completed_items}
                )
                self._update_job(
                    job_id,
                    message=(
                        f"Prepared {len(completed_items)} repositories; building one system graph"
                    ),
                    result={
                        "phase": "building_graph",
                        "generation_mode": generation_mode,
                        "repository_count": requested_count,
                        "scheduled_count": len(repositories),
                        "skipped_count": len(skipped_items),
                        "skipped_items": list(skipped_items),
                        "worker_count": worker_count,
                        "completed_count": len(completed_items),
                        "failed_count": len(failed_items),
                    },
                )
                snapshot = self._build_graph(
                    cancel_event=cancel_event,
                    job_id=job_id,
                    analysis_mode=generation_mode,
                    verify_all=generation_mode == "gigacode",
                )
                self._raise_if_cancelled(cancel_event)

                gigacode_results: list[dict[str, Any]] = []
                rebuilt_index_ids: set[str] = set()
                if generation_mode == "gigacode":
                    repository_ids_by_index: dict[str, list[str]] = {}
                    for completed_item in completed_items:
                        repository_ids_by_index.setdefault(
                            str(completed_item["index_id"]),
                            [],
                        ).append(str(completed_item["repository_id"]))
                    for index_id in successful_index_ids:
                        self._raise_if_cancelled(cancel_event)
                        try:
                            self._execute_system_ssot_generation_job(
                                job_id,
                                index_id,
                                tuple(sorted(repository_ids_by_index[index_id])),
                                (),
                                False,
                                False,
                                "gigacode",
                                cancel_event,
                                complete_job=False,
                            )
                            current_result = self.job_status(job_id).result or {}
                            gigacode_results.append(dict(current_result))
                        except (
                            CatalogJobCancelled,
                            GigaCodeCancelled,
                            IndexBuildCancelled,
                            RepositoryOperationCancelled,
                            ServiceMapBuildCancelled,
                        ):
                            raise
                        except Exception as exc:
                            error = self._gigacode_failure_message(exc)
                            self._append_job_log(
                                job_id,
                                "GigaCode index fallback: rebuilding from static/OpenSpec "
                                f"documents; index={index_id}; error={error}",
                            )
                            stats = self._rebuild_index(index_id, cancel_event)
                            gigacode_results.append(
                                {
                                    "index_id": index_id,
                                    "generation_mode": "static",
                                    "fallback_used": True,
                                    "gigacode_error": error,
                                    "document_count": stats.document_count,
                                    "chunk_count": stats.chunk_count,
                                }
                            )
                        rebuilt_index_ids.add(index_id)
                else:
                    for index_id in successful_index_ids:
                        self._raise_if_cancelled(cancel_event)
                        index = self._record(index_id)
                        self._update_job(
                            job_id,
                            message=f"Rebuilding linked RAG index once: {index.name}",
                        )
                        stats = self._rebuild_index(index_id, cancel_event)
                        self._append_job_log(
                            job_id,
                            f"RAG index ready: index={index_id}; "
                            f"documents={stats.document_count}; chunks={stats.chunk_count}",
                        )
                        rebuilt_index_ids.add(index_id)

                for index_id in set(affected_index_ids) - rebuilt_index_ids:
                    self._restore_index_status(index_id)

                ordered_items = sorted(
                    [*completed_items, *failed_items],
                    key=lambda value: int(value["position"]),
                )
                self._update_job(
                    job_id,
                    status="completed",
                    message=(
                        f"Batch imported {len(completed_items)}/{len(repositories)} scheduled "
                        f"repositories; skipped={len(skipped_items)}; errors={len(failed_items)}; "
                        f"rebuilt {len(rebuilt_index_ids)} indexes and one system graph"
                    ),
                    completed_at=_now(),
                    result={
                        "phase": "indexed",
                        "generation_mode": generation_mode,
                        "repository_count": requested_count,
                        "scheduled_count": len(repositories),
                        "skipped_count": len(skipped_items),
                        "skipped_items": list(skipped_items),
                        "worker_count": worker_count,
                        "completed_count": len(completed_items),
                        "failed_count": len(failed_items),
                        "index_ids": sorted(rebuilt_index_ids),
                        "repository_ids": sorted(successful_repository_ids),
                        "graph_node_count": len(snapshot.nodes),
                        "graph_edge_count": len(snapshot.edges),
                        "gigacode_results": gigacode_results,
                        "items": ordered_items,
                    },
                )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            for index_id in affected_index_ids:
                self._restore_index_status(index_id)
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            for index_id in affected_index_ids:
                self._update_index(
                    index_id,
                    status="error",
                    error="Repository batch failed; open the job log",
                    updated_at=_now(),
                )
            self._fail_background_job(job_id, None, "Repository batch failed", exc)
        finally:
            self._cleanup_repository_checkouts(checkout_repository_ids, job_id=job_id)
            with self._lock:
                self._repository_import_reservations.difference_update(
                    self._repository_source_key(item.git_url) for item in repositories
                )
            self._release_cancel_event(job_id)

    def _prepare_repository_batch_item(
        self,
        job_id: str,
        position: int,
        total: int,
        item: RepositoryBatchItem,
        cancel_event: threading.Event,
    ) -> RepositorySource:
        self._raise_if_cancelled(cancel_event)
        self._append_job_log(
            job_id,
            f"Worker started [{position}/{total}]: repository={item.name}; "
            f"ref={item.ref or 'default'}; index={item.index_id}",
        )
        paths, records = RepositorySourceManager(self._graph_settings()).materialize(
            [RepositorySpec(source=item.git_url, ref=item.ref)],
            refresh=True,
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event)
        checkout = paths[0]
        ingestion = records[0]
        repository_id = self._repository_id(
            item.name,
            item.git_url,
            item.ref,
            item.index_id,
        )
        documentation = (
            Path(self._record(item.index_id).knowledge_dir)
            / "repositories"
            / repository_id
        ).resolve()
        try:
            previous = self._repository(repository_id)
            repository = previous.model_copy(
                update={
                    "name": item.name,
                    "git_url": item.git_url,
                    "ref": item.ref,
                    "checkout_path": str(checkout),
                    "checkout_state": (
                        "external" if ingestion.source_type == "local" else "available"
                    ),
                    "checkout_removed_at": None,
                    "documentation_path": str(documentation),
                    "commit": ingestion.commit,
                }
            )
        except KeyError:
            repository = RepositorySource(
                id=repository_id,
                name=item.name,
                git_url=item.git_url,
                ref=item.ref,
                index_id=item.index_id,
                checkout_path=str(checkout),
                checkout_state=(
                    "external" if ingestion.source_type == "local" else "available"
                ),
                documentation_path=str(documentation),
                commit=ingestion.commit,
            )
        self._upsert_repository(repository)
        openspecs = self._find_openspecs(checkout, cancel_event=cancel_event)
        document_count = self._sync_openspec(
            sources=openspecs,
            checkout=checkout,
            destination=documentation / "openspec",
            cancel_event=cancel_event,
        )
        self._raise_if_cancelled(cancel_event)
        repository = repository.model_copy(
            update={
                "openspec_path": str(openspecs[0]) if openspecs else None,
                "openspec_paths": [str(path) for path in openspecs],
                "document_count": document_count,
                "synced_at": _now(),
            }
        )
        self._upsert_repository(repository)
        paths_summary = (
            ",".join(path.relative_to(checkout).as_posix() for path in openspecs)
            or "none"
        )
        self._append_job_log(
            job_id,
            f"Worker ready [{position}/{total}]: repository={repository.id}; "
            f"openspec={paths_summary}; documents={document_count}",
        )
        return repository

    def _execute_repository_job(
        self,
        job_id: str,
        name: str,
        git_url: str,
        ref: str | None,
        index_id: str,
        cancel_event: threading.Event,
        generation_mode: Literal["static", "gigacode"],
    ) -> None:
        self._update_job(
            job_id,
            status="running",
            message="Refreshing repository checkout",
            started_at=_now(),
        )
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
            repository_id = self._repository_id(name, git_url, ref, index_id)
            documentation = (
                Path(self._record(index_id).knowledge_dir) / "repositories" / repository_id
            ).resolve()
            try:
                previous = self._repository(repository_id)
                repository = previous.model_copy(
                    update={
                        "name": name,
                        "git_url": git_url,
                        "ref": ref,
                        "checkout_path": str(checkout),
                        "checkout_state": (
                            "external" if ingestion.source_type == "local" else "available"
                        ),
                        "checkout_removed_at": None,
                        "documentation_path": str(documentation),
                        "commit": ingestion.commit,
                    }
                )
            except KeyError:
                repository = RepositorySource(
                    id=repository_id,
                    name=name,
                    git_url=git_url,
                    ref=ref,
                    index_id=index_id,
                    checkout_path=str(checkout),
                    checkout_state=(
                        "external" if ingestion.source_type == "local" else "available"
                    ),
                    documentation_path=str(documentation),
                    commit=ingestion.commit,
                )
            # Persist the source before deeper inspection. Even an empty or broken
            # repository must remain visible in the UI so the user can retry it.
            self._upsert_repository(repository)
            self._update_job(job_id, message="Scanning repository for OpenSpec directories")
            openspecs = self._find_openspecs(checkout, cancel_event=cancel_event)
            openspec_summary = (
                ", ".join(str(path.relative_to(checkout)) for path in openspecs) or "none"
            )
            self._append_job_log(
                job_id,
                f"OpenSpec scan ready: roots={len(openspecs)}; paths={openspec_summary}",
            )
            document_count = self._sync_openspec(
                sources=openspecs,
                checkout=checkout,
                destination=documentation / "openspec",
                cancel_event=cancel_event,
            )
            self._raise_if_cancelled(cancel_event)
            repository = repository.model_copy(
                update={
                    "openspec_path": str(openspecs[0]) if openspecs else None,
                    "openspec_paths": [str(path) for path in openspecs],
                    "document_count": document_count,
                    "synced_at": _now(),
                }
            )
            self._upsert_repository(repository)
            if generation_mode == "static":
                self._update_job(
                    job_id,
                    message=f"Building RAG index from {document_count} documents",
                )
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
            else:
                self._append_job_log(
                    job_id,
                    f"OpenSpec staged: documents={document_count}; final index build will run "
                    "after GigaCode SSOT generation",
                )
            self._update_job(job_id, message="Building service graph")
            self._build_graph(
                cancel_event=cancel_event,
                job_id=job_id,
                analysis_mode=("gigacode" if generation_mode == "gigacode" else "static"),
                verify_all=generation_mode == "gigacode",
            )
            self._raise_if_cancelled(cancel_event)
            if generation_mode == "gigacode":
                self._append_job_log(
                    job_id,
                    f"Static graph ready; starting GigaCode for repository={repository_id}",
                )
                self._execute_system_ssot_generation_job(
                    job_id,
                    index_id,
                    (repository_id,),
                    (),
                    False,
                    False,
                    "gigacode",
                    cancel_event,
                    complete_job=False,
                )
                return
            self._update_job(
                job_id,
                message=(
                    f"Imported {document_count} OpenSpec documents; finalizing checkout cleanup"
                ),
            )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
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

    def _run_graph_job(
        self,
        job_id: str,
        generation_mode: Literal["static", "gigacode"],
        verify_all: bool,
        algorithm: str,
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        with self._lock:
            repository_ids = {item.id for item in self._state.repositories}
        cleanup_pending = set(repository_ids)
        try:
            self._raise_if_cancelled(cancel_event)
            for repository_id in sorted(repository_ids):
                self._ensure_repository_checkout(
                    repository_id,
                    job_id=job_id,
                    cancel_event=cancel_event,
                )
            snapshot = self._execute_graph_job(
                job_id,
                cancel_event,
                generation_mode,
                verify_all,
                algorithm,
            )
            self._cleanup_repository_checkouts(cleanup_pending, job_id=job_id)
            cleanup_pending.clear()
            partial = self._is_partial_analysis(snapshot)
            self._update_job(
                job_id,
                status="completed",
                message=(
                    f"Published partial map with {len(snapshot.nodes)} nodes"
                    if partial
                    else (
                        f"Built {len(snapshot.nodes)} nodes and {len(snapshot.edges)} edges "
                        f"with {snapshot.analysis_mode}"
                    )
                ),
                completed_at=_now(),
                result={
                    "phase": "published",
                    "generation_mode": generation_mode,
                    "snapshot_id": snapshot.snapshot_id,
                    "verification": snapshot.verification,
                    "algorithm": snapshot.algorithm,
                    "checkout_cleanup": "completed",
                },
            )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            self._fail_background_job(job_id, None, "Graph build failed", exc)
        finally:
            self._cleanup_repository_checkouts(cleanup_pending, job_id=job_id)
            self._release_cancel_event(job_id)

    def _execute_graph_job(
        self,
        job_id: str,
        cancel_event: threading.Event,
        generation_mode: Literal["static", "gigacode"],
        verify_all: bool,
        algorithm: str,
    ) -> GraphSnapshot:
        self._update_job(
            job_id,
            status="running",
            message=(
                "Static scan before GigaCode verification"
                if generation_mode == "gigacode"
                else "Analyzing repositories"
            ),
            started_at=_now(),
        )
        snapshot = self._build_graph(
            cancel_event=cancel_event,
            job_id=job_id,
            force_all=generation_mode == "gigacode",
            analysis_mode=generation_mode,
            verify_all=verify_all,
            algorithm=algorithm,
        )
        self._raise_if_cancelled(cancel_event)
        return snapshot

    def _run_all_services_ssot_refresh_job(
        self,
        job_id: str,
        generation_mode: Literal["static", "gigacode"],
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        affected_index_ids: list[str] = []
        checkout_repository_ids: set[str] = set()
        try:
            with self._cancellable_lock(self._all_services_refresh_lock, cancel_event):
                with self._lock:
                    repositories = [item.model_copy(deep=True) for item in self._state.repositories]
                if not repositories:
                    raise RuntimeError("No connected repositories are available to refresh")
                checkout_repository_ids = {item.id for item in repositories}
                repositories = [
                    self._ensure_repository_checkout(
                        item.id,
                        job_id=job_id,
                        cancel_event=cancel_event,
                    )
                    for item in repositories
                ]
                service_map = self._service_map_store.load()
                services_by_repository: dict[str, list[ServiceRecord]] = {}
                repository_by_service: dict[str, RepositorySource] = {}
                for service in service_map.services:
                    try:
                        _mapped, repository = self._service_context(service.id)
                    except KeyError:
                        self._append_job_log(
                            job_id,
                            f"Skipping unmapped service={service.id}; repository was not found",
                        )
                        continue
                    services_by_repository.setdefault(repository.id, []).append(service)
                    repository_by_service[service.id] = repository
                if not repository_by_service:
                    raise RuntimeError("No services are linked to connected repositories")

                affected_index_ids = sorted({item.index_id for item in repositories})
                with self._cancellable_index_locks(affected_index_ids, cancel_event):
                    self._update_job(
                        job_id,
                        status="running",
                        message=("Checking OpenSpec before GigaCode/static analysis"),
                        started_at=_now(),
                    )
                    for index_id in affected_index_ids:
                        self._update_index(index_id, status="indexing", error=None)

                    skipped_service_ids: set[str] = set()
                    openspec_documents = 0
                    for position, repository in enumerate(repositories, start=1):
                        self._raise_if_cancelled(cancel_event)
                        checkout = Path(repository.checkout_path).resolve()
                        self._update_job(
                            job_id,
                            message=(
                                f"OpenSpec preflight [{position}/{len(repositories)}]: "
                                f"{repository.name}"
                            ),
                        )
                        openspecs = self._find_openspecs(
                            checkout,
                            cancel_event=cancel_event,
                        )
                        document_count = self._sync_openspec(
                            sources=openspecs,
                            checkout=checkout,
                            destination=(
                                Path(self._record(repository.index_id).knowledge_dir)
                                / "repositories"
                                / repository.id
                                / "openspec"
                            ),
                            cancel_event=cancel_event,
                        )
                        openspec_documents += document_count
                        repository = repository.model_copy(
                            update={
                                "openspec_path": str(openspecs[0]) if openspecs else None,
                                "openspec_paths": [str(path) for path in openspecs],
                                "document_count": document_count,
                                "synced_at": _now(),
                            }
                        )
                        self._upsert_repository(repository)
                        owned = self._services_owning_openspec(
                            services_by_repository.get(repository.id, []),
                            checkout,
                            openspecs,
                        )
                        skipped_service_ids.update(owned)
                        removed_generated = self._remove_generated_ssot(
                            repository.index_id,
                            owned,
                        )
                        paths = (
                            ",".join(path.relative_to(checkout).as_posix() for path in openspecs)
                            or "none"
                        )
                        self._append_job_log(
                            job_id,
                            f"OpenSpec preflight ready: repository={repository.id}; "
                            f"roots={paths}; documents={document_count}; "
                            f"source_scan_skipped={','.join(sorted(owned)) or 'none'}; "
                            f"stale_generated_ssot_removed={removed_generated}",
                        )

                    analyzable_service_ids = set(repository_by_service) - skipped_service_ids
                    self._update_job(
                        job_id,
                        message=(
                            f"Static analysis: {len(analyzable_service_ids)} services; "
                            f"OpenSpec-only: {len(skipped_service_ids)}"
                        ),
                    )
                    snapshot = self._build_graph(
                        cancel_event=cancel_event,
                        job_id=job_id,
                        force_service_ids=analyzable_service_ids,
                        skip_service_ids=skipped_service_ids,
                    )
                    self._raise_if_cancelled(cancel_event)

                    gigacode_results: list[dict[str, Any]] = []
                    static_ssot_files: list[str] = []
                    rebuilt_index_ids: set[str] = set()
                    refreshed_map = self._service_map_store.load()
                    refreshed_services = {item.id: item for item in refreshed_map.services}
                    if generation_mode == "static" and analyzable_service_ids:
                        static_ssot_files = self._write_static_ssot_documents(
                            job_id,
                            analyzable_service_ids,
                            refreshed_map,
                            repository_by_service,
                        )
                    elif generation_mode == "gigacode" and analyzable_service_ids:
                        targets_by_index: dict[str, list[dict[str, Any]]] = {}
                        repository_ids_by_index: dict[str, set[str]] = {}
                        for service_id in sorted(analyzable_service_ids):
                            target_service = refreshed_services.get(service_id)
                            target_repository = repository_by_service.get(service_id)
                            if target_service is None or target_repository is None:
                                self._append_job_log(
                                    job_id,
                                    f"GigaCode target skipped after refresh: service={service_id}",
                                )
                                continue
                            targets_by_index.setdefault(target_repository.index_id, []).append(
                                self._ssot_target(job_id, target_service, target_repository)
                            )
                            repository_ids_by_index.setdefault(
                                target_repository.index_id,
                                set(),
                            ).add(target_repository.id)
                        for index_id in sorted(targets_by_index):
                            self._raise_if_cancelled(cancel_event)
                            result = self._generate_ssot_with_gigacode(
                                job_id=job_id,
                                index=self._record(index_id),
                                targets=targets_by_index[index_id],
                                repository_ids=sorted(repository_ids_by_index[index_id]),
                                cancel_event=cancel_event,
                                complete_job=False,
                            )
                            gigacode_results.append(result)
                            rebuilt_index_ids.add(index_id)

                    for index_id in affected_index_ids:
                        if index_id in rebuilt_index_ids:
                            continue
                        self._raise_if_cancelled(cancel_event)
                        index = self._record(index_id)
                        self._update_job(
                            job_id,
                            message=f"Rebuilding linked RAG index: {index.name}",
                        )
                        stats = self._rebuild_index(index_id, cancel_event)
                        self._append_job_log(
                            job_id,
                            f"RAG index ready: index={index_id}; "
                            f"documents={stats.document_count}; chunks={stats.chunk_count}",
                        )
                        rebuilt_index_ids.add(index_id)

                    self._update_job(
                        job_id,
                        status="completed",
                        message=(
                            f"Refreshed {len(repository_by_service)} services: "
                            f"OpenSpec-only {len(skipped_service_ids)}, "
                            f"{generation_mode} {len(analyzable_service_ids)}"
                        ),
                        completed_at=_now(),
                        result={
                            "phase": "indexed",
                            "generation_mode": generation_mode,
                            "service_count": len(repository_by_service),
                            "openspec_service_count": len(skipped_service_ids),
                            "openspec_service_ids": sorted(skipped_service_ids),
                            "analyzed_service_count": len(analyzable_service_ids),
                            "analyzed_service_ids": sorted(analyzable_service_ids),
                            "openspec_document_count": openspec_documents,
                            "index_ids": sorted(rebuilt_index_ids),
                            "graph_node_count": len(snapshot.nodes),
                            "graph_edge_count": len(snapshot.edges),
                            "gigacode_results": gigacode_results,
                            "static_ssot_files": static_ssot_files,
                            "gigacode_used": generation_mode == "gigacode",
                        },
                    )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            for index_id in affected_index_ids:
                self._restore_index_status(index_id)
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            for index_id in affected_index_ids:
                self._update_index(
                    index_id,
                    status="error",
                    error="All-services SSOT refresh failed; open the job log",
                    updated_at=_now(),
                )
            self._fail_background_job(job_id, None, "All-services SSOT refresh failed", exc)
        finally:
            self._cleanup_repository_checkouts(checkout_repository_ids, job_id=job_id)
            self._release_cancel_event(job_id)

    def _run_service_analysis_job(
        self,
        job_id: str,
        service_id: str,
        service_name: str,
        index_id: str,
        generation_mode: Literal["static", "gigacode"],
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        repository_id: str | None = None
        try:
            with self._cancellable_lock(self._index_work_lock(index_id), cancel_event):
                self._raise_if_cancelled(cancel_event)
                _service, repository = self._service_context(service_id)
                repository_id = repository.id
                self._ensure_repository_checkout(
                    repository_id,
                    job_id=job_id,
                    cancel_event=cancel_event,
                )
                self._update_job(
                    job_id,
                    status="running",
                    message=f"Static source scan: {service_name}",
                    started_at=_now(),
                )
                snapshot = self._build_graph(
                    cancel_event=cancel_event,
                    job_id=job_id,
                    force_service_ids={service_id},
                )
                self._raise_if_cancelled(cancel_event)
                self._append_job_log(
                    job_id,
                    (
                        f"Snapshot contains {len(snapshot.nodes)} nodes and "
                        f"{len(snapshot.edges)} edges"
                    ),
                )
                if generation_mode == "gigacode":
                    self._append_job_log(
                        job_id,
                        f"Static scan completed; starting GigaCode for service={service_id}",
                    )
                    self._execute_system_ssot_generation_job(
                        job_id,
                        index_id,
                        (),
                        (service_id,),
                        False,
                        False,
                        "gigacode",
                        cancel_event,
                    )
                    return
                self._update_job(
                    job_id,
                    status="completed",
                    message=f"Static service analysis completed: {service_name}",
                    completed_at=_now(),
                )
        except (
            CatalogJobCancelled,
            GigaCodeCancelled,
            IndexBuildCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, index_id)
        except Exception as exc:
            self._fail_background_job(job_id, None, f"Service analysis failed: {service_id}", exc)
        finally:
            if repository_id is not None:
                self._cleanup_managed_repository_checkout(repository_id, job_id=job_id)
            self._release_cancel_event(job_id)

    def _run_system_ssot_generation_job(
        self,
        job_id: str,
        index_id: str,
        repository_ids: tuple[str, ...],
        service_ids: tuple[str, ...],
        all_services: bool,
        refresh_analysis: bool,
        generation_mode: Literal["client", "gigacode"],
    ) -> None:
        cancel_event = self._cancel_event(job_id)
        checkout_repository_ids = set(repository_ids)
        if all_services:
            with self._lock:
                checkout_repository_ids.update(item.id for item in self._state.repositories)
        retain_for_client = False
        try:
            for service_id in service_ids:
                _service, repository = self._service_context(service_id)
                checkout_repository_ids.add(repository.id)
            with self._cancellable_lock(self._index_work_lock(index_id), cancel_event):
                self._raise_if_cancelled(cancel_event)
                for repository_id in sorted(checkout_repository_ids):
                    self._ensure_repository_checkout(
                        repository_id,
                        job_id=job_id,
                        cancel_event=cancel_event,
                    )
                self._execute_system_ssot_generation_job(
                    job_id,
                    index_id,
                    repository_ids,
                    service_ids,
                    all_services,
                    refresh_analysis,
                    generation_mode,
                    cancel_event,
                )
                retain_for_client = generation_mode == "client"
        except (
            CatalogJobCancelled,
            IndexBuildCancelled,
            GigaCodeCancelled,
            RepositoryOperationCancelled,
            ServiceMapBuildCancelled,
        ):
            self._finish_cancelled_job(job_id, None)
        except Exception as exc:
            self._fail_background_job(job_id, None, "SSOT source preparation failed", exc)
        finally:
            if not retain_for_client:
                self._cleanup_repository_checkouts(checkout_repository_ids, job_id=job_id)
            self._release_cancel_event(job_id)

    def _execute_system_ssot_generation_job(
        self,
        job_id: str,
        index_id: str,
        repository_ids: tuple[str, ...],
        service_ids: tuple[str, ...],
        all_services: bool,
        refresh_analysis: bool,
        generation_mode: Literal["client", "gigacode"],
        cancel_event: threading.Event,
        *,
        complete_job: bool = True,
    ) -> None:
        self._update_job(
            job_id,
            status="running",
            message=(
                "Refreshing source analysis before GigaCode repository scan"
                if refresh_analysis and generation_mode == "gigacode"
                else "Loading source analysis before GigaCode repository scan"
                if generation_mode == "gigacode"
                else "Refreshing source analysis for the calling client"
                if refresh_analysis
                else "Loading the latest source analysis for the calling client"
            ),
            started_at=_now(),
        )
        if refresh_analysis:
            forced_service_ids = set(service_ids)
            if not all_services and repository_ids:
                current_map = self._service_map_store.load()
                selected_repository_ids = set(repository_ids)
                for service in current_map.services:
                    try:
                        _mapped, repository = self._service_context(service.id)
                    except KeyError:
                        continue
                    if repository.id in selected_repository_ids:
                        forced_service_ids.add(service.id)
            self._build_graph(
                cancel_event=cancel_event,
                job_id=job_id,
                force_service_ids=forced_service_ids or None,
                force_all=all_services,
            )
        self._raise_if_cancelled(cancel_event)
        service_map = self._service_map_store.load()
        index = self._record(index_id)
        with self._lock:
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
        repository_by_id = {item.id: item for item in repositories}
        selected_repository_ids = set(repository_ids)
        selected_service_ids = set(service_ids)
        if all_services:
            selected_repository_ids = set(repository_by_id)

        targets: list[dict[str, Any]] = []
        seen_repository_ids: set[str] = set()
        found_service_ids: set[str] = set()
        services_by_repository: dict[str, int] = {}
        for service in service_map.services:
            self._raise_if_cancelled(cancel_event)
            try:
                _mapped, repository = self._service_context(service.id)
            except KeyError:
                continue
            selected = (
                all_services
                or service.id in selected_service_ids
                or repository.id in selected_repository_ids
            )
            if not selected:
                continue
            found_service_ids.add(service.id)
            seen_repository_ids.add(repository.id)
            services_by_repository[repository.id] = services_by_repository.get(repository.id, 0) + 1
            targets.append(
                {
                    "id": service.id,
                    "kind": "service",
                    "name": service.name,
                    "service_id": service.id,
                    "repository_id": repository.id,
                    "repository_name": repository.name,
                    "module_path": service.module_path,
                    "module_state": service.module_state,
                    "context_call": {
                        "action": "context",
                        "job_id": job_id,
                        "service_id": service.id,
                    },
                }
            )
        missing_services = selected_service_ids - found_service_ids
        if missing_services:
            raise KeyError(
                f"Unknown services after analysis: {', '.join(sorted(missing_services))}"
            )

        for repository_id in sorted(selected_repository_ids):
            repository = repository_by_id[repository_id]
            seen_repository_ids.add(repository.id)
            if services_by_repository.get(repository.id, 0):
                continue
            target_id = f"repository-{repository.id}"
            targets.append(
                {
                    "id": target_id,
                    "kind": "repository",
                    "name": repository.name,
                    "service_id": target_id,
                    "repository_id": repository.id,
                    "repository_name": repository.name,
                    "module_path": ".",
                    "module_state": "unfinished-or-undetected",
                    "context_call": {
                        "action": "context",
                        "job_id": job_id,
                        "service_id": target_id,
                    },
                }
            )
        if not targets:
            raise RuntimeError(
                "No SSOT targets were selected; choose cloned repositories or discovered services"
            )

        targets.sort(key=lambda item: str(item["id"]))
        if generation_mode == "gigacode":
            gigacode_payload = self._generate_ssot_with_gigacode(
                job_id=job_id,
                index=index,
                targets=targets,
                repository_ids=sorted(seen_repository_ids),
                cancel_event=cancel_event,
                complete_job=complete_job,
            )
            if not complete_job:
                self._update_job(
                    job_id,
                    message="GigaCode analysis ready; finalizing checkout cleanup",
                    result=gigacode_payload,
                )
            return
        result_payload: dict[str, Any] = {
            "phase": "awaiting_client_generation",
            "session_id": job_id,
            "index_id": index_id,
            "index_name": index.name,
            "repository_ids": sorted(seen_repository_ids),
            "target_count": len(targets),
            "targets": targets,
            "submitted_service_ids": [],
            "server_llm_used": False,
            "next": (
                "For each targets[] item call action='context'. Review analysis and file manifest, "
                "then call action='read_file' for any source needed. Generate Markdown with the "
                "calling client's model, then upload it directly with action='submit'."
            ),
        }
        self._update_job(
            job_id,
            status="completed",
            message=(
                f"Prepared source context for {len(targets)} SSOT targets; waiting for the "
                "calling client's model"
            ),
            completed_at=_now(),
            result=result_payload,
        )

    def _generate_ssot_with_gigacode(
        self,
        *,
        job_id: str,
        index: RagIndex,
        targets: list[dict[str, Any]],
        repository_ids: list[str],
        cancel_event: threading.Event,
        complete_job: bool = True,
    ) -> dict[str, Any]:
        knowledge_root = Path(index.knowledge_dir).resolve()
        knowledge_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".gigacode-ssot-", dir=knowledge_root))
        gigacode_runs: list[dict[str, Any]] = []
        gigacode_errors: list[dict[str, str]] = []
        published: list[str] = []
        preserved: list[str] = []
        service_map = self._service_map_store.load()
        services = {item.id: item for item in service_map.services}
        try:
            for position, target in enumerate(targets, start=1):
                self._raise_if_cancelled(cancel_event)
                service_id = str(target["id"])
                repository = self._repository(str(target["repository_id"]))
                analysis = self._ssot_analysis_for_target(target, repository)
                existing_path = knowledge_root / "ssot" / "generated" / f"{_slug(service_id)}.md"
                existing_ssot = (
                    existing_path.read_text(encoding="utf-8")[:20_000]
                    if existing_path.is_file()
                    else None
                )
                self._update_job(
                    job_id,
                    message=(
                        f"GigaCode [{position}/{len(targets)}] is analyzing "
                        f"{service_id} in {repository.name}"
                    ),
                )
                self._append_job_log(
                    job_id,
                    f"GigaCode target [{position}/{len(targets)}]: service={service_id}; "
                    f"repository={repository.id}; module={target['module_path']}; "
                    f"checkout={repository.checkout_path}",
                )

                def authentication_required(
                    url: str,
                    target_service_id: str = service_id,
                ) -> None:
                    self._gigacode_authentication_required(
                        job_id,
                        target_service_id,
                        url,
                    )

                def authentication_completed(
                    target_service_id: str = service_id,
                ) -> None:
                    self._gigacode_authentication_completed(
                        job_id,
                        target_service_id,
                    )

                try:
                    result = self._gigacode.run(
                        checkout=Path(repository.checkout_path),
                        prompt=self._gigacode_ssot_prompt(
                            target=target,
                            repository=repository,
                            analysis=analysis,
                            existing_ssot=existing_ssot,
                        ),
                        cancel=cancel_event,
                        progress=lambda message: self._append_job_log(job_id, message),
                        authentication_url=authentication_required,
                        authentication_complete=authentication_completed,
                    )
                except GigaCodeCancelled:
                    raise
                except Exception as exc:
                    error = self._gigacode_failure_message(exc)
                    relative_path = (
                        Path("ssot") / "generated" / f"{_slug(service_id)}.md"
                    ).as_posix()
                    if existing_path.is_file():
                        fallback = "preserved-existing-ssot"
                        preserved.append(relative_path)
                    else:
                        fallback = "static-analysis-ssot"
                        service = services.get(service_id)
                        markdown = (
                            self._static_ssot_markdown(service, repository, service_map)
                            if service is not None
                            else self._repository_fallback_ssot_markdown(target, repository)
                        )
                        document = self._client_ssot_document(
                            markdown,
                            target,
                            repository,
                            generated_by="kb_generate_system_ssot/gigacode-fallback",
                            authority="static-source-analysis",
                        )
                        (staging / f"{_slug(service_id)}.md").write_text(
                            document,
                            encoding="utf-8",
                        )
                    failure = {
                        "service_id": service_id,
                        "repository_id": repository.id,
                        "error": error,
                        "fallback": fallback,
                    }
                    gigacode_errors.append(failure)
                    gigacode_runs.append({**failure, "status": "failed"})
                    self._append_job_log(
                        job_id,
                        "GigaCode target fallback: "
                        f"service={service_id}; fallback={fallback}; error={error}",
                    )
                    continue
                document = self._client_ssot_document(
                    result.markdown,
                    target,
                    repository,
                    generated_by="kb_generate_system_ssot/gigacode",
                    authority="gigacode-source-analysis",
                )
                filename = f"{_slug(service_id)}.md"
                (staging / filename).write_text(document, encoding="utf-8")
                gigacode_runs.append(
                    {
                        "service_id": service_id,
                        "repository_id": repository.id,
                        "session_id": result.session_id,
                        "model": result.model,
                        "duration_ms": result.duration_ms,
                        "analyzed_files": list(result.analyzed_files),
                        "blocking_unknowns": list(result.blocking_unknowns),
                        "usage": result.usage,
                        "status": "completed",
                    }
                )
                self._append_job_log(
                    job_id,
                    f"GigaCode target ready: service={service_id}; "
                    f"files={len(result.analyzed_files)}; "
                    f"unknowns={len(result.blocking_unknowns)}",
                )

            self._raise_if_cancelled(cancel_event)
            destination_root = knowledge_root / "ssot" / "generated"
            destination_root.mkdir(parents=True, exist_ok=True)
            for staged in sorted(staging.glob("*.md")):
                destination = destination_root / staged.name
                os.replace(staged, destination)
                published.append(destination.relative_to(knowledge_root).as_posix())

            available_files = sorted(set([*published, *preserved]))
            successful_runs = len(gigacode_runs) - len(gigacode_errors)

            self._update_job(
                job_id,
                message=(
                    f"GigaCode completed {successful_runs}/{len(targets)} targets; "
                    f"fallbacks={len(gigacode_errors)}; rebuilding RAG index"
                ),
            )
            self._update_index(index.id, status="indexing", error=None)
            IndexBuildProcessRunner(
                self.service_for(index.id).settings,
                timeout_seconds=self.settings.index_build_timeout_seconds,
            ).build(cancel=cancel_event)
            stats = self.service_for(index.id).reload_cached_index()
            self._update_index(
                index.id,
                status="ready",
                document_count=stats.document_count,
                chunk_count=stats.chunk_count,
                updated_at=_now(),
                error=None,
            )
            result_payload: dict[str, Any] = {
                "phase": "indexed",
                "generation_mode": "gigacode",
                "index_id": index.id,
                "index_name": index.name,
                "repository_ids": repository_ids,
                "target_count": len(targets),
                "targets": targets,
                "files": available_files,
                "gigacode_runs": gigacode_runs,
                "gigacode_errors": gigacode_errors,
                "gigacode_success_count": successful_runs,
                "gigacode_failure_count": len(gigacode_errors),
                "fallback_used": bool(gigacode_errors),
                "document_count": stats.document_count,
                "chunk_count": stats.chunk_count,
                "server_llm_used": False,
                "gigacode_used": True,
            }
            if complete_job:
                self._update_job(
                    job_id,
                    status="completed",
                    message=(
                        f"GigaCode analyzed {successful_runs}/{len(targets)} targets, "
                        f"used {len(gigacode_errors)} safe fallbacks and rebuilt "
                        f"index {index.name}"
                    ),
                    completed_at=_now(),
                    result=result_payload,
                )
            else:
                self._append_job_log(
                    job_id,
                    f"GigaCode index batch ready: index={index.id}; targets={len(targets)}",
                )
            return result_payload
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _ssot_target(
        job_id: str,
        service: ServiceRecord,
        repository: RepositorySource,
    ) -> dict[str, Any]:
        return {
            "id": service.id,
            "kind": "service",
            "name": service.name,
            "service_id": service.id,
            "repository_id": repository.id,
            "repository_name": repository.name,
            "module_path": service.module_path,
            "module_state": service.module_state,
            "context_call": {
                "action": "context",
                "job_id": job_id,
                "service_id": service.id,
            },
        }

    @staticmethod
    def _static_ssot_markdown(
        service: ServiceRecord,
        repository: RepositorySource,
        service_map: ServiceMapSnapshot,
    ) -> str:
        lines = [
            f"# {service.name}",
            "",
            (
                "Minimal SSOT generated from static source analysis. It records only "
                "interfaces and relationships observed in the current checkout."
            ),
            "",
            "## Service identity",
            "",
            f"- Service ID: `{service.id}`",
            f"- Repository: `{repository.name}`",
            f"- Module: `{service.module_path}`",
            f"- Build: `{service.build_system}`",
            f"- Module state: `{service.module_state}`",
            f"- Owner: `{service.owner or 'unknown'}`",
            "",
            "## Inbound interfaces",
            "",
        ]
        if service.entrypoints:
            lines.extend(
                f"- {item.kind} `{item.operation}` — {item.description} "
                f"(evidence: {', '.join(item.evidence_ids) or 'static extractor'})"
                for item in service.entrypoints
            )
        else:
            lines.append("- No inbound interface was found by the static analyzer.")
        lines.extend(["", "## Outbound interfaces", ""])
        if service.outbound_interfaces:
            lines.extend(
                f"- {item.kind} `{item.operation}` → "
                f"`{item.target_hint or 'unresolved target'}` — {item.description} "
                f"(evidence: {', '.join(item.evidence_ids) or 'static extractor'})"
                for item in service.outbound_interfaces
            )
        else:
            lines.append("- No outbound interface was found by the static analyzer.")
        dependencies = [
            item for item in service_map.dependencies if item.source_service_id == service.id
        ]
        lines.extend(["", "## Resolved and candidate dependencies", ""])
        if dependencies:
            lines.extend(
                f"- {item.protocol} `{item.operation}` → "
                f"`{item.target_service_id or item.target_hint}` "
                f"(confidence: {item.confidence}; status: {item.status}; "
                f"origin: {item.origin})"
                for item in dependencies
            )
        else:
            lines.append("- No service dependency was found by the static analyzer.")
        lines.extend(
            [
                "",
                "## Analysis limits",
                "",
                (
                    "- This document is source-derived and does not assert runtime traffic, "
                    "deployment topology, ownership, or behavior not visible in annotations "
                    "and configuration."
                ),
                "- Missing interfaces mean 'not detected', not necessarily 'does not exist'.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _repository_fallback_ssot_markdown(
        target: dict[str, Any],
        repository: RepositorySource,
    ) -> str:
        return "\n".join(
            [
                f"# {target['name']}",
                "",
                (
                    "Minimal SSOT generated from the deterministic repository scan because "
                    "optional model enrichment was unavailable."
                ),
                "",
                "## Repository identity",
                "",
                f"- Repository: `{repository.name}`",
                f"- Module: `{target['module_path']}`",
                f"- Module state: `{target['module_state']}`",
                f"- Commit: `{repository.commit or 'unknown'}`",
                "",
                "## Analysis limits",
                "",
                "- Static analysis did not discover a complete service module.",
                "- Missing interfaces are unknown and are not evidence that they do not exist.",
            ]
        )

    @staticmethod
    def _gigacode_failure_message(exc: Exception) -> str:
        detail = " ".join(str(exc).split()) or "no error details"
        return f"{type(exc).__name__}: {detail}"[:4000]

    def _write_static_ssot_documents(
        self,
        job_id: str,
        service_ids: set[str],
        service_map: ServiceMapSnapshot,
        repository_by_service: dict[str, RepositorySource],
    ) -> list[str]:
        services = {item.id: item for item in service_map.services}
        published: list[str] = []
        for service_id in sorted(service_ids):
            service = services.get(service_id)
            repository = repository_by_service.get(service_id)
            if service is None or repository is None:
                continue
            markdown = self._static_ssot_markdown(service, repository, service_map)
            target = self._ssot_target(job_id, service, repository)
            document = self._client_ssot_document(
                markdown,
                target,
                repository,
                generated_by="kb_generate_system_ssot/static-analysis",
                authority="static-source-analysis",
            )
            knowledge_root = Path(self._record(repository.index_id).knowledge_dir).resolve()
            destination = (
                knowledge_root / "ssot" / "generated" / f"{_slug(service.id)}.md"
            ).resolve()
            if not destination.is_relative_to(knowledge_root):
                raise RuntimeError("Refusing to publish static SSOT outside knowledge root")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(document, encoding="utf-8")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            relative = destination.relative_to(knowledge_root).as_posix()
            published.append(f"{repository.index_id}:{relative}")
            self._append_job_log(
                job_id,
                f"Static SSOT ready: service={service.id}; index={repository.index_id}; "
                f"path={relative}",
            )
        return published

    def _remove_generated_ssot(self, index_id: str, service_ids: set[str]) -> int:
        knowledge_root = Path(self._record(index_id).knowledge_dir).resolve()
        removed = 0
        for service_id in service_ids:
            path = (knowledge_root / "ssot" / "generated" / f"{_slug(service_id)}.md").resolve()
            if not path.is_relative_to(knowledge_root):
                raise RuntimeError("Refusing to delete generated SSOT outside knowledge root")
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def _gigacode_authentication_required(
        self,
        job_id: str,
        service_id: str,
        url: str,
    ) -> None:
        self._update_job(
            job_id,
            message="GigaCode ожидает вход через браузер",
            result={
                "phase": "awaiting_authentication",
                "generation_mode": "gigacode",
                "service_id": service_id,
                "authentication_url": url,
            },
        )
        self._append_job_log(
            job_id,
            f"GigaCode authentication URL for service={service_id}: {url}",
        )

    def _gigacode_authentication_completed(
        self,
        job_id: str,
        service_id: str,
    ) -> None:
        self._update_job(
            job_id,
            message=f"GigaCode: вход завершён, анализируется {service_id}",
            result={
                "phase": "analyzing",
                "generation_mode": "gigacode",
                "service_id": service_id,
            },
        )
        self._append_job_log(
            job_id,
            f"GigaCode authentication completed for service={service_id}; analysis resumed",
        )

    def _gigacode_ssot_prompt(
        self,
        *,
        target: dict[str, Any],
        repository: RepositorySource,
        analysis: dict[str, Any],
        existing_ssot: str | None,
    ) -> str:
        return "\n\n".join(
            (
                "You are running as a read-only repository analyst. Inspect the checkout with "
                "GigaCode read/list/glob/grep tools. Do not modify files and do not execute "
                "project code. Build one concise evidence-backed service SSOT Markdown document. "
                "Cover observed purpose, APIs, events, jobs, outbound calls, dependencies, runtime "
                "and build facts. Label conservative inference and unknowns explicitly. Never "
                "invent business rules, owners, SLAs, security guarantees or runtime behavior. "
                "Use repository-relative file references and static evidence IDs where available. "
                "Return the required structured object with markdown, analyzed_files and "
                "blocking_unknowns.",
                "TARGET:\n" + json.dumps(target, ensure_ascii=False, indent=2),
                "REPOSITORY:\n"
                + json.dumps(
                    {
                        "id": repository.id,
                        "name": repository.name,
                        "git_url": repository.git_url,
                        "commit": repository.commit,
                        "module_path": target["module_path"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                "STATIC ANALYSIS:\n" + json.dumps(analysis, ensure_ascii=False, indent=2),
                "GENERATION CONTRACT:\n" + self._ssot_skill_instructions(),
                (
                    "EXISTING GENERATED SSOT TO REVISE:\n" + existing_ssot
                    if existing_ssot
                    else "There is no existing generated SSOT."
                ),
            )
        )

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
        skip_service_ids: set[str] | None = None,
        analysis_mode: Literal["static", "gigacode"] = "static",
        verify_all: bool = False,
        algorithm: str | None = None,
    ) -> GraphSnapshot:
        if job_id is not None and self._analysis_lock.locked():
            self._append_job_log(job_id, "Waiting for another graph analysis to finish")
        with self._cancellable_lock(self._analysis_lock, cancel_event):
            return self._build_graph_unlocked(
                cancel_event=cancel_event,
                job_id=job_id,
                force_service_ids=force_service_ids,
                force_all=force_all,
                skip_service_ids=skip_service_ids,
                analysis_mode=analysis_mode,
                verify_all=verify_all,
                algorithm=algorithm,
            )

    def _build_graph_unlocked(
        self,
        *,
        cancel_event: threading.Event | None,
        job_id: str | None,
        force_service_ids: set[str] | None,
        force_all: bool,
        skip_service_ids: set[str] | None,
        analysis_mode: Literal["static", "gigacode"],
        verify_all: bool,
        algorithm: str | None,
    ) -> GraphSnapshot:
        with self._lock:
            repositories = [item.model_copy(deep=True) for item in self._state.repositories]
        available_repositories = [
            item for item in repositories if Path(item.checkout_path).is_dir()
        ]
        unavailable_repositories = [
            item for item in repositories if not Path(item.checkout_path).is_dir()
        ]
        if job_id is not None:
            self._append_job_log(
                job_id,
                f"Analyzing {len(available_repositories)} available repositories; "
                f"retaining archived analysis for {len(unavailable_repositories)} "
                "documentation-only repositories",
            )
        inputs = self._repository_inputs(available_repositories)
        previous_graph = self._graph_store.load()
        retained_graph = self._retained_graph_excluding(
            previous_graph,
            available_repositories,
        )

        graph_settings = self._graph_settings(algorithm)

        def merge_retained(active_result: ServiceMapBuildResult) -> ServiceMapBuildResult:
            if not unavailable_repositories:
                return active_result
            merged = merge_and_relink_snapshots([retained_graph, active_result.graph])
            projected = ServiceMapBuilder(graph_settings).from_graph(
                merged,
                self._repository_inputs(repositories),
            )
            return ServiceMapBuildResult(
                graph=projected.graph,
                service_map=projected.service_map,
                partial=active_result.partial,
            )

        analysis_timeout = self.settings.repository_analysis_timeout_seconds
        if force_all:
            analysis_timeout = max(
                analysis_timeout,
                min(14_400, max(1, len(inputs)) * 30),
            )
            if (
                job_id is not None
                and analysis_timeout != self.settings.repository_analysis_timeout_seconds
            ):
                self._append_job_log(
                    job_id,
                    f"Full rebuild timeout scaled to {analysis_timeout}s for "
                    f"{len(inputs)} repositories",
                )
        runner = ServiceMapProcessRunner(
            graph_settings,
            timeout_seconds=analysis_timeout,
        )
        build_options: dict[str, Any] = {}
        build_parameters = inspect.signature(runner.build).parameters
        if "force_service_ids" in build_parameters:
            build_options["force_service_ids"] = force_service_ids
        if "force_all" in build_parameters:
            build_options["force_all"] = force_all
        if "skip_service_ids" in build_parameters:
            build_options["skip_service_ids"] = skip_service_ids
        result = runner.build(
            inputs,
            cancel=cancel_event,
            progress=(
                (lambda message: self._append_job_log(job_id, message))
                if job_id is not None
                else None
            ),
            checkpoint=(
                None
                if force_all
                else lambda checkpoint: self._publish_analysis_checkpoint(
                    merge_retained(checkpoint)
                )
            ),
            **build_options,
        )
        if force_all and result.partial:
            if job_id is not None:
                self._append_job_log(
                    job_id,
                    "Full rebuild produced only a partial checkpoint; previous graph preserved",
                )
            raise RuntimeError(
                "Full graph rebuild did not finish before the analysis deadline; "
                "the previous graph snapshot was preserved"
            )
        result = merge_retained(result)
        verification: dict[str, Any] = {}
        if analysis_mode == "gigacode" and not result.partial:
            if job_id is not None:
                self._update_job(
                    job_id,
                    message="Static graph ready; GigaCode is verifying dependency evidence",
                    result={
                        "phase": "gigacode_verification",
                        "generation_mode": "gigacode",
                    },
                )
            try:
                result, verification = self._graph_verifier.verify(
                    result,
                    inputs,
                    verify_all=verify_all,
                    cancel=cancel_event,
                    progress=(
                        (lambda message: self._append_job_log(job_id, message))
                        if job_id is not None
                        else None
                    ),
                    authentication_url=(
                        (
                            lambda target, url: self._gigacode_authentication_required(
                                job_id, target, url
                            )
                        )
                        if job_id is not None
                        else None
                    ),
                    authentication_complete=(
                        (lambda target: self._gigacode_authentication_completed(job_id, target))
                        if job_id is not None
                        else None
                    ),
                )
            except GigaCodeCancelled:
                raise
            except Exception as exc:
                error = self._gigacode_failure_message(exc)
                verification = {
                    "failed": 1,
                    "fallback": "static-graph",
                    "warnings": [f"GigaCode verification failed: {error}"],
                }
                if job_id is not None:
                    self._append_job_log(
                        job_id,
                        f"GigaCode graph fallback: static graph preserved; error={error}",
                    )
        elif analysis_mode == "gigacode":
            verification = {
                "skipped": True,
                "reason": "Static analysis published only a partial checkpoint",
            }
        gigacode_enriched = (
            analysis_mode == "gigacode" and verification.get("fallback") != "static-graph"
        ) or any(edge.origin in {"gigacode", "static+gigacode"} for edge in result.graph.edges)
        result = finalize_snapshot(
            result,
            mode=(
                "partial"
                if result.partial
                else "static+gigacode"
                if gigacode_enriched
                else "static"
            ),
            verification=verification,
        )
        self._service_map_store.save(result.service_map)
        self._graph_store.save(result.graph)
        self._remove_legacy_graph_documents(job_id=job_id)
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

    def _remove_legacy_graph_documents(self, *, job_id: str | None) -> None:
        """Remove documents created by the retired graph-to-RAG publishing path."""
        with self._lock:
            indexes = [item.model_copy(deep=True) for item in self._state.indexes]
        removed = 0
        for index in indexes:
            route_root = Path(index.knowledge_dir).resolve() / "system-graph"
            if not route_root.is_dir():
                continue
            for existing in route_root.glob("*.md"):
                if not existing.is_file():
                    continue
                header = existing.read_text(encoding="utf-8", errors="replace")[:1000]
                if "authority: source-derived-graph" not in header:
                    continue
                existing.unlink()
                removed += 1
            marker = route_root / ".snapshot-id"
            if marker.is_file():
                marker.unlink()
            with suppress(OSError):
                route_root.rmdir()
        if job_id is not None:
            self._append_job_log(
                job_id,
                "Graph snapshot saved outside RAG indexes; "
                f"removed_legacy_graph_documents={removed}",
            )

    def _index_work_lock(self, index_id: str) -> threading.Lock:
        with self._lock:
            lock = self._index_work_locks.get(index_id)
            if lock is None:
                lock = threading.Lock()
                self._index_work_locks[index_id] = lock
            return lock

    @contextmanager
    def _cancellable_index_locks(
        self,
        index_ids: list[str],
        cancel_event: threading.Event | None,
    ) -> Iterator[None]:
        acquired: list[threading.Lock] = []
        try:
            for index_id in sorted(set(index_ids)):
                lock = self._index_work_lock(index_id)
                while not lock.acquire(timeout=0.1):
                    self._raise_if_cancelled(cancel_event)
                acquired.append(lock)
                self._raise_if_cancelled(cancel_event)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

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

    @staticmethod
    def _retained_graph_excluding(
        graph: GraphSnapshot,
        repositories: list[RepositorySource],
    ) -> GraphSnapshot:
        """Keep archived graph facts for repositories whose checkout has been removed."""
        if not repositories:
            return graph.model_copy(deep=True)
        paths = {str(Path(item.checkout_path).resolve()) for item in repositories}
        sources = {item.git_url for item in repositories}
        repository_names = {item.name for item in repositories}
        removed_service_ids = {
            str(node.service_id)
            for node in graph.nodes
            if node.type == "Service"
            and node.service_id
            and (
                str(node.metadata.get("repository_path") or "") in paths
                or str(node.metadata.get("source") or "") in sources
            )
        }
        retained_nodes = [
            node
            for node in graph.nodes
            if node.service_id not in removed_service_ids
            and not (
                node.type == "Repository"
                and (
                    str(node.metadata.get("path") or "") in paths
                    or str(node.metadata.get("source") or "") in sources
                )
            )
        ]
        retained_node_ids = {node.id for node in retained_nodes}
        retained_edges = [
            edge
            for edge in graph.edges
            if edge.source in retained_node_ids and edge.target in retained_node_ids
        ]
        retained_evidence_ids = {
            evidence_id for node in retained_nodes for evidence_id in node.evidence_ids
        }
        retained_evidence_ids.update(
            evidence_id for edge in retained_edges for evidence_id in edge.evidence_ids
        )
        return graph.model_copy(
            update={
                "nodes": retained_nodes,
                "edges": retained_edges,
                "evidence": [item for item in graph.evidence if item.id in retained_evidence_ids],
                "issues": [
                    item for item in graph.issues if item.repository not in repository_names
                ],
            },
            deep=True,
        )

    def _find_openspecs(
        self,
        checkout: Path,
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[Path]:
        """Find OpenSpec roots without running the full source-layout analyzer twice."""
        ignored = {
            ".git",
            ".gradle",
            ".idea",
            ".mvn",
            ".settings",
            "build",
            "dist",
            "generated",
            "node_modules",
            "out",
            "target",
            "vendor",
        }
        found: list[Path] = []
        for current, directories, _files in os.walk(checkout, followlinks=False):
            self._raise_if_cancelled(cancel_event)
            root = Path(current)
            retained: list[str] = []
            for name in directories:
                path = root / name
                if path.is_symlink() or name.startswith(".") or name in ignored:
                    continue
                if name.lower() == "openspec":
                    found.append(path.resolve())
                    continue
                retained.append(name)
            directories[:] = retained
        return sorted(set(found))

    @staticmethod
    def _services_owning_openspec(
        services: list[ServiceRecord],
        checkout: Path,
        openspecs: list[Path],
    ) -> set[str]:
        """Assign each OpenSpec root to its most specific discovered service module."""
        checkout = checkout.resolve()
        service_roots: list[tuple[ServiceRecord, Path]] = []
        for service in services:
            module_path = Path(service.module_path or ".")
            root = (checkout / module_path).resolve()
            if root.is_relative_to(checkout):
                service_roots.append((service, root))
        owners: set[str] = set()
        for openspec in openspecs:
            matches = [
                (service, root)
                for service, root in service_roots
                if openspec.resolve().is_relative_to(root)
            ]
            if not matches:
                continue
            deepest = max(len(root.parts) for _service, root in matches)
            owners.update(service.id for service, root in matches if len(root.parts) == deepest)
        return owners

    def _rebuild_index(
        self,
        index_id: str,
        cancel_event: threading.Event | None,
    ) -> Any:
        self._update_index(index_id, status="indexing", error=None)
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
        return stats

    def _restore_index_status(self, index_id: str) -> None:
        try:
            stats = self.service_for(index_id).stats()
            self._update_index(
                index_id,
                status="ready",
                document_count=stats.document_count,
                chunk_count=stats.chunk_count,
                updated_at=_now(),
                error=None,
            )
        except Exception:
            self._update_index(index_id, status="empty", updated_at=_now(), error=None)

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
                        and path.suffix.lower() in SUPPORTED_DOCUMENT_SUFFIXES
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

    @staticmethod
    def _safe_uploaded_document_path(root: Path, raw_path: str) -> Path:
        normalized = raw_path.strip().replace("\\", "/")
        relative = Path(normalized)
        if not normalized or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Document path must be a safe relative path")
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("Hidden document paths are not allowed")
        if relative.suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES:
            supported = ", ".join(sorted(SUPPORTED_DOCUMENT_SUFFIXES))
            raise ValueError(f"Unsupported document type; allowed: {supported}")
        target = (root / "uploads" / relative).resolve()
        uploads_root = (root / "uploads").resolve()
        if not target.is_relative_to(uploads_root):
            raise ValueError("Document path escapes the index upload directory")
        return target

    @staticmethod
    def _document_origin(source_path: str) -> str:
        if source_path.startswith("repositories/"):
            return "repository"
        if source_path.startswith("uploads/"):
            return "upload"
        if source_path.startswith("ssot/"):
            return "ssot"
        return "local"

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

    def _upsert_repository(self, repository: RepositorySource) -> None:
        with self._lock:
            self._state.repositories = [
                item for item in self._state.repositories if item.id != repository.id
            ]
            self._state.repositories.append(repository)
            self._refresh_source_counts_locked()
            self._save_locked()

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

    def _cleanup_managed_repository_checkout(self, repository_id: str, *, job_id: str) -> None:
        if not self.settings.repository_cleanup_after_scan:
            return
        try:
            repository = self._repository(repository_id)
        except KeyError:
            return
        checkout = Path(repository.checkout_path).resolve()
        cache_root = self.settings.repository_cache_dir.resolve()
        marker = checkout / ".gigacode-graph-source.json"
        try:
            if repository.checkout_state == "removed" and not checkout.exists():
                return
            managed_path = checkout != cache_root and checkout.is_relative_to(cache_root)
            if not managed_path:
                if repository.checkout_state != "external":
                    self._upsert_repository(
                        repository.model_copy(update={"checkout_state": "external"})
                    )
                self._append_job_log(
                    job_id,
                    f"Checkout cleanup skipped for user-owned source: {checkout}",
                )
                return
            if checkout.exists() and not marker.is_file():
                self._append_job_log(
                    job_id,
                    f"Checkout cleanup refused because the managed marker is missing: {checkout}",
                )
                return
            if checkout.exists():
                shutil.rmtree(checkout)
            removed_at = _now()
            self._upsert_repository(
                repository.model_copy(
                    update={
                        "checkout_state": "removed",
                        "checkout_removed_at": removed_at,
                    }
                )
            )
            self._append_job_log(
                job_id,
                "Managed checkout removed after analysis; retained documentation at "
                f"{repository.documentation_path or 'the linked knowledge index'}",
            )
        except Exception as exc:
            self._append_job_log(
                job_id,
                "Checkout cleanup failed without invalidating analysis: "
                f"{type(exc).__name__}: {exc}",
            )

    def _ensure_repository_checkout(
        self,
        repository_id: str,
        *,
        job_id: str,
        cancel_event: threading.Event | None,
    ) -> RepositorySource:
        repository = self._repository(repository_id)
        if Path(repository.checkout_path).is_dir():
            return repository
        self._append_job_log(
            job_id,
            f"Temporarily restoring checkout for analysis: {repository.name}",
        )
        paths, records = RepositorySourceManager(self._graph_settings()).materialize(
            [RepositorySpec(source=repository.git_url, ref=repository.ref)],
            refresh=True,
            cancel_event=cancel_event,
        )
        checkout = paths[0]
        ingestion = records[0]
        replacement = repository.model_copy(
            update={
                "checkout_path": str(checkout),
                "checkout_state": ("external" if ingestion.source_type == "local" else "available"),
                "checkout_removed_at": None,
                "commit": ingestion.commit,
            }
        )
        self._upsert_repository(replacement)
        return replacement

    def _cleanup_repository_checkouts(
        self,
        repository_ids: set[str],
        *,
        job_id: str,
    ) -> None:
        for repository_id in sorted(repository_ids):
            self._cleanup_managed_repository_checkout(repository_id, job_id=job_id)

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
        job_type: Literal["index", "repository", "graph", "service", "ssot", "cleanup"],
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

    def _job_log_files(self) -> list[Path]:
        root = self.settings.job_logs_dir.resolve()
        if not root.is_dir():
            return []
        return sorted(
            path
            for path in root.glob("*.log")
            if path.parent == root and path.is_file() and not path.is_symlink()
        )

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

    def _graph_settings(self, algorithm: str | None = None) -> GraphSettings:
        return GraphSettings(
            store_path=self.settings.graph_store_path,
            repository_cache_path=self.settings.repository_cache_dir,
            module_cache_path=self.settings.repository_cache_dir.parent / "module-analysis",
            ingestion_path=self.settings.repository_cache_dir.parent / "graph-ingestion.json",
            git_timeout_seconds=self.settings.repository_git_timeout_seconds,
            builder_algorithm=algorithm or GraphSettings().builder_algorithm,
        ).resolved()

    @staticmethod
    def _repository_source_key(git_url: str) -> str:
        """Return one stable identity for common URL spellings of a Git repository."""
        clean = git_url.strip().replace("\\", "/").rstrip("/")
        parsed = urlsplit(clean)
        if parsed.scheme and parsed.scheme != "file":
            host = (parsed.hostname or "").casefold()
            path = unquote(parsed.path).strip("/")
            if path.casefold().endswith(".git"):
                path = path[:-4]
            return f"remote:{host}/{path.casefold()}"
        if parsed.scheme == "file":
            return f"local:{Path(unquote(parsed.path)).expanduser().resolve()}"

        scp_match = re.fullmatch(r"(?:[^@/:]+@)?([^:]+):(.+)", clean)
        if scp_match:
            host, path = scp_match.groups()
            path = path.strip("/")
            if path.casefold().endswith(".git"):
                path = path[:-4]
            return f"remote:{host.casefold()}/{path.casefold()}"

        return f"local:{Path(clean).expanduser().resolve()}"

    def _repository_for_source_key_locked(
        self,
        source_key: str,
    ) -> RepositorySource | None:
        return next(
            (
                repository
                for repository in self._state.repositories
                if self._repository_source_key(repository.git_url) == source_key
            ),
            None,
        )

    def _release_repository_import_reservation(self, git_url: str) -> None:
        with self._lock:
            self._repository_import_reservations.discard(
                self._repository_source_key(git_url)
            )

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
        self._state.jobs = sorted(self._jobs.values(), key=lambda item: item.id)
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
