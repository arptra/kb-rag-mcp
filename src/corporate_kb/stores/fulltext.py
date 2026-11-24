"""A reusable BM25 index over the chunk bodies in one serving snapshot."""

from __future__ import annotations

import re
import sqlite3
from itertools import islice
from threading import Lock
from weakref import finalize

import numpy as np
from numpy.typing import NDArray

from corporate_kb.models import Chunk


class ChunkTextIndex:
    def __init__(self) -> None:
        self._db: sqlite3.Connection | None = None
        self._lock = Lock()
        self._unavailable = False

    def search(
        self, text: str, chunks: list[Chunk], eligible: NDArray[np.bool_], limit: int,
    ) -> list[int]:
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", text.casefold())))[:64]
        if not terms:
            return []
        clauses = [f'"{term}"' for term in terms]
        clauses.extend(f'"{term[:-2]}"*' for term in terms if len(term) >= 7)
        with self._lock:
            if self._unavailable:
                return []
            if self._db is None:
                db = sqlite3.connect(":memory:", check_same_thread=False)
                try:
                    db.execute("CREATE VIRTUAL TABLE body USING fts5(text, content='')")
                    with db:
                        db.executemany("INSERT INTO body(text) VALUES (?)",
                                       ((chunk.text,) for chunk in chunks))
                except sqlite3.Error as exc:
                    db.close()
                    if "no such module: fts5" in str(exc):
                        self._unavailable = True
                        return []
                    raise
                self._db = db
                finalize(self, db.close)
            rows = self._db.execute("SELECT rowid FROM body WHERE body MATCH ? ORDER BY rank",
                                    (" OR ".join(clauses),))
            try:
                # A popular term can have many hidden, stale or unrelated-scope hits.
                # Apply the same mask as vector search before taking the top K.
                return list(islice((i - 1 for (i,) in rows if eligible[i - 1]), limit))
            finally:
                rows.close()


def fuse_ranks(vector: list[int], lexical: list[int]) -> list[int]:
    """Reciprocal rank fusion, retaining a lead from each independent retriever."""
    if not lexical:
        return vector
    scores: dict[int, float] = {}
    for order in (lexical, vector):
        for rank, index in enumerate(order, 1):
            scores[index] = scores.get(index, 0.0) + 1 / (60 + rank)
    ranked = sorted(scores, key=scores.__getitem__, reverse=True)
    # Agreement on generic terms should not crowd a unique body match out of a
    # small review budget. Keep each retriever's strongest lead near the front.
    return list(dict.fromkeys([ranked[0], lexical[0], vector[0], *ranked]))
