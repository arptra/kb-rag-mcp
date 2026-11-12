"""Index lifecycle and query orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from corporate_kb.cache.manager import (
    CACHE_SCHEMA_VERSION,
    CachedIndex,
    CacheManager,
    CacheManifest,
)
from corporate_kb.chunking.structural_chunker import SimpleTokenCounter, StructuralChunker
from corporate_kb.config import Settings
from corporate_kb.embeddings.base import EmbeddingProvider
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.embeddings.sentence_transformer import SentenceTransformerEmbeddingProvider
from corporate_kb.history import HistoryStore, embedding_key, normalize_document, utc_datetime
from corporate_kb.loaders.filesystem import FileSystemDocumentLoader
from corporate_kb.models import Chunk, Document, IndexStats, SearchFilters, SearchResult
from corporate_kb.stores.base import KnowledgeStore
from corporate_kb.stores.memory import InMemoryKnowledgeStore

logger = logging.getLogger(__name__)


class KnowledgeIndexMissingError(RuntimeError):
    """Raised when auto-indexing is disabled and no compatible cache exists."""


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "hash":
        return HashEmbeddingProvider(dimension=settings.embedding_dimension)
    return SentenceTransformerEmbeddingProvider(
        model_name=settings.embedding_model,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        max_seq_length=settings.embedding_max_seq_length,
        dimension=settings.embedding_dimension,
        query_instruction=settings.query_instruction,
        local_files_only=settings.embedding_local_files_only,
    )


class KnowledgeService:
    """Coordinates loaders, chunking, embeddings, cache, and a replaceable store."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: EmbeddingProvider | None = None,
        store: KnowledgeStore | None = None,
        loader: FileSystemDocumentLoader | None = None,
        chunker: StructuralChunker | None = None,
        cache: CacheManager | None = None,
    ) -> None:
        self.settings = settings.resolved()
        self.provider = provider or create_embedding_provider(self.settings)
        self.store = store or InMemoryKnowledgeStore()
        self.loader = loader or FileSystemDocumentLoader()
        self.chunker = chunker or StructuralChunker(
            SimpleTokenCounter(),
            target_tokens=self.settings.chunk_size_tokens,
            hard_max_tokens=self.settings.chunk_hard_max_tokens,
            overlap_tokens=self.settings.chunk_overlap_tokens,
        )
        self.cache = cache or CacheManager(self.settings.cache_dir)
        history_root = getattr(
            self.settings, "history_dir", self.settings.knowledge_dir.parent / "var" / "history"
        )
        history_key = getattr(self.settings, "history_index_id", None) or str(
            self.settings.knowledge_dir.resolve()
        )
        self.history = HistoryStore(Path(history_root), history_key)
        self._progress: Callable[[dict[str, object]], None] | None = None
        self._stats: IndexStats | None = None
        self._lock = threading.RLock()

    def build_index(
        self,
        force: bool = False,
        *,
        reuse_unchanged: bool = False,
        progress: Callable[[dict[str, object]], None] | None = None,
    ) -> IndexStats:
        """Build or reuse an index, replacing the active store only after full validation."""
        with self._lock:
            self._progress = progress
            self._report("loading", message="Loading source documents")
            started = time.perf_counter()
            documents = self._load_documents()
            knowledge_hash = self._knowledge_hash(documents)
            if not force:
                cached = self.cache.load(
                    knowledge_hash=knowledge_hash,
                    embedding_cache_identity=self.provider.cache_identity,
                    chunking=self.chunker.identity,
                )
                if cached is not None and not self.history.has_pending_chunks(
                    self.provider.cache_identity
                ):
                    self.store.replace_index(cached.documents, cached.chunks, cached.embeddings)
                    self._archive_cached_snapshot(cached)
                    self.history.activate(cached.documents)
                    self._stats = self._stats_from_cache(cached.manifest)
                    logger.info(
                        "Loaded compatible knowledge index: documents=%d chunks=%d in %.3f seconds",
                        self._stats.document_count,
                        self._stats.chunk_count,
                        time.perf_counter() - started,
                    )
                    return self._stats

            return self._build_index_locked(
                documents=documents,
                knowledge_hash=knowledge_hash,
                started=started,
                reuse_cache=reuse_unchanged or not force,
            )

    def load_or_build_index(self) -> IndexStats:
        """Load a compatible cache; build only when explicitly allowed."""
        with self._lock:
            if self._stats is not None:
                return self._stats
            started = time.perf_counter()
            documents = self._load_documents()
            knowledge_hash = self._knowledge_hash(documents)
            cached = self.cache.load(
                knowledge_hash=knowledge_hash,
                embedding_cache_identity=self.provider.cache_identity,
                chunking=self.chunker.identity,
            )
            if cached is not None:
                self.store.replace_index(cached.documents, cached.chunks, cached.embeddings)
                self._archive_cached_snapshot(cached)
                self.history.activate(cached.documents)
                self._stats = self._stats_from_cache(cached.manifest)
                logger.info(
                    "Loaded compatible knowledge index: documents=%d chunks=%d in %.3f seconds",
                    self._stats.document_count,
                    self._stats.chunk_count,
                    time.perf_counter() - started,
                )
                return self._stats
            if not self.settings.auto_index:
                raise KnowledgeIndexMissingError(
                    "Knowledge index is missing or incompatible.\nRun: ./scripts/dev.sh index"
                )
            logger.info(
                "No compatible cache found; building index from %d documents",
                len(documents),
            )
            return self._build_index_locked(
                documents=documents,
                knowledge_hash=knowledge_hash,
                started=started,
                reuse_cache=True,
            )

    def load_cached_index(self) -> IndexStats:
        """Load the prepared cache without walking the source-document tree.

        Read-only MCP servers call this path in the normal production configuration. Freshness is
        established by the explicit indexing job; serving a query must not scan thousands of source
        documents just to recompute their hash.
        """
        with self._lock:
            if self._stats is not None:
                return self._stats
            started = time.perf_counter()
            logger.info("Loading prepared knowledge index from %s", self.settings.cache_dir)
            cached = self.cache.load_compatible(
                embedding_cache_identity=self.provider.cache_identity,
                chunking=self.chunker.identity,
            )
            if cached is None:
                raise KnowledgeIndexMissingError(
                    "Prepared knowledge index is missing or incompatible.\n"
                    "Run: ./scripts/dev.sh index"
                )
            self.store.replace_index(cached.documents, cached.chunks, cached.embeddings)
            self._archive_cached_snapshot(cached)
            self.history.activate(cached.documents)
            self._stats = self._stats_from_cache(cached.manifest)
            logger.info(
                "Loaded prepared knowledge index: documents=%d chunks=%d in %.3f seconds",
                self._stats.document_count,
                self._stats.chunk_count,
                time.perf_counter() - started,
            )
            return self._stats

    def reload_cached_index(self) -> IndexStats:
        """Atomically replace the serving store from a newly prepared compatible cache."""
        with self._lock:
            self._stats = None
            return self.load_cached_index()

    def load_read_index(self) -> IndexStats:
        """Load the serving index, rebuilding only when explicitly enabled."""
        if self.settings.auto_index:
            return self.load_or_build_index()
        return self.load_cached_index()

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        self.load_read_index()
        if not query.strip():
            raise ValueError("query must not be empty")
        started = time.perf_counter()
        query_vector = self.provider.embed_query(query)
        active_filters = (filters or SearchFilters()).model_copy(
            update={"excluded_page_ids": self.history.hidden_page_ids()}
        )
        effective_top_k = self.settings.default_top_k if top_k is None else top_k
        results = self.store.search(
            query_vector,
            top_k=effective_top_k,
            min_score=min_score,
            filters=active_filters,
        )
        logger.info(
            "Search returned %d results in %.3f seconds; filters=%s",
            len(results),
            time.perf_counter() - started,
            active_filters.active(),
        )
        return results

    def search_versions(
        self, query: str, *, filters: SearchFilters, top_k: int = 3, min_score: float | None = None
    ) -> list[SearchResult]:
        if filters.version_scope == "latest":
            return self.search(query, filters=filters, top_k=top_k, min_score=min_score)
        self.load_read_index()
        if not query.strip():
            raise ValueError("query must not be empty")
        return self.history.search(
            self.provider.embed_query(query),
            identity=self.provider.cache_identity,
            filters=filters,
            top_k=top_k,
            min_score=min_score,
        )

    def archive_document(self, document: Document, *, make_current: bool = False) -> Document:
        document = self.history.archive_document(document, make_current=make_current)
        self.history.archive_chunks(self.chunker.chunk(document))
        return document

    def set_page_visibility(self, page_id: str, visible: bool) -> None:
        self.history.set_page_visibility(page_id, visible)

    def read_page(
        self,
        page_id: str,
        *,
        source_version: str | None = None,
        as_of: str | datetime | None = None,
    ) -> Document:
        self.load_read_index()
        if not self.history.is_visible(page_id):
            raise KeyError(f"Unknown page/version: {page_id}")
        if source_version is None and as_of is None:
            documents = self.store.list_documents(SearchFilters(page_id=page_id), limit=1)
            if documents:
                return documents[0]
        document = self.history.read(page_id, source_version=source_version, as_of=as_of)
        if document is None:
            raise KeyError(f"Unknown page/version: {page_id}")
        return document

    def get_document(self, document_id: str) -> Document:
        self.load_read_index()
        document = self.store.get_document(document_id) or self.history.get_document(document_id)
        if document is None or not self.history.is_visible(document.page_id):
            raise KeyError(f"Unknown document_id: {document_id}")
        return document

    def get_chunk(self, chunk_id: str) -> Chunk:
        self.load_read_index()
        chunk = self.store.get_chunk(chunk_id) or self.history.get_chunk(chunk_id)
        if chunk is None or not self.history.is_visible(chunk.page_id):
            raise KeyError(f"Unknown chunk_id: {chunk_id}")
        return chunk

    def list_documents(
        self,
        *,
        filters: SearchFilters | None = None,
        limit: int = 50,
    ) -> list[Document]:
        self.load_read_index()
        active_filters = (filters or SearchFilters()).model_copy(
            update={"excluded_page_ids": self.history.hidden_page_ids()}
        )
        return self.store.list_documents(active_filters, limit=limit)

    def browse_documents(
        self,
        *,
        query: str = "",
        offset: int = 0,
        limit: int = 50,
        filters: SearchFilters | None = None,
    ) -> tuple[list[Document], int]:
        self.load_read_index()
        return self.store.browse_documents(
            (filters or SearchFilters()).model_copy(
                update={"excluded_page_ids": self.history.hidden_page_ids()}
            ),
            query=query,
            offset=offset,
            limit=limit,
        )

    def stats(self) -> IndexStats:
        return self.load_read_index()

    def _knowledge_hash(self, documents: list[Document]) -> str:
        payload = {
            "documents": [
                {
                    "source_path": document.source_path,
                    "source_id": document.source_id,
                    "content_hash": document.content_hash,
                    "metadata": document.metadata,
                }
                for document in sorted(documents, key=lambda item: item.source_path)
            ],
            "chunking": self.chunker.identity,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _load_documents(self) -> list[Document]:
        """Load documents with a clear phase boundary in server logs."""
        logger.info("Loading knowledge documents from %s", self.settings.knowledge_dir)
        started = time.perf_counter()
        if isinstance(self.loader, FileSystemDocumentLoader):
            loaded = self.loader.load_directory(
                self.settings.knowledge_dir,
                progress=lambda stage, count: self._report(stage, completed=count),
            )
        else:
            loaded = self.loader.load_directory(self.settings.knowledge_dir)
        documents = [
            normalize_document(document)
            for document in loaded
            if document.metadata.get("document_type") != "system_graph"
            and document.metadata.get("authority") != "source-derived-graph"
        ]
        logger.info(
            "Loaded %d knowledge documents in %.3f seconds",
            len(documents),
            time.perf_counter() - started,
        )
        return documents

    def _report(self, stage: str, **details: object) -> None:
        if self._progress is not None:
            self._progress({"stage": stage, "at": datetime.now(UTC).isoformat(), **details})

    def _embed_checkpoint_batch(self, chunks: list[Chunk], *, reuse: bool = True) -> np.ndarray:
        identity = self.provider.cache_identity
        cached = (
            self.history.cached_embeddings(
                identity, [c.embedding_text for c in chunks], self.provider.dimension
            )
            if reuse
            else {}
        )
        matrix = np.empty((len(chunks), self.provider.dimension), dtype=np.float32)
        missing: dict[str, tuple[str, list[int]]] = {}
        for position, chunk in enumerate(chunks):
            key = embedding_key(identity, chunk.embedding_text)
            if key in cached:
                matrix[position] = cached[key]
            elif key in missing:
                missing[key][1].append(position)
            else:
                missing[key] = (chunk.embedding_text, [position])
        if missing:
            fresh = self.provider.embed_documents([item[0] for item in missing.values()])
            for vector, (_, positions) in zip(fresh, missing.values(), strict=True):
                for position in positions:
                    matrix[position] = vector
        self.history.save_embeddings(identity, chunks, matrix)
        return matrix

    def _build_index_locked(
        self,
        *,
        documents: list[Document],
        knowledge_hash: str,
        started: float,
        reuse_cache: bool,
    ) -> IndexStats:
        """Build an index from already-loaded documents while the service lock is held."""
        chunk_started = time.perf_counter()
        chunks: list[Chunk] = []
        progress_step = max(100, len(documents) // 20 or 1)
        # Migrate the last legacy snapshot before a rebuild discards its exact IDs.
        previous = self.cache.load_snapshot()
        if previous is not None:
            self._archive_cached_snapshot(previous)
        del previous
        # Exports can contain multiple files for one logical page. Preserve every
        # captured revision, but publish exactly one current document per page.
        latest: dict[str, Document] = {}
        prepared_chunks: dict[str, list[Chunk]] = {}
        for position, document in enumerate(documents, start=1):
            normalized = self.history.archive_document(document)
            document_chunks = self.chunker.chunk(normalized)
            self.history.archive_chunks(document_chunks)
            key = normalized.page_id or normalized.source_id
            old = latest.get(key)
            if old is None or self._revision_order(normalized) > self._revision_order(old):
                latest[key] = normalized
                prepared_chunks[key] = document_chunks
            self._report("chunking", total=len(documents), completed=position)
        documents = sorted(latest.values(), key=lambda item: item.source_path)
        for position, document in enumerate(documents, start=1):
            documents[position - 1] = document.model_copy(update={"is_latest": True})
            document_chunks = prepared_chunks[document.page_id or document.source_id]
            chunks.extend(document_chunks)
            if position == 1 or position % progress_step == 0 or position == len(documents):
                logger.info(
                    "Chunked knowledge documents: %d/%d (chunks=%d)",
                    position,
                    len(documents),
                    len(chunks),
                )
        logger.info(
            "Created %d structural chunks from %d documents in %.3f seconds",
            len(chunks),
            len(documents),
            time.perf_counter() - chunk_started,
        )
        self._report("embedding", total=len(chunks), completed=0)
        self.cache.cache_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".embedding-build-", dir=self.cache.cache_dir.parent
        ) as work:
            embeddings = (
                np.lib.format.open_memmap(
                    Path(work) / "embeddings.npy",
                    mode="w+",
                    dtype=np.float32,
                    shape=(len(chunks), self.provider.dimension),
                )
                if chunks
                else np.empty((0, self.provider.dimension), dtype=np.float32)
            )
            try:
                batch_size = getattr(self.settings, "embedding_checkpoint_batch_size", 32)
                for offset in range(0, len(chunks), batch_size):
                    batch = chunks[offset : offset + batch_size]
                    embeddings[offset : offset + len(batch)] = self._embed_checkpoint_batch(
                        batch, reuse=reuse_cache
                    )
                    self._report("embedding", total=len(chunks), completed=offset + len(batch))
                # Historical backfill can be large: retain only one bounded batch in memory.
                archived_completed = 0
                for batch in self.history.pending_chunks(
                    self.provider.cache_identity, batch_size=batch_size
                ):
                    self._embed_checkpoint_batch(batch)
                    archived_completed += len(batch)
                    self._report("history_embedding", completed=archived_completed)
                indexed_at = datetime.now(UTC)
                manifest = CacheManifest(
                    cache_schema_version=CACHE_SCHEMA_VERSION,
                    created_at=indexed_at,
                    embedding_provider=self.provider.provider_name,
                    embedding_model=self.provider.model_name,
                    embedding_dimension=self.provider.dimension,
                    embedding_cache_identity=self.provider.cache_identity,
                    query_instruction=self.settings.query_instruction,
                    chunk_size=self.chunker.target_tokens,
                    chunk_overlap=self.chunker.overlap_tokens,
                    chunk_hard_max=self.chunker.hard_max_tokens,
                    knowledge_hash=knowledge_hash,
                    document_count=len(documents),
                    chunk_count=len(chunks),
                    embeddings_shape=list(embeddings.shape),
                )
                cache_started = time.perf_counter()
                self._report("saving", message="Publishing validated cache")
                self.cache.save(
                    documents=documents,
                    chunks=chunks,
                    embeddings=embeddings,
                    manifest=manifest,
                )
                logger.info(
                    "Saved knowledge cache in %.3f seconds", time.perf_counter() - cache_started
                )
                if not getattr(self.cache, "defer_history_activation", False):
                    self.history.activate(documents)
                serving_matrix = np.load(
                    self.cache.embeddings_path, mmap_mode="r", allow_pickle=False
                )
                self.store.replace_index(documents, chunks, serving_matrix)
                self._report("complete", documents=len(documents), chunks=len(chunks))
                self._stats = IndexStats(
                    document_count=len(documents),
                    chunk_count=len(chunks),
                    embedding_dimension=self.provider.dimension,
                    embedding_provider=self.provider.provider_name,
                    embedding_model=self.provider.model_name,
                    loaded_from_cache=False,
                    indexed_at=indexed_at,
                    knowledge_hash=knowledge_hash,
                    cache_schema_version=CACHE_SCHEMA_VERSION,
                )
                logger.info(
                    "Index built: documents=%d chunks=%d total_seconds=%.3f",
                    len(documents),
                    len(chunks),
                    time.perf_counter() - started,
                )
                return self._stats
            finally:
                if isinstance(embeddings, np.memmap):
                    embeddings.flush()
                    mapping = getattr(embeddings, "_mmap", None)
                    if mapping is not None:
                        mapping.close()

    def _archive_cached_snapshot(self, cached: CachedIndex) -> None:
        if self.history.contains_documents(
            [item.document_id for item in cached.documents]
        ) and self.history.contains_chunk_embeddings(
            [item.chunk_id for item in cached.chunks], cached.manifest.embedding_cache_identity
        ):
            return
        for position, document in enumerate(cached.documents, 1):
            self.history.archive_document(document)
            if position % 100 == 0:
                self._report("migration", total=len(cached.documents), completed=position)
        self.history.archive_chunks(cached.chunks)
        for offset in range(0, len(cached.chunks), 32):
            self.history.save_embeddings(
                cached.manifest.embedding_cache_identity,
                cached.chunks[offset : offset + 32],
                cached.embeddings[offset : offset + 32],
            )
            self._report("migration", chunks=min(offset + 32, len(cached.chunks)))

    @staticmethod
    def _revision_order(document: Document) -> tuple[datetime, int, str]:
        version = document.source_version or ""
        date = utc_datetime(document.revision_updated_at or document.source_updated_at)
        return (
            date or datetime.min.replace(tzinfo=UTC),
            int(version) if version.isdecimal() else 0,
            document.document_id,
        )

    @staticmethod
    def _stats_from_cache(manifest: CacheManifest) -> IndexStats:
        return IndexStats(
            document_count=manifest.document_count,
            chunk_count=manifest.chunk_count,
            embedding_dimension=manifest.embedding_dimension,
            embedding_provider=manifest.embedding_provider,
            embedding_model=manifest.embedding_model,
            loaded_from_cache=True,
            indexed_at=manifest.created_at,
            knowledge_hash=manifest.knowledge_hash,
            cache_schema_version=manifest.cache_schema_version,
        )


def create_service(settings: Settings | None = None) -> KnowledgeService:
    """Construct the default service without loading a model or an index."""
    return KnowledgeService((settings or Settings()).resolved())


def create_ssot_service(
    settings: Settings | None = None,
    *,
    provider: EmbeddingProvider | None = None,
) -> KnowledgeService:
    """Construct the separate global index containing current SSOTs of every service."""
    resolved = (settings or Settings()).resolved()
    return KnowledgeService(
        resolved.model_copy(
            update={
                "knowledge_dir": resolved.ssot_knowledge_dir,
                "cache_dir": resolved.ssot_cache_dir,
            }
        ),
        provider=provider,
    )


def configure_logging(level: str) -> None:
    """Configure application logs on stderr (the logging default stream)."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def absolute_paths(service: KnowledgeService) -> tuple[Path, Path]:
    return service.settings.knowledge_dir, service.settings.cache_dir
