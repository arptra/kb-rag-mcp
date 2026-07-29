"""Validated, pickle-free, atomically-written disk cache."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, ValidationError

from corporate_kb.models import Chunk, Document

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1


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
        self.manifest_path = self.cache_dir / "manifest.json"
        self.documents_path = self.cache_dir / "documents.json"
        self.chunks_path = self.cache_dir / "chunks.json"
        self.embeddings_path = self.cache_dir / "embeddings.npy"

    def load(
        self,
        *,
        knowledge_hash: str,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> CachedIndex | None:
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
            documents = [Document.model_validate(item) for item in documents_raw]
            chunks = [Chunk.model_validate(item) for item in chunks_raw]
            with self.embeddings_path.open("rb") as handle:
                embeddings = np.load(handle, allow_pickle=False)
            matrix = np.asarray(embeddings, dtype=np.float32)
            expected_shape = (manifest.chunk_count, manifest.embedding_dimension)
            if matrix.shape != expected_shape or list(matrix.shape) != manifest.embeddings_shape:
                raise ValueError(
                    f"embeddings.npy shape {matrix.shape} does not match manifest {expected_shape}"
                )
            if len(documents) != manifest.document_count or len(chunks) != manifest.chunk_count:
                raise ValueError("Cached JSON counts do not match manifest")
            if not np.isfinite(matrix).all():
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
        matrix = np.asarray(embeddings, dtype=np.float32)
        expected_shape = (len(chunks), manifest.embedding_dimension)
        if matrix.shape != expected_shape:
            raise ValueError(
                f"Cannot cache embeddings shape {matrix.shape}; expected {expected_shape}"
            )
        if not np.isfinite(matrix).all():
            raise ValueError("Cannot cache NaN or infinite embeddings")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        document_payload = json.dumps(
            [item.model_dump(mode="json") for item in documents],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        chunk_payload = json.dumps(
            [item.model_dump(mode="json") for item in chunks],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest_payload = manifest.model_dump_json().encode("utf-8")

        temporary: list[Path] = []
        try:
            documents_temp = self._write_bytes(document_payload, temporary)
            chunks_temp = self._write_bytes(chunk_payload, temporary)
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
        knowledge_hash: str,
        embedding_cache_identity: str,
        chunking: dict[str, int],
    ) -> None:
        expected: dict[str, Any] = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "knowledge_hash": knowledge_hash,
            "embedding_cache_identity": embedding_cache_identity,
            **chunking,
        }
        actual = manifest.model_dump()
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
