"""Select bounded, verbatim passages from a current source document."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing

_WORDS = re.compile(r"[^\W_]{2,}", re.UNICODE)
_SEPARATOR = "\n…\n"


def _bm25(passages: Iterable[str], query: str) -> list[tuple[int, float]]:
    terms = list(dict.fromkeys(_WORDS.findall(query.casefold())))[:64]
    if not terms:
        return []
    # Store row numbers and postings only. The authoritative text stays in memory
    # once and is used for all returned slices, including when accents differ.
    with closing(sqlite3.connect(":memory:")) as db:
        try:
            db.execute("CREATE VIRTUAL TABLE passages USING fts5(body, content='')")
        except sqlite3.OperationalError as exc:
            if "no such module" not in str(exc):
                raise
            # Portable Python builds can omit the optional SQLite extension.
            terms_set = set(terms)
            return [(i, -float(hits)) for i, passage in enumerate(passages)
                    if (hits := len(terms_set.intersection(_WORDS.findall(passage.casefold()))))]
        db.executemany(
            "INSERT INTO passages(body) VALUES (?)",
            ((passage,) for passage in passages),
        )
        # Short prefix alternatives cover common inflections without a language
        # dictionary. Exact terms still contribute their own BM25 match.
        clauses = [f'"{term}"' for term in terms]
        clauses.extend(f'"{term[:-2]}"*' for term in terms if len(term) >= 7)
        rows = db.execute("SELECT rowid, rank FROM passages WHERE passages MATCH ?",
                          (" OR ".join(clauses),))
        return [(rowid - 1, score) for rowid, score in rows]


def _rank_offsets(text: str, query: str, width: int) -> list[tuple[int, float]]:
    offsets = list(range(0, len(text) - width, max(1, width // 2)))
    offsets.append(len(text) - width)
    ranked = _bm25((text[start:start + width] for start in offsets), query)
    return [(offsets[i] + width // 2, score) for i, score in ranked]


def _anchor_positions(text: str, anchors: Sequence[str]) -> list[int]:
    positions = []
    for anchor in anchors[:3]:
        # A search excerpt can have an ellipsis inserted by a context budget.
        # Use only material which actually occurs in the currently read page.
        fragments = re.split(r"…|\n\s*\n", anchor[:4000])
        for fragment in sorted(fragments, key=len, reverse=True):
            fragment = fragment.strip()
            if len(fragment) < 12:
                continue
            start = text.find(fragment)
            if start >= 0 and text.find(fragment, start + 1) == -1:
                positions.append(start + len(fragment) // 2)
                break
    return positions


def _match_center(text: str, point: int, width: int, query: str) -> int:
    """Carry surrounding facts when the matched words lie on a block boundary."""
    terms = {word for word in _WORDS.findall(query.casefold()) if len(word) >= 3}
    start = point - width // 2
    matches: dict[str, list[int]] = {}
    for word in _WORDS.finditer(text[start:start + width]):
        term = word[0].casefold()
        if term in terms:
            matches.setdefault(term, []).append(start + (word.start() + word.end()) // 2)
    if not matches:
        return point
    # Repeated boilerplate receives the same vote as a single matching term.
    return round(sum(sum(values) / len(values) for values in matches.values()) / len(matches))


def source_passage(text: str, question: str, limit: int, *, anchors: Sequence[str] = ()) -> str:
    """Use BM25 and fresh search hits to retain evidence within a character budget."""
    if len(text) <= limit:
        return text
    if limit < 4:
        return text[:max(0, limit)]

    # For a large budget keep independent matching sections, e.g. different rows
    # recording changes to the same entity. For a tiny budget keep one passage.
    count = min(3, max(1, limit // 480))
    width = min((limit - 2 - (count - 1) * len(_SEPARATOR)) // count, 1800)
    points: list[int] = []
    for point in _anchor_positions(text, anchors):
        if len(points) < count and all(abs(point - other) >= width for other in points):
            points.append(point)
    ranked = _rank_offsets(text, question, width) if len(points) < count else []
    while len(points) < count:
        choices = [(score, -min((abs(point - other) for other in points), default=0), point)
                   for point, score in ranked
                   if all(abs(point - other) >= width for other in points)]
        if not choices:
            break
        center = min(choices)[2]
        point = _match_center(text, center, width, question)
        # A shifted focus from this block can also be present in its neighbour.
        ranked = [(other, score) for other, score in ranked if abs(other - center) >= width]
        if all(abs(point - other) >= width for other in points):
            points.append(point)
    if not points:
        return text[:limit - 1] + "…"

    allowance = (limit - 2 - (len(points) - 1) * len(_SEPARATOR)) // len(points)
    spans: list[tuple[int, int]] = []
    for point in sorted(points):
        start = max(0, min(point - allowance // 2, len(text) - allowance))
        end = start + allowance
        if spans and spans[-1][1] >= start:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    excerpt = _SEPARATOR.join(text[start:end] for start, end in spans)
    return ("…" if spans[0][0] else "") + excerpt + ("…" if spans[-1][1] < len(text) else "")
