"""Loader interfaces and shared document construction helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import frontmatter

from corporate_kb.models import Document


class DocumentLoader(Protocol):
    """Load one supported file into a normalized document."""

    def load(self, path: Path, knowledge_root: Path) -> Document: ...


def parse_front_matter(raw: str) -> tuple[dict[str, Any], str]:
    """Parse YAML front matter while preserving every metadata field."""
    post = frontmatter.loads(raw)
    return dict(post.metadata), post.content


def first_h1(content: str) -> str | None:
    """Return the first ATX H1 heading, if present."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip().rstrip("#").strip()
            if title:
                return title
    return None


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def make_document(
    *,
    path: Path,
    knowledge_root: Path,
    source_type: str,
    content: str,
    front_matter: dict[str, Any],
) -> Document:
    """Build a document with safe paths, defaults, and deterministic identifiers."""
    resolved_root = knowledge_root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError(f"Document path escapes KB_KNOWLEDGE_DIR: {path}")
    relative_path = resolved_path.relative_to(resolved_root).as_posix()

    title = str(front_matter.get("title") or first_h1(content) or path.stem).strip()
    source_id = str(front_matter.get("source_id") or relative_path)
    source_url_raw = front_matter.get("source_url")
    source_url = str(source_url_raw) if source_url_raw not in (None, "") else None
    metadata = dict(front_matter)
    metadata.setdefault("status", "current")
    metadata.setdefault("authority", "local_file")
    metadata.setdefault("authority_priority", 50)
    metadata["source_type"] = source_type

    normalized = content.strip()
    content_hash_payload = f"{normalized}\n{stable_json(metadata)}"
    content_hash = hashlib.sha256(content_hash_payload.encode("utf-8")).hexdigest()
    document_id_input = f"{relative_path}\0{source_id}\0{content_hash}"
    document_id = hashlib.sha256(document_id_input.encode("utf-8")).hexdigest()
    return Document(
        document_id=document_id,
        title=title,
        source_path=relative_path,
        source_type=source_type,
        source_id=source_id,
        source_url=source_url,
        content=normalized,
        content_hash=content_hash,
        metadata=metadata,
        loaded_at=datetime.now(UTC),
    )
