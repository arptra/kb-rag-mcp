"""Durable, immutable document revisions and resumable embedding checkpoints.

The SQLite archive is deliberately outside the disposable serving cache. Historical
search streams bounded batches from disk instead of loading every old revision in RAM.
"""

from __future__ import annotations

import hashlib
import heapq
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Any, overload

import numpy as np
from numpy.typing import NDArray

from corporate_kb.models import Chunk, Document, SearchFilters, SearchResult, VersionMetadata

VERSION_FIELDS = tuple(VersionMetadata.model_fields)
_REVISION_TIME = "COALESCE(revision_updated_at,source_updated_at,fetched_at)"


def version_fields(item: VersionMetadata) -> dict[str, Any]:
    return {name: getattr(item, name) for name in VERSION_FIELDS}


@overload
def utc_datetime(value: datetime | str) -> datetime: ...


@overload
def utc_datetime(value: Any) -> datetime | None: ...


def utc_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    result = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def normalize_document(document: Document) -> Document:
    """Keep legacy exact IDs while assigning a stable logical page identity."""
    meta = document.metadata
    page_id = document.page_id or str(meta.get("page_id") or document.source_id)
    # Older Confluence front matter may contain the bare numeric remote page ID.
    if str(document.source_id).startswith("confluence:"):
        page_id = document.source_id
    source_version = document.source_version or meta.get("source_version", meta.get("page_version"))
    updated = utc_datetime(document.source_updated_at or meta.get("source_updated_at"))
    return document.model_copy(
        update={
            "page_id": page_id,
            "revision_id": document.revision_id
            or str(meta.get("revision_id") or document.document_id),
            "source_version": str(source_version) if source_version is not None else None,
            "source_updated_at": updated,
            "revision_updated_at": utc_datetime(
                document.revision_updated_at or meta.get("snapshot_updated_at")
            )
            or updated,
            "fetched_at": utc_datetime(document.fetched_at or meta.get("fetched_at"))
            or document.loaded_at,
            "history_baseline": document.history_baseline or updated is None,
            "is_latest": True,
        }
    )


def embedding_key(identity: str, text: str) -> str:
    return hashlib.sha256((identity + "\0" + text).encode("utf-8")).hexdigest()


