"""Validated, pickle-free, atomically-written disk cache."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, ValidationError

from corporate_kb.embeddings.base import matrix_is_finite
from corporate_kb.history import normalize_document, version_fields
from corporate_kb.models import Chunk, Document

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 2


class CacheManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_schema_version: int
    created_at: datetime
    embedding_provider: str
    embedding_model: str
    embedding_dimension: int
    embedding_cache_identity: str
    query_instruction: str
    chunk_size: int
    chunk_overlap: int
    chunk_hard_max: int
    knowledge_hash: str
    document_count: int
    chunk_count: int
    embeddings_shape: list[int]


@dataclass(frozen=True, slots=True)
class CachedIndex:
    documents: list[Document]
    chunks: list[Chunk]
    embeddings: NDArray[np.float32]
    manifest: CacheManifest


class CacheManager:
    """Persist cache artifacts and reject any incompatible or corrupt set."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir.resolve()
        try:
            self._bind_paths(self.snapshot_directory())
        except (OSError, ValueError):
            # load() reports corrupt pointers as a cache miss; a rebuild can recover.
            self._bind_paths(self.cache_dir)

    def _bind_paths(self, directory: Path) -> None:
        self.manifest_path = directory / "manifest.json"
        self.documents_path = directory / "documents.json"
        self.chunks_path = directory / "chunks.json"
        self.embeddings_path = directory / "embeddings.npy"

    def snapshot_directory(self) -> Path:
        pointer = self.cache_dir / "CURRENT"
        if pointer.is_file():
            generation = pointer.read_text(encoding="ascii").strip()
            if len(generation) != 32 or any(c not in "0123456789abcdef" for c in generation):
                raise ValueError("Invalid cache generation pointer")
            return self.cache_dir / "generations" / generation
        return self.cache_dir

    def load(
        self,
        *,
        knowledge_hash: str | None,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
        """Load a cache, optionally requiring an exact knowledge hash."""
        return self._load(
            knowledge_hash=knowledge_hash,
            embedding_cache_identity=embedding_cache_identity,
            chunking=chunking,
        )

    def load_compatible(
        self,
        *,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
        """Load a structurally compatible cache even when its documents are stale."""
        return self.load(
            knowledge_hash=None,
            embedding_cache_identity=embedding_cache_identity,
            chunking=chunking,
        )

    def load_snapshot(self) -> CachedIndex | None:
        """Read a valid prior snapshot for archival, even after model/config changes."""
        try:
            manifest = CacheManifest.model_validate_json(
                (self.snapshot_directory() / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, ValidationError):
            return None
        return self.load(
            knowledge_hash=None,
            embedding_cache_identity=manifest.embedding_cache_identity,
            chunking={
                "chunk_size": manifest.chunk_size,
                "chunk_overlap": manifest.chunk_overlap,
                "chunk_hard_max": manifest.chunk_hard_max,
            },
        )

    def _load(
        self,
        *,
        knowledge_hash: str | None,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
        try:
            self._bind_paths(self.snapshot_directory())
        except (OSError, ValueError) as exc:
            logger.warning("Invalid cache generation: %s", exc)
            return None
        if not self.manifest_path.is_file():
            logger.info("Knowledge cache is absent: %s", self.manifest_path)
            return None
        try:
            manifest = CacheManifest.model_validate_json(
                self.manifest_path.read_text(encoding="utf-8")
            )
            self._validate_compatibility(
                manifest,
                knowledge_hash=knowledge_hash,
                embedding_cache_identity=embedding_cache_identity,
                chunking=chunking,
            )
            documents_raw = json.loads(self.documents_path.read_text(encoding="utf-8"))
            chunks_raw = json.loads(self.chunks_path.read_text(encoding="utf-8"))
            if not isinstance(documents_raw, list) or not isinstance(chunks_raw, list):
                raise ValueError("documents.json and chunks.json must contain JSON arrays")
            documents = [
                normalize_document(Document.model_validate(item)) for item in documents_raw
            ]
            chunks = [Chunk.model_validate(item) for item in chunks_raw]
            by_id = {document.document_id: document for document in documents}
            chunks = [
                chunk.model_copy(update=version_fields(by_id[chunk.document_id]))
                if chunk.document_id in by_id
                else chunk
                for chunk in chunks
            ]
            # Memory-map the matrix so startup does not allocate a second full copy of a
            # large index before the in-memory store takes ownership of the read-only view.
            embeddings = np.load(self.embeddings_path, mmap_mode="r", allow_pickle=False)
            matrix = np.asarray(embeddings, dtype=np.float32)
            expected_shape = (manifest.chunk_count, manifest.embedding_dimension)
            if matrix.shape != expected_shape or list(matrix.shape) != manifest.embeddings_shape:
                raise ValueError(
                    f"embeddings.npy shape {matrix.shape} does not match manifest {expected_shape}"
                )
            if len(documents) != manifest.document_count or len(chunks) != manifest.chunk_count:
                raise ValueError("Cached JSON counts do not match manifest")
            if not matrix_is_finite(matrix):
                raise ValueError("Cached embeddings contain NaN or infinite values")
            logger.info("Loaded %d chunks from cache", len(chunks))
            return CachedIndex(
                documents=documents, chunks=chunks, embeddings=matrix, manifest=manifest
            )
        except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            logger.warning("Knowledge cache is invalid and must be rebuilt: %s", exc)
            return None

    def save(
        self,
        *,
        documents: list[Document],
        chunks: list[Chunk],
        embeddings: NDArray[np.float32],
        manifest: CacheManifest,
    ) -> None:
        """Publish one complete immutable generation with a single pointer swap."""
        generation = uuid.uuid4().hex
        directory = self.cache_dir / "generations" / generation
        writer = CacheManager(directory)
        writer._save_flat(
            documents=documents, chunks=chunks, embeddings=embeddings, manifest=manifest
        )
        self.publish_generation(generation)

    def publish_generation(self, generation: str) -> None:
        if len(generation) != 32 or any(c not in "0123456789abcdef" for c in generation):
            raise ValueError("Invalid cache generation")
        directory = self.cache_dir / "generations" / generation
        if not all(
            (directory / name).is_file()
            for name in ("documents.json", "chunks.json", "embeddings.npy", "manifest.json")
        ):
            raise ValueError("Cannot publish an incomplete cache generation")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        temporary: list[Path] = []
        path = self._write_bytes(generation.encode("ascii"), temporary)
        os.replace(path, self.cache_dir / "CURRENT")
        self._fsync_directory()
        self._bind_paths(self.snapshot_directory())

    def _save_flat(
        self,
        *,
        documents: list[Document],
        chunks: list[Chunk],
        embeddings: NDArray[np.float32],
        manifest: CacheManifest,
    ) -> None:
        ids = {document.document_id for document in documents}
        if len(ids) != len(documents) or len({c.chunk_id for c in chunks}) != len(chunks):
            raise ValueError("Duplicate document or chunk IDs in cache")
        if any(chunk.document_id not in ids for chunk in chunks):
            raise ValueError("Cached chunk references an unknown document")
        if len(documents) != manifest.document_count or len(chunks) != manifest.chunk_count:
            raise ValueError("Cache manifest counts do not match documents/chunks")
        matrix = np.asarray(embeddings, dtype=np.float32)
        expected_shape = (len(chunks), manifest.embedding_dimension)
        if matrix.shape != expected_shape:
            raise ValueError(
                f"Cannot cache embeddings shape {matrix.shape}; expected {expected_shape}"
            )
        if not matrix_is_finite(matrix):
            raise ValueError("Cannot cache NaN or infinite embeddings")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        manifest_payload = manifest.model_dump_json().encode("utf-8")

        temporary: list[Path] = []
        try:
            documents_temp = self._write_models(documents, temporary)
            chunks_temp = self._write_models(chunks, temporary)
            embeddings_temp = self._write_numpy(matrix, temporary)
            manifest_temp = self._write_bytes(manifest_payload, temporary)
            os.replace(documents_temp, self.documents_path)
            os.replace(chunks_temp, self.chunks_path)
            os.replace(embeddings_temp, self.embeddings_path)
            os.replace(manifest_temp, self.manifest_path)
            self._fsync_directory()
        finally:
            for path in temporary:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove temporary cache file: %s", path)

    def _validate_compatibility(
        self,
        manifest: CacheManifest,
        *,
        knowledge_hash: str | None,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> None:
        expected: dict[str, Any] = {
            "embedding_cache_identity": embedding_cache_identity,
            **chunking,
        }
        if knowledge_hash is not None:
            expected["knowledge_hash"] = knowledge_hash
        actual = manifest.model_dump()
        if manifest.cache_schema_version not in {1, CACHE_SCHEMA_VERSION}:
            raise ValueError("Unsupported cache schema version")
        for key, value in expected.items():
            if actual.get(key) != value:
                raise ValueError(
                    f"cache mismatch for {key}: found {actual.get(key)!r}, expected {value!r}"
                )
        if manifest.embeddings_shape != [manifest.chunk_count, manifest.embedding_dimension]:
            raise ValueError("Manifest embeddings_shape is inconsistent")

    def _write_bytes(self, payload: bytes, temporary: list[Path]) -> Path:
        with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as handle:
            path = Path(handle.name)
            temporary.append(path)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _write_models(self, models: Sequence[Document | Chunk], temporary: list[Path]) -> Path:
        with tempfile.NamedTemporaryFile(
            dir=self.cache_dir, mode="w", encoding="utf-8", delete=False
        ) as handle:
            path = Path(handle.name)
            temporary.append(path)
            handle.write("[")
            for position, model in enumerate(models):
                if position:
                    handle.write(",")
                handle.write(model.model_dump_json())
            handle.write("]")
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _write_numpy(self, matrix: NDArray[np.float32], temporary: list[Path]) -> Path:
        with tempfile.NamedTemporaryFile(dir=self.cache_dir, delete=False) as handle:
            path = Path(handle.name)
            temporary.append(path)
            np.save(handle, matrix, allow_pickle=False)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.cache_dir, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
