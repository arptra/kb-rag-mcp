"""Preserve chunk identities while avoiding repeated tokenization of long prose."""

import re
from datetime import UTC, datetime

import pytest

import corporate_kb.chunking.structural_chunker as chunker_module
from corporate_kb.chunking.blocks import Block
from corporate_kb.chunking.structural_chunker import SimpleTokenCounter, StructuralChunker
from corporate_kb.models import Document

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…])\s+(?=[A-Z\u0410-\u042f\u04010-9])")


class LegacyStructuralChunker(StructuralChunker):
    """Reference implementation from before the additive-count optimization."""

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


def _document(content: str) -> Document:
    return Document(
        document_id="document-id",
        title="Сервис счетов",
        source_path="confluence/accounts.md",
        source_type="markdown",
        source_id="confluence:source:123",
        source_url="https://example.test/pages/123",
        content=content,
        content_hash="content-hash",
        metadata={"authority": "confluence", "service": "accounts", "source_version": "4"},
        loaded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


_CONTENTS = [
    "",
    "# Long paragraph\n\n" + "customer account endpoint schema " * 750,
    "# Sentences\n\n" + "One small sentence. Ещё предложение! 42 значения? " * 100,
    "# Mixed\n\nShort sentence. "
    + "слово\tword_123\u00a0中文\u2003🙂→(x/y) " * 100
    + ". End sentence.\n\n## New section\n\nTail one. Tail two.",
    "# Hard words\n\n" + "::/?!🙂" * 30 + " end. After punctuation. " * 20,
    "# Blocks\n\n"
    + "\n\n".join(["- One item\n- Второй пункт", "Small prose block."] * 20)
    + "\n\n```python\n"
    + "print('whole code block')\n" * 30
    + "```\n\n| Key | Value |\n| --- | --- |\n"
    + "| key | value |\n" * 30
    + "\nTail text.",
]


@pytest.mark.parametrize(
    "content", _CONTENTS, ids=["empty", "words", "sentences", "mixed", "punctuation", "blocks"]
)
@pytest.mark.parametrize("limits", [(5, 9, 0), (18, 22, 3), (700, 900, 80)])
def test_optimized_chunks_are_identical_to_legacy(
    content: str, limits: tuple[int, int, int]
) -> None:
    target, maximum, overlap = limits
    options = {"target_tokens": target, "hard_max_tokens": maximum, "overlap_tokens": overlap}
    document = _document(content)
    expected = LegacyStructuralChunker(SimpleTokenCounter(), **options).chunk(document)
    actual = StructuralChunker(SimpleTokenCounter(), **options).chunk(document)

    # This covers IDs, boundaries, text, embedding context, counts, and metadata.
    assert [chunk.model_dump_json() for chunk in actual] == [
        chunk.model_dump_json() for chunk in expected
    ]


class CharacterCounter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def count(self, text: str) -> int:
        self.calls.append(text)
        return len(text)


class SubclassCounter(SimpleTokenCounter, CharacterCounter):
    def count(self, text: str) -> int:
        return CharacterCounter.count(self, text)


@pytest.mark.parametrize("counter_type", [CharacterCounter, SubclassCounter])
def test_non_additive_custom_counters_keep_original_calls_and_chunks(
    counter_type: type[CharacterCounter],
) -> None:
    content = "# Custom\n\n" + "First sentence. Second sentence! длинноеслово " * 10
    document = _document(content)
    expected_counter, actual_counter = counter_type(), counter_type()
    options = {"target_tokens": 17, "hard_max_tokens": 27, "overlap_tokens": 4}

    expected = LegacyStructuralChunker(expected_counter, **options).chunk(document)
    actual = StructuralChunker(actual_counter, **options).chunk(document)

    assert actual == expected
    assert actual_counter.calls == expected_counter.calls


def test_long_paragraph_has_bounded_tokenization_work(monkeypatch: pytest.MonkeyPatch) -> None:
    pattern = re.compile(r"\w+|[^\w\s]", re.UNICODE)

    class CountingPattern:
        scanned_characters = 0

        def findall(self, text: str) -> list[str]:
            self.scanned_characters += len(text)
            return pattern.findall(text)

    instrumented = CountingPattern()
    monkeypatch.setattr(chunker_module, "_TOKEN", instrumented)
    content = "technical service api documentation account customer response message " * 750
    chunks = StructuralChunker(SimpleTokenCounter()).chunk(_document(content))

    assert len(chunks) > 1
    assert all(chunk.token_count <= 900 for chunk in chunks)
    # A work bound is stable across hosts; the legacy implementation rescans the
    # accumulated chunk for each word and exceeds this by more than an order of magnitude.
    assert instrumented.scanned_characters < len(content) * 20