class HistoryStore:
    def __init__(self, root: Path, index_key: str) -> None:
        # Hash the key rather than trusting a caller-provided filesystem component.
        self.root = root.resolve() / hashlib.sha256(index_key.encode()).hexdigest()[:32]
        self.path = self.root / "history.sqlite3"
        self._schema_ready = False
        self._schema_lock = Lock()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=60)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            self._initialize_schema(connection)
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize_schema(self, connection: sqlite3.Connection) -> None:
        # Every normal lookup uses its own connection, but schema checks and DDL
        # only belong to the first connection opened by this store. The write
        # transaction also serializes migrations across different processes.
        with self._schema_lock:
            if self._schema_ready:
                return
            # SQLite's busy timeout is not always honored when concurrent first
            # connections switch a brand-new database from DELETE to WAL mode.
            deadline = monotonic() + 60
            while True:
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if (
                        exc.sqlite_errorcode not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                        or monotonic() >= deadline
                    ):
                        raise
                    sleep(0.05)
            connection.execute("BEGIN IMMEDIATE")
            schema = """
                CREATE TABLE IF NOT EXISTS revisions (
                    document_id TEXT PRIMARY KEY, page_id TEXT NOT NULL,
                    source_version TEXT, source_updated_at TEXT, fetched_at TEXT NOT NULL,
                    payload TEXT NOT NULL, is_latest INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS revisions_page ON revisions(page_id, source_updated_at);
                CREATE TABLE IF NOT EXISTS page_visibility (
                    page_id TEXT PRIMARY KEY, visible INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES revisions(document_id),
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_document ON chunks(document_id);
                CREATE TABLE IF NOT EXISTS embeddings (
                    key TEXT PRIMARY KEY, identity TEXT NOT NULL, dimension INTEGER NOT NULL,
                    vector BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id), identity TEXT NOT NULL,
                    embedding_key TEXT NOT NULL REFERENCES embeddings(key),
                    PRIMARY KEY(chunk_id, identity)
                );
            """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(revisions)")}
            if "revision_updated_at" not in columns:
                connection.execute("ALTER TABLE revisions ADD COLUMN revision_updated_at TEXT")
            connection.commit()
            self._schema_ready = True

    def archive_document(self, document: Document, *, make_current: bool = False) -> Document:
        with self._db() as db:
            normalized = self._archive_document(db, document)
            if make_current:
                db.execute(
                    "UPDATE revisions SET is_latest=0 WHERE page_id=?", (normalized.page_id,)
                )
                db.execute(
                    "UPDATE revisions SET is_latest=1 WHERE document_id=?",
                    (normalized.document_id,),
                )
        return normalized

    @staticmethod
    def _archive_document(db: sqlite3.Connection, document: Document) -> Document:
        normalized = normalize_document(document)
        existing = db.execute(
            "SELECT payload FROM revisions WHERE document_id=?", (normalized.document_id,)
        ).fetchone()
        if existing:
            saved = Document.model_validate_json(existing[0])
            if saved.content_hash != normalized.content_hash or saved.page_id != normalized.page_id:
                raise ValueError("A document revision ID cannot be reused for different content")
            return saved
        db.execute(
            "INSERT INTO revisions "
            "(document_id,page_id,source_version,source_updated_at,fetched_at,"
            "payload,is_latest,revision_updated_at) VALUES (?,?,?,?,?,?,0,?)",
            (
                normalized.document_id,
                normalized.page_id,
                normalized.source_version,
                normalized.source_updated_at.isoformat() if normalized.source_updated_at else None,
                (normalized.fetched_at or normalized.loaded_at).isoformat(),
                normalized.model_dump_json(),
                normalized.revision_updated_at.isoformat()
                if normalized.revision_updated_at
                else None,
            ),
        )
        return normalized

    def import_snapshot(
        self,
        documents: Sequence[Document],
        chunks: Sequence[Chunk],
        matrix: NDArray[np.float32],
        identity: str,
        *,
        batch_size: int = 512,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> None:
        """Archive a serving cache using bounded, resumable transactions.

        The supplied matrix may be a read-only memory map. No model is invoked
        and no full matrix copy is made. Each chunk and its existing vector are
        committed together; a restart fills missing batches without replacing
        immutable archive records or changing the active revision pointers.
        """
        if batch_size < 1:
            raise ValueError("Import batch_size must be positive")
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks):
            raise ValueError("Invalid embedding checkpoint")
        # Stay below SQLite's parameter limit even on older installations.
        batch_size = min(batch_size, 500)
        with self._db() as db:
            if progress:
                progress("documents", 0, len(documents))
            for offset in range(0, len(documents), batch_size):
                db.execute("BEGIN IMMEDIATE")
                for document in documents[offset : offset + batch_size]:
                    self._archive_document(db, document)
                db.commit()
                if progress:
                    progress("documents", min(offset + batch_size, len(documents)), len(documents))
            if progress:
                progress("chunks", 0, len(chunks))
            for offset in range(0, len(chunks), batch_size):
                batch = chunks[offset : offset + batch_size]
                vectors = matrix[offset : offset + len(batch)]
                if not np.isfinite(vectors).all():
                    raise ValueError("Invalid embedding checkpoint")
                db.execute("BEGIN IMMEDIATE")
                self._import_chunk_batch(db, batch, vectors, identity)
                db.commit()
                if progress:
                    progress("chunks", offset + len(batch), len(chunks))

    @staticmethod
    def _import_chunk_batch(
        db: sqlite3.Connection,
        chunks: Sequence[Chunk],
        matrix: NDArray[np.float32],
        identity: str,
    ) -> None:
        placeholders = ",".join("?" for _ in chunks)
        chunk_ids = [chunk.chunk_id for chunk in chunks]
        existing_chunks = {
            row[0]: Chunk.model_validate_json(row[1])
            for row in db.execute(
                f"SELECT chunk_id,payload FROM chunks WHERE chunk_id IN ({placeholders})",
                chunk_ids,
            )
        }
        existing_links = dict(
            db.execute(
                "SELECT chunk_id,embedding_key FROM chunk_embeddings WHERE identity=? "
                f"AND chunk_id IN ({placeholders})",
                [identity, *chunk_ids],
            )
        )
        keys = [embedding_key(identity, chunk.embedding_text) for chunk in chunks]
        existing_dimensions = dict(
            db.execute(
                f"SELECT key,dimension FROM embeddings WHERE key IN ({placeholders})", keys
            )
        )
        dimension = matrix.shape[1]
        for chunk, key, vector in zip(chunks, keys, matrix, strict=True):
            saved = existing_chunks.get(chunk.chunk_id)
            if saved is not None:
                if (
                    saved.document_id != chunk.document_id
                    or saved.chunk_index != chunk.chunk_index
                    or saved.text != chunk.text
                    or saved.embedding_text != chunk.embedding_text
                ):
                    raise ValueError("A chunk ID cannot be reused for different content")
            else:
                db.execute(
                    "INSERT INTO chunks VALUES (?,?,?)",
                    (chunk.chunk_id, chunk.document_id, chunk.model_dump_json()),
                )
                existing_chunks[chunk.chunk_id] = chunk
            if key in existing_dimensions and existing_dimensions[key] != dimension:
                raise ValueError(
                    "An embedding identity cannot be reused with a different dimension"
                )
            previous_key = existing_links.get(chunk.chunk_id)
            if previous_key is not None:
                if previous_key != key:
                    raise ValueError("A chunk embedding cannot be replaced with different content")
                continue
            if key not in existing_dimensions:
                db.execute(
                    "INSERT INTO embeddings VALUES (?,?,?,?)",
                    (key, identity, dimension, np.asarray(vector, dtype=np.float32).tobytes()),
                )
                existing_dimensions[key] = dimension
            db.execute(
                "INSERT INTO chunk_embeddings VALUES (?,?,?)", (chunk.chunk_id, identity, key)
            )
            existing_links[chunk.chunk_id] = key

    def activate(self, documents: list[Document]) -> None:
        with self._db() as db:
            db.execute("UPDATE revisions SET is_latest=0")
            db.executemany(
                "UPDATE revisions SET is_latest=1 WHERE document_id=?",
                ((d.document_id,) for d in documents),
            )

    def contains_documents(self, document_ids: list[str]) -> bool:
        with self._db() as db:
            for offset in range(0, len(document_ids), 500):
                batch = document_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                count = db.execute(
                    f"SELECT COUNT(*) FROM revisions WHERE document_id IN ({placeholders})", batch
                ).fetchone()[0]
                if count != len(set(batch)):
                    return False
        return True

    def set_page_visibility(self, page_id: str, visible: bool) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO page_visibility VALUES (?,?)", (page_id, int(visible))
            )

    def contains_chunk_embeddings(self, chunk_ids: list[str], identity: str) -> bool:
        with self._db() as db:
            for offset in range(0, len(chunk_ids), 500):
                batch = chunk_ids[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                count = db.execute(
                    "SELECT COUNT(*) FROM chunk_embeddings WHERE identity=? "
                    f"AND chunk_id IN ({placeholders})",
                    [identity, *batch],
                ).fetchone()[0]
                if count != len(set(batch)):
                    return False
        return True

    def hidden_page_ids(self) -> list[str]:
        with self._db() as db:
            return [
                row[0] for row in db.execute("SELECT page_id FROM page_visibility WHERE visible=0")
            ]

    def is_visible(self, page_id: str | None) -> bool:
        with self._db() as db:
            row = db.execute(
                "SELECT visible FROM page_visibility WHERE page_id=?", (page_id,)
            ).fetchone()
        return row is None or bool(row[0])

    def archive_chunks(self, chunks: list[Chunk]) -> None:
        with self._db() as db:
            db.executemany(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?)",
                ((chunk.chunk_id, chunk.document_id, chunk.model_dump_json()) for chunk in chunks),
            )

    @staticmethod
    def _document(row: tuple[Any, ...]) -> Document:
        return Document.model_validate_json(row[0]).model_copy(update={"is_latest": bool(row[1])})

    def get_document(self, document_id: str) -> Document | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload,is_latest FROM revisions WHERE document_id=?", (document_id,)
            ).fetchone()
        document = self._document(row) if row else None
        return document if document is None or self.is_visible(document.page_id) else None

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        with self._db() as db:
            row = db.execute(
                "SELECT c.payload,r.is_latest FROM chunks c "
                "JOIN revisions r USING(document_id) WHERE c.chunk_id=?",
                (chunk_id,),
            ).fetchone()
        chunk = (
            Chunk.model_validate_json(row[0]).model_copy(update={"is_latest": bool(row[1])})
            if row
            else None
        )
        return chunk if chunk is None or self.is_visible(chunk.page_id) else None

    def list_versions(self, page_id: str, *, limit: int = 100, offset: int = 0) -> list[Document]:
        if not self.is_visible(page_id):
            return []
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("Invalid version pagination")
        with self._db() as db:
            rows = db.execute(
                "SELECT payload,is_latest FROM revisions WHERE page_id=? "
                f"ORDER BY is_latest DESC,{_REVISION_TIME} DESC,"
                "fetched_at DESC,document_id DESC LIMIT ? OFFSET ?",
                (page_id, limit, offset),
            ).fetchall()
        return [self._document(row) for row in rows]

    def read(
        self,
        page_id: str,
        *,
        source_version: str | None = None,
        as_of: str | datetime | None = None,
    ) -> Document | None:
        if not self.is_visible(page_id):
            return None
        if source_version is not None and as_of is not None:
            raise ValueError("source_version and as_of are mutually exclusive")
        clauses = ["page_id=?"]
        values: list[Any] = [page_id]
        if source_version is not None:
            clauses.append("source_version=?")
            values.append(str(source_version))
        elif as_of is not None:
            clauses.append(f"{_REVISION_TIME}<=?")
            values.append(utc_datetime(as_of).isoformat())
        else:
            clauses.append("is_latest=1")
        with self._db() as db:
            row = db.execute(
                "SELECT payload,is_latest FROM revisions WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY {_REVISION_TIME} DESC,fetched_at DESC,document_id DESC LIMIT 1",
                values,
            ).fetchone()
        return self._document(row) if row else None

    def coverage(
        self, page_id: str | None = None, *, identity: str | None = None
    ) -> dict[str, Any]:
        clauses = [
            "NOT EXISTS (SELECT 1 FROM page_visibility v WHERE v.page_id=r.page_id AND v.visible=0)"
        ]
        values = []
        if page_id:
            clauses.append("r.page_id=?")
            values.append(page_id)
        where = " WHERE " + " AND ".join(clauses)
        with self._db() as db:
            row = db.execute(
                f"SELECT COUNT(*),MIN({_REVISION_TIME}),SUM(source_updated_at IS NULL) "
                f"FROM revisions r{where}",
                values,
            ).fetchone()
            pending = (
                db.execute(
                    "SELECT COUNT(*) FROM chunks c JOIN revisions r USING(document_id)"
                    + where
                    + " AND NOT EXISTS (SELECT 1 FROM chunk_embeddings ce "
                    "WHERE ce.chunk_id=c.chunk_id AND ce.identity=?)",
                    [*values, identity or ""],
                ).fetchone()[0]
                if identity
                else None
            )
        return {
            "known_since": row[1],
            "revision_count": row[0],
            "has_baseline": bool(row[2]),
            "complete": False,
            "pending_chunks": pending,
            "note": (
                "Only captured revisions are available; "
                "gaps before or between synchronizations may exist."
            ),
        }

    def pending_chunks(self, identity: str, *, batch_size: int = 32) -> Iterator[list[Chunk]]:
        # Keyset pagination avoids retaining a read transaction while writing checkpoints.
        after = ""
        while True:
            with self._db() as db:
                rows = db.execute(
                    "SELECT c.chunk_id,c.payload FROM chunks c WHERE c.chunk_id>? "
                    "AND NOT EXISTS (SELECT 1 FROM chunk_embeddings ce "
                    "WHERE ce.chunk_id=c.chunk_id AND ce.identity=?) ORDER BY c.chunk_id LIMIT ?",
                    (after, identity, batch_size),
                ).fetchall()
            if not rows:
                return
            after = rows[-1][0]
            yield [Chunk.model_validate_json(row[1]) for row in rows]

    def has_pending_chunks(self, identity: str) -> bool:
        with self._db() as db:
            return (
                db.execute(
                    "SELECT 1 FROM chunks c WHERE NOT EXISTS "
                    "(SELECT 1 FROM chunk_embeddings ce WHERE ce.chunk_id=c.chunk_id "
                    "AND ce.identity=?) LIMIT 1",
                    (identity,),
                ).fetchone()
                is not None
            )

    def latest_references(self, page_ids: list[str]) -> dict[str, dict[str, str | None]]:
        """Read small current-version pointers without loading document bodies."""
        if not page_ids:
            return {}
        page_ids = list(dict.fromkeys(page_ids))
        placeholders = ",".join("?" for _ in page_ids)
        with self._db() as db:
            rows = db.execute(
                "SELECT r.page_id,r.document_id,r.source_version,r.source_updated_at,"
                "r.revision_updated_at FROM revisions r WHERE r.is_latest=1 "
                f"AND r.page_id IN ({placeholders}) AND NOT EXISTS "
                "(SELECT 1 FROM page_visibility v WHERE v.page_id=r.page_id AND v.visible=0)",
                page_ids,
            ).fetchall()
        return {
            row[0]: {
                "document_id": row[1],
                "source_version": row[2],
                "source_updated_at": row[3],
                "revision_updated_at": row[4],
            }
            for row in rows
        }

    def cached_embeddings(
        self, identity: str, texts: list[str], dimension: int
    ) -> dict[str, NDArray[np.float32]]:
        found: dict[str, NDArray[np.float32]] = {}
        with self._db() as db:
            for text in texts:
                key = embedding_key(identity, text)
                row = db.execute(
                    "SELECT dimension,vector FROM embeddings WHERE key=?", (key,)
                ).fetchone()
                if row and row[0] == dimension:
                    vector = np.frombuffer(row[1], dtype=np.float32)
                    if vector.shape == (dimension,) and np.isfinite(vector).all():
                        found[key] = vector
        return found

    def save_embeddings(
        self, identity: str, chunks: list[Chunk], matrix: NDArray[np.float32]
    ) -> None:
        if matrix.ndim != 2 or matrix.shape[0] != len(chunks) or not np.isfinite(matrix).all():
            raise ValueError("Invalid embedding checkpoint")
        with self._db() as db:
            for chunk, vector in zip(chunks, matrix, strict=True):
                key = embedding_key(identity, chunk.embedding_text)
                db.execute(
                    "INSERT OR REPLACE INTO embeddings VALUES (?,?,?,?)",
                    (key, identity, len(vector), np.asarray(vector, dtype=np.float32).tobytes()),
                )
                db.execute(
                    "INSERT OR REPLACE INTO chunk_embeddings VALUES (?,?,?)",
                    (chunk.chunk_id, identity, key),
                )

    def search(
        self,
        query: NDArray[np.float32],
        *,
        identity: str,
        filters: SearchFilters,
        top_k: int,
        min_score: float | None = None,
    ) -> list[SearchResult]:
        from corporate_kb.stores.memory import InMemoryKnowledgeStore

        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        query = np.asarray(query, dtype=np.float32).reshape(-1)
        clauses, args = (
            [
                "ce.identity=?",
                "e.dimension=?",
                "NOT EXISTS (SELECT 1 FROM page_visibility v "
                "WHERE v.page_id=r.page_id AND v.visible=0)",
            ],
            [identity, len(query)],
        )
        if filters.page_id:
            clauses.append("r.page_id=?")
            args.append(filters.page_id)
        if filters.version_scope == "latest":
            clauses.append("r.is_latest=1")
        if filters.version_scope == "as_of":
            if filters.as_of is None:
                raise ValueError("as_of is required for version_scope=as_of")
            stamp = utc_datetime(filters.as_of).isoformat()
            clauses.append(
                "r.document_id=(SELECT rr.document_id FROM revisions rr "
                "WHERE rr.page_id=r.page_id "
                "AND COALESCE(rr.revision_updated_at,rr.source_updated_at,rr.fetched_at)<=? "
                "ORDER BY COALESCE(rr.revision_updated_at,rr.source_updated_at,rr.fetched_at) "
                "DESC,rr.fetched_at DESC,rr.document_id DESC LIMIT 1)"
            )
            args.append(stamp)
        for bound, operator in ((filters.updated_from, ">="), (filters.updated_to, "<=")):
            if bound is not None:
                clauses.append(f"r.source_updated_at{operator}?")
                args.append(utc_datetime(bound).isoformat())
        best: list[tuple[float, str, Chunk]] = []
        with self._db() as db:
            cursor = db.execute(
                "SELECT c.payload,r.is_latest,e.vector FROM chunks c "
                "JOIN revisions r USING(document_id) JOIN chunk_embeddings ce USING(chunk_id) "
                "JOIN embeddings e ON e.key=ce.embedding_key WHERE " + " AND ".join(clauses),
                args,
            )
            while rows := cursor.fetchmany(128):
                for payload, is_latest, raw in rows:
                    chunk = Chunk.model_validate_json(payload).model_copy(
                        update={"is_latest": bool(is_latest)}
                    )
                    if not InMemoryKnowledgeStore._matches(chunk, filters):
                        continue
                    vector = np.frombuffer(raw, dtype=np.float32)
                    if vector.shape != query.shape:
                        continue
                    score = float(vector @ query)
                    if not np.isfinite(score) or (min_score is not None and score < min_score):
                        continue
                    item = (score, chunk.chunk_id, chunk)
                    if len(best) < top_k:
                        heapq.heappush(best, item)
                    elif item[:2] > best[0][:2]:
                        heapq.heapreplace(best, item)
        return [
            SearchResult(
                rank=rank,
                score=score,
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                title=chunk.title,
                heading_path=chunk.heading_path,
                text=chunk.text,
                source_path=chunk.source_path,
                source_url=chunk.source_url,
                metadata=chunk.metadata,
                **version_fields(chunk),
            )
            for rank, (score, _, chunk) in enumerate(sorted(best, key=lambda x: (-x[0], x[1])), 1)
        ]
