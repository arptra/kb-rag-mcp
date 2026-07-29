"""Index lifecycle and query orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from corporate_kb.cache.manager import CACHE_SCHEMA_VERSION, CacheManager, CacheManifest
from corporate_kb.chunking.structural_chunker import SimpleTokenCounter, StructuralChunker
from corporate_kb.config import Settings
from corporate_kb.embeddings.base import EmbeddingProvider
from corporate_kb.embeddings.hash_provider import HashEmbeddingProvider
from corporate_kb.embeddings.sentence_transformer import SentenceTransformerEmbeddingProvider
from corporate_kb.loaders.filesystem import FileSystemDocumentLoader
from corporate_kb.models import Document, IndexStats, SearchFilters, SearchResult
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
        self._stats: IndexStats | None = None
        self._lock = threading.RLock()

    def build_index(self, force: bool = False) -> IndexStats:
        """Build or reuse an index, replacing the active store only after full validation."""
        with self._lock:
            started = time.perf_counter()
            documents = self.loader.load_directory(self.settings.knowledge_dir)
            knowledge_hash = self._knowledge_hash(documents)
            if not force:
                cached = self.cache.load(
                    knowledge_hash=knowledge_hash,
                    embedding_cache_identity=self.provider.cache_identity,
                    chunking=self.chunker.identity,
                )
                if cached is not None:
                    self.store.replace_index(cached.documents, cached.chunks, cached.embeddings)
                    self._stats = self._stats_from_cache(cached.manifest)
                    return self._stats

            chunks = [chunk for document in documents for chunk in self.chunker.chunk(document)]
            logger.info("Created %d structural chunks", len(chunks))
            if chunks:
                embeddings = self.provider.embed_documents(
                    [chunk.embedding_text for chunk in chunks]
                )
            else:
                embeddings = np.empty((0, self.provider.dimension), dtype=np.float32)
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
            self.cache.save(
                documents=documents,
                chunks=chunks,
                embeddings=embeddings,
                manifest=manifest,
            )
            self.store.replace_index(documents, chunks, embeddings)
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
            logger.info("Index built in %.3f seconds", time.perf_counter() - started)
            return self._stats

    def load_or_build_index(self) -> IndexStats:
        """Load a compatible cache; build only when explicitly allowed."""
        with self._lock:
            if self._stats is not None:
                return self._stats
            documents = self.loader.load_directory(self.settings.knowledge_dir)
            knowledge_hash = self._knowledge_hash(documents)
            cached = self.cache.load(
                knowledge_hash=knowledge_hash,
                embedding_cache_identity=self.provider.cache_identity,
                chunking=self.chunker.identity,
            )
            if cached is not None:
                self.store.replace_index(cached.documents, cached.chunks, cached.embeddings)
                self._stats = self._stats_from_cache(cached.manifest)
                return self._stats
            if not self.settings.auto_index:
                raise KnowledgeIndexMissingError(
                    "Knowledge index is missing or incompatible.\nRun: ./scripts/dev.sh index"
                )
            return self.build_index(force=True)

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        min_score: float | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        self.load_or_build_index()
        if not query.strip():
            raise ValueError("query must not be empty")
        started = time.perf_counter()
        query_vector = self.provider.embed_query(query)
        active_filters = filters or SearchFilters()
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

    def get_document(self, document_id: str) -> Document:
        self.load_or_build_index()
        document = self.store.get_document(document_id)
        if document is None:
            raise KeyError(f"Unknown document_id: {document_id}")
        return document

    def list_documents(
        self,
        *,
        filters: SearchFilters | None = None,
        limit: int = 50,
    ) -> list[Document]:
        self.load_or_build_index()
        return self.store.list_documents(filters or SearchFilters(), limit=limit)

    def stats(self) -> IndexStats:
        return self.load_or_build_index()

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


def configure_logging(level: str) -> None:
    """Configure application logs on stderr (the logging default stream)."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def absolute_paths(service: KnowledgeService) -> tuple[Path, Path]:
    return service.settings.knowledge_dir, service.settings.cache_dir
