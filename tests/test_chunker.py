from datetime import UTC, datetime

from corporate_kb.chunking.structural_chunker import SimpleTokenCounter, StructuralChunker
from corporate_kb.models import Document


def make_document(content: str) -> Document:
    return Document(
        document_id="doc-id",
        title="Limits Service",
        source_path="services/limits.md",
        source_type="markdown",
        source_id="source-id",
        content=content,
        content_hash="content-hash",
        metadata={
            "document_type": "service",
            "service": "limits-service",
            "domain": "payments",
            "authority": "confluence",
        },
        loaded_at=datetime.now(UTC),
    )


def test_heading_paths_and_multiple_sections() -> None:
    chunker = StructuralChunker(
        SimpleTokenCounter(), target_tokens=30, hard_max_tokens=40, overlap_tokens=5
    )
    chunks = chunker.chunk(
        make_document(
            "# Limits Service\n\n## Rules\n\nDaily limit rule.\n\n### Corporate\n\nSegment rule."
        )
    )

    assert [chunk.heading_path for chunk in chunks] == [
        "Limits Service > Rules",
        "Limits Service > Rules > Corporate",
    ]
    assert all(chunk.text.strip() for chunk in chunks)
    assert "Document type: service" in chunks[0].embedding_text
    assert "Daily limit rule." in chunks[0].text
    assert "Document:" not in chunks[0].text


def test_overlap_reuses_whole_semantic_block() -> None:
    content = "# Page\n\n" + "\n\n".join(
        [
            "alpha one two three four",
            "beta one two three four",
            "gamma one two three four",
            "delta one two three four",
        ]
    )
    chunker = StructuralChunker(
        SimpleTokenCounter(), target_tokens=11, hard_max_tokens=20, overlap_tokens=6
    )
    chunks = chunker.chunk(make_document(content))

    assert len(chunks) >= 2
    assert "beta one two three four" in chunks[0].text
    assert "beta one two three four" in chunks[1].text


def test_code_fence_is_never_split_even_when_oversized() -> None:
    code = "```python\n" + "\n".join(f"value_{index} = {index}" for index in range(30)) + "\n```"
    chunker = StructuralChunker(
        SimpleTokenCounter(), target_tokens=10, hard_max_tokens=15, overlap_tokens=2
    )
    chunks = chunker.chunk(make_document(f"# Code\n\n{code}"))

    assert len(chunks) == 1
    assert chunks[0].text.startswith("```python")
    assert chunks[0].text.endswith("```")
    assert chunks[0].token_count > 15


def test_large_table_stays_whole() -> None:
    rows = ["| Key | Value |", "| --- | --- |"] + [f"| k{i} | v{i} |" for i in range(20)]
    chunker = StructuralChunker(
        SimpleTokenCounter(), target_tokens=10, hard_max_tokens=15, overlap_tokens=2
    )
    chunks = chunker.chunk(make_document("# Table\n\n" + "\n".join(rows)))

    assert len(chunks) == 1
    assert "| k0 | v0 |" in chunks[0].text
    assert "| k19 | v19 |" in chunks[0].text


def test_oversized_text_is_split_below_hard_max() -> None:
    prose = " ".join(f"word{i}." for i in range(100))
    chunker = StructuralChunker(
        SimpleTokenCounter(), target_tokens=18, hard_max_tokens=22, overlap_tokens=3
    )
    chunks = chunker.chunk(make_document(f"# Long\n\n{prose}"))

    assert len(chunks) > 1
    assert all(0 < chunk.token_count <= 22 for chunk in chunks)
