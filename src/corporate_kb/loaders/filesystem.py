"""Safe recursive loader for the configured knowledge directory."""

from __future__ import annotations

import logging
from pathlib import Path

from corporate_kb.loaders.base import DocumentLoader
from corporate_kb.loaders.html import HtmlDocumentLoader
from corporate_kb.loaders.markdown import MarkdownDocumentLoader
from corporate_kb.loaders.text import TextDocumentLoader
from corporate_kb.models import Document

logger = logging.getLogger(__name__)

_IGNORED_PARTS = {".git", ".cache", "__pycache__", "node_modules"}


class FileSystemDocumentLoader:
    """Discover supported files, reject escapes, and load in stable path order."""

    def __init__(self) -> None:
        markdown = MarkdownDocumentLoader()
        html = HtmlDocumentLoader()
        text = TextDocumentLoader()
        self._loaders: dict[str, DocumentLoader] = {
            ".md": markdown,
            ".markdown": markdown,
            ".html": html,
            ".htm": html,
            ".txt": text,
        }

    def load_directory(self, knowledge_root: Path) -> list[Document]:
        root = knowledge_root.resolve()
        if not root.exists():
            raise FileNotFoundError(f"Knowledge directory does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Knowledge path is not a directory: {root}")

        paths: list[Path] = []
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if any(part.startswith(".") or part in _IGNORED_PARTS for part in relative.parts):
                continue
            if not path.is_file() or path.suffix.lower() not in self._loaders:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                logger.warning("Skipping file outside knowledge directory: %s", path)
                continue
            paths.append(path)

        documents: list[Document] = []
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
            try:
                raw_prefix = path.read_bytes()[:8192]
                if b"\x00" in raw_prefix:
                    logger.warning("Skipping binary-looking file: %s", path)
                    continue
                documents.append(self._loaders[path.suffix.lower()].load(path, root))
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF-8 file: %s", path)
        logger.info("Loaded %d knowledge documents", len(documents))
        return documents
