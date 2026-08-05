"""Deterministic, query-aware context packing for MCP tool responses."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCE_OR_BLOCK_BOUNDARY = re.compile(
    r"\n{2,}|(?<=[.!?…])\s+(?=[A-Z\u0410-\u042f\u04010-9])",
)


@dataclass(frozen=True, slots=True)
class ContextExcerpt:
    """A token-budgeted piece of source text."""

    text: str
    token_count: int
    truncated: bool


class ContextCompressor:
    """Extract relevant sentences without invoking another language model."""

    @staticmethod
    def count_tokens(text: str) -> int:
        return len(_TOKEN.findall(text))

    def excerpt(self, *, query: str, text: str, max_tokens: int) -> ContextExcerpt:
        """Return a bounded extractive excerpt, preferring query-overlapping sentences."""
        if max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        normalized = "\n".join(line.rstrip() for line in text.splitlines()).strip()
        token_count = self.count_tokens(normalized)
        if token_count <= max_tokens:
            return ContextExcerpt(normalized, token_count, truncated=False)

        query_terms = self._terms(query)
        segments = [
            segment.strip()
            for segment in _SENTENCE_OR_BLOCK_BOUNDARY.split(normalized)
            if segment.strip()
        ]
        if not segments:
            return self._truncate(normalized, max_tokens)

        ranked = sorted(
            range(len(segments)),
            key=lambda index: (
                -self._overlap_score(segments[index], query_terms),
                index,
            ),
        )
        selected: list[tuple[int, str]] = []
        remaining = max_tokens
        for index in ranked:
            candidate = segments[index]
            candidate_tokens = self.count_tokens(candidate)
            if candidate_tokens <= remaining:
                selected.append((index, candidate))
                remaining -= candidate_tokens
                if remaining == 0:
                    break
            elif not selected:
                excerpt = self._truncate(candidate, remaining)
                selected.append((index, excerpt.text))
                remaining = 0
                break

        if not selected:
            return self._truncate(normalized, max_tokens)
        excerpt_text = "\n\n".join(text for _, text in sorted(selected)).strip()
        excerpt = self._truncate(excerpt_text, max_tokens)
        return ContextExcerpt(excerpt.text, excerpt.token_count, truncated=True)

    def _truncate(self, text: str, max_tokens: int) -> ContextExcerpt:
        matches = list(_TOKEN.finditer(text))
        if len(matches) <= max_tokens:
            return ContextExcerpt(text.strip(), len(matches), truncated=False)
        if max_tokens == 1:
            return ContextExcerpt("…", 1, truncated=True)
        end = matches[max_tokens - 2].end()
        return ContextExcerpt(f"{text[:end].rstrip()} …", max_tokens, truncated=True)

    @staticmethod
    def _overlap_score(segment: str, query_terms: set[str]) -> int:
        if not query_terms:
            return 0
        return len(query_terms & ContextCompressor._terms(segment))

    @staticmethod
    def _terms(text: str) -> set[str]:
        words = {
            term.lower()
            for term in _TOKEN.findall(text)
            if any(character.isalnum() for character in term)
        }
        # A small, language-independent prefix catches common Russian inflections such as
        # "лимиты" / "лимитами" without a heavyweight morphological dependency.
        return words | {word[:5] for word in words if len(word) >= 5}
