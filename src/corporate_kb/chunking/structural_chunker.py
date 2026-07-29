"""Heading-aware, semantic-block chunking."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Protocol

from corporate_kb.chunking.blocks import Block, parse_markdown_blocks
from corporate_kb.models import Chunk, Document

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+(?=[A-Z\u0410-\u042f\u04010-9])")


class TokenCounter(Protocol):
    """Tokenizer-independent counter used by the chunker."""

    def count(self, text: str) -> int: ...


class SimpleTokenCounter:
    """Deterministic approximation suitable for tests and pre-model indexing."""

    def count(self, text: str) -> int:
        return len(_TOKEN.findall(text))


class StructuralChunker:
    """Build chunks from semantic blocks while retaining section context."""

    def __init__(
        self,
        token_counter: TokenCounter,
        *,
        target_tokens: int = 700,
        hard_max_tokens: int = 900,
        overlap_tokens: int = 80,
    ) -> None:
        if not 0 <= overlap_tokens < target_tokens <= hard_max_tokens:
            raise ValueError("Expected 0 <= overlap < target <= hard maximum")
        self._counter = token_counter
        self.target_tokens = target_tokens
        self.hard_max_tokens = hard_max_tokens
        self.overlap_tokens = overlap_tokens

    @property
    def identity(self) -> dict[str, int]:
        return {
            "chunk_size": self.target_tokens,
            "chunk_overlap": self.overlap_tokens,
            "chunk_hard_max": self.hard_max_tokens,
        }

    def chunk(self, document: Document) -> list[Chunk]:
        parsed = parse_markdown_blocks(document.content, document.title)
        expanded = [piece for block in parsed for piece in self._split_oversized(block)]
        groups = self._group_blocks(expanded)
        chunks: list[Chunk] = []
        for chunk_index, (heading_path, blocks) in enumerate(groups):
            text = "\n\n".join(block.text for block in blocks).strip()
            if not text:
                continue
            heading = " > ".join(heading_path)
            chunk_id_input = "\0".join(
                [
                    document.document_id,
                    heading,
                    str(chunk_index),
                    document.content_hash,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ]
            )
            chunks.append(
                Chunk(
                    chunk_id=hashlib.sha256(chunk_id_input.encode("utf-8")).hexdigest(),
                    document_id=document.document_id,
                    chunk_index=chunk_index,
                    title=document.title,
                    heading_path=heading,
                    text=text,
                    embedding_text=self._embedding_text(document, heading, text),
                    token_count=self._counter.count(text),
                    source_path=document.source_path,
                    source_url=document.source_url,
                    metadata=dict(document.metadata),
                )
            )
        return chunks

    def _split_oversized(self, block: Block) -> list[Block]:
        if self._counter.count(block.text) <= self.hard_max_tokens:
            return [block]
        if block.kind in {"code", "table"}:
            return [block]

        sentences = [item.strip() for item in _SENTENCE_BOUNDARY.split(block.text) if item.strip()]
        pieces: list[str] = []
        current: list[str] = []
        for sentence in sentences:
            if self._counter.count(sentence) > self.hard_max_tokens:
                if current:
                    pieces.append(" ".join(current))
                    current = []
                pieces.extend(self._split_by_words(sentence))
                continue
            candidate = " ".join([*current, sentence])
            if current and self._counter.count(candidate) > self.hard_max_tokens:
                pieces.append(" ".join(current))
                current = [sentence]
            else:
                current.append(sentence)
        if current:
            pieces.append(" ".join(current))
        return [
            Block(kind=block.kind, text=text, heading_path=block.heading_path) for text in pieces
        ]

    def _split_by_words(self, text: str) -> list[str]:
        words = text.split()
        if not words:
            return []
        pieces: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and self._counter.count(candidate) > self.hard_max_tokens:
                pieces.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            pieces.append(" ".join(current))
        return pieces

    def _group_blocks(self, blocks: Iterable[Block]) -> list[tuple[tuple[str, ...], list[Block]]]:
        groups: list[tuple[tuple[str, ...], list[Block]]] = []
        current: list[Block] = []
        current_heading: tuple[str, ...] | None = None

        def emit() -> None:
            nonlocal current
            if current and current_heading is not None:
                groups.append((current_heading, current))
            current = []

        for block in blocks:
            if current_heading is not None and block.heading_path != current_heading:
                emit()
            current_heading = block.heading_path
            block_tokens = self._counter.count(block.text)
            if block_tokens > self.hard_max_tokens:
                emit()
                groups.append((block.heading_path, [block]))
                continue
            candidate_tokens = self._count_blocks([*current, block])
            if current and candidate_tokens > self.target_tokens:
                previous = current
                emit()
                current = self._overlap_tail(previous)
                if current and self._count_blocks([*current, block]) > self.hard_max_tokens:
                    current = []
            current.append(block)
        emit()
        return self._merge_tiny_tail(groups)

    def _overlap_tail(self, blocks: list[Block]) -> list[Block]:
        if self.overlap_tokens == 0:
            return []
        tail: list[Block] = []
        for block in reversed(blocks):
            candidate = [block, *tail]
            if tail and self._count_blocks(candidate) > self.overlap_tokens:
                break
            if not tail and self._counter.count(block.text) > self.overlap_tokens:
                break
            tail = candidate
        return tail

    def _merge_tiny_tail(
        self, groups: list[tuple[tuple[str, ...], list[Block]]]
    ) -> list[tuple[tuple[str, ...], list[Block]]]:
        if len(groups) < 2:
            return groups
        last_heading, last_blocks = groups[-1]
        previous_heading, previous_blocks = groups[-2]
        tiny_threshold = max(1, self.target_tokens // 5)
        if (
            last_heading == previous_heading
            and self._count_blocks(last_blocks) < tiny_threshold
            and self._count_blocks([*previous_blocks, *last_blocks]) <= self.hard_max_tokens
        ):
            groups[-2] = (previous_heading, [*previous_blocks, *last_blocks])
            groups.pop()
        return groups

    def _count_blocks(self, blocks: list[Block]) -> int:
        return self._counter.count("\n\n".join(block.text for block in blocks))

    @staticmethod
    def _embedding_text(document: Document, heading_path: str, text: str) -> str:
        labels = [
            ("Document", document.title),
            ("Section", heading_path),
            ("Document type", document.metadata.get("document_type")),
            ("Service", document.metadata.get("service")),
            ("Domain", document.metadata.get("domain")),
            ("Authority", document.metadata.get("authority")),
        ]
        context = "\n".join(
            f"{label}: {value}" for label, value in labels if value not in (None, "")
        )
        return f"{context}\n\n{text}"
