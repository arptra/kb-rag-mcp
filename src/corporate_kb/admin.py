"""Protected administration primitives shared by the built-in web dashboard."""

from __future__ import annotations

import os
import resource
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from corporate_kb.service import KnowledgeService, create_service
from corporate_kb.usage import UsageTracker

_SUPPORTED_DOCUMENT_SUFFIXES = {".md", ".markdown", ".html", ".htm", ".txt"}


class AdminController:
    """Manage uploads and one background index job without exposing arbitrary filesystem access."""

    def __init__(self, service: KnowledgeService, usage: UsageTracker) -> None:
        self._service = service
        self._usage = usage
        self._started_monotonic = time.monotonic()
        self._lock = Lock()
        self._index_job: dict[str, Any] = {
            "status": "idle",
            "started_at": None,
            "finished_at": None,
            "elapsed_seconds": None,
            "documents": None,
            "chunks": None,
            "error": None,
        }

    def overview(self) -> dict[str, Any]:
        stats = self._service.stats()
        documents = self._service.list_documents(limit=100)
        with self._lock:
            index_job = dict(self._index_job)
        return {
            "index": stats.model_dump(mode="json"),
            "usage": self._usage.snapshot(),
            "server_metrics": self._server_metrics(),
            "index_job": index_job,
            "documents": [
                {
                    "document_id": document.document_id,
                    "title": document.title,
                    "source_path": document.source_path,
                    "source_type": document.source_type,
                    "loaded_at": document.loaded_at.isoformat(),
                }
                for document in documents
            ],
        }

    def _server_metrics(self) -> dict[str, Any]:
        cpu_cores = max(1, os.cpu_count() or 1)
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
        except OSError:
            load_1m = load_5m = load_15m = 0.0
        raw_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_bytes = raw_rss if sys.platform == "darwin" else raw_rss * 1024
        return {
            "cpu_cores": cpu_cores,
            "load_percent": round(min(100.0, load_1m / cpu_cores * 100), 1),
            "load_1m": round(load_1m, 2),
            "load_5m": round(load_5m, 2),
            "load_15m": round(load_15m, 2),
            "peak_rss_mb": round(rss_bytes / 1024 / 1024, 1),
            "uptime_seconds": round(time.monotonic() - self._started_monotonic),
        }

    def upload_document(
        self,
        *,
        relative_path: str,
        content: str,
        overwrite: bool,
    ) -> dict[str, Any]:
        settings = self._service.settings
        encoded = content.encode("utf-8")
        if len(encoded) > settings.admin_max_upload_bytes:
            raise ValueError(
                f"Document exceeds KB_ADMIN_MAX_UPLOAD_BYTES={settings.admin_max_upload_bytes}"
            )
        target = self._safe_document_path(relative_path)
        if target.exists() and not overwrite:
            raise ValueError("Document already exists; enable overwrite to replace it")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary_path = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return {
            "status": "uploaded",
            "source_path": target.relative_to(settings.knowledge_dir).as_posix(),
            "bytes": len(encoded),
            "index_required": True,
        }

    def start_index(self) -> dict[str, Any]:
        with self._lock:
            if self._index_job["status"] == "running":
                raise RuntimeError("Index build is already running")
            self._index_job = {
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "finished_at": None,
                "elapsed_seconds": None,
                "documents": None,
                "chunks": None,
                "error": None,
            }
        Thread(target=self._build_index, name="kb-admin-index", daemon=True).start()
        return self.index_job()

    def index_job(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._index_job)

    def _build_index(self) -> None:
        started = time.perf_counter()
        try:
            builder = create_service(self._service.settings)
            stats = builder.build_index(force=True, reuse_unchanged=True)
            self._service.reload_cached_index()
            update = {
                "status": "completed",
                "documents": stats.document_count,
                "chunks": stats.chunk_count,
                "error": None,
            }
        except Exception as exc:
            update = {
                "status": "failed",
                "documents": None,
                "chunks": None,
                "error": str(exc),
            }
        with self._lock:
            self._index_job.update(
                update,
                finished_at=datetime.now(UTC).isoformat(),
                elapsed_seconds=round(time.perf_counter() - started, 2),
            )

    def _safe_document_path(self, raw_path: str) -> Path:
        normalized = raw_path.strip().replace("\\", "/")
        relative = Path(normalized)
        if not normalized or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Document path must be a safe relative path")
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("Hidden document paths are not allowed")
        if relative.suffix.lower() not in _SUPPORTED_DOCUMENT_SUFFIXES:
            supported = ", ".join(sorted(_SUPPORTED_DOCUMENT_SUFFIXES))
            raise ValueError(f"Unsupported document type; allowed: {supported}")
        root = self._service.settings.knowledge_dir.resolve()
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            raise ValueError("Document path escapes KB_KNOWLEDGE_DIR")
        return target
