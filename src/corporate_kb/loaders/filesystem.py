"""Safe recursive loader for the configured knowledge directory."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from corporate_kb.loaders.base import DocumentLoader
from corporate_kb.loaders.html import HtmlDocumentLoader
from corporate_kb.loaders.markdown import MarkdownDocumentLoader
from corporate_kb.loaders.text import TextDocumentLoader
from corporate_kb.models import Document

logger = logging.getLogger(__name__)

_IGNORED_PARTS = {".git", ".cache", "__pycache__", "node_modules"}
MARKDOWN_SUFFIXES = {".md", ".markdown"}
HTML_SUFFIXES = {".html", ".htm"}
PLAIN_TEXT_SUFFIXES = {
    ".txt",
    ".rst",
    ".adoc",
    ".log",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".properties",
}
SUPPORTED_DOCUMENT_SUFFIXES = MARKDOWN_SUFFIXES | HTML_SUFFIXES | PLAIN_TEXT_SUFFIXES


class FileSystemDocumentLoader:
    """Discover supported files, reject escapes, and load in stable path order."""

    def __init__(self) -> None:
        markdown = MarkdownDocumentLoader()
        html = HtmlDocumentLoader()
        text = TextDocumentLoader()
        self._loaders: dict[str, DocumentLoader] = {
            **{suffix: markdown for suffix in MARKDOWN_SUFFIXES},
            **{suffix: html for suffix in HTML_SUFFIXES},
            **{suffix: text for suffix in PLAIN_TEXT_SUFFIXES},
        }

    def load_directory(self, knowledge_root: Path) -> list[Document]:
        root = knowledge_root.resolve()
        if not root.exists():
            raise FileNotFoundError(f"Knowledge directory does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Knowledge path is not a directory: {root}")

        paths: list[Path] = []

        def on_walk_error(error: OSError) -> None:
            logger.warning("Skipping directory that cannot be read: %s", error)

        # Do not use Path.rglob here: an exported knowledge tree can contain symlinked
        # directories, including cycles back to an ancestor. os.walk with followlinks=False
        # lets us prune those entries before they can become recursive traversal.
        for current_dir, dir_names, file_names in os.walk(
            root,
            topdown=True,
            followlinks=False,
            onerror=on_walk_error,
        ):
            current = Path(current_dir)
            dir_names[:] = [
                name
                for name in dir_names
                if not name.startswith(".")
                and name not in _IGNORED_PARTS
                and not (current / name).is_symlink()
            ]
            for name in file_names:
                path = current / name
                relative = path.relative_to(root)
                if any(
                    part.startswith(".") or part in _IGNORED_PARTS for part in relative.parts
                ):
                    continue
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.suffix.lower() not in self._loaders
                ):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    logger.warning("Skipping unresolved file %s: %s", path, exc)
                    continue
                if not resolved.is_relative_to(root):
                    logger.warning("Skipping file outside knowledge directory: %s", path)
                    continue
                paths.append(path)

        ordered_paths = sorted(paths, key=lambda item: item.relative_to(root).as_posix())
        logger.info("Discovered %d knowledge files under %s", len(ordered_paths), root)
        documents: list[Document] = []
        progress_step = max(100, len(ordered_paths) // 20 or 1)
        for position, path in enumerate(ordered_paths, start=1):
            try:
                raw_prefix = path.read_bytes()[:8192]
                if b"\x00" in raw_prefix:
                    logger.warning("Skipping binary-looking file: %s", path)
                    continue
                documents.append(self._loaders[path.suffix.lower()].load(path, root))
            except Exception as exc:
                # One malformed export must not abort a 10k-document indexing run. This also
                # catches YAML/parser failures and RecursionError from pathological HTML nesting.
                logger.warning(
                    "Skipping unreadable or malformed document %s (%s): %s",
                    path,
                    type(exc).__name__,
                    exc,
                )
            if position == 1 or position % progress_step == 0 or position == len(ordered_paths):
                logger.info(
                    "Loaded knowledge files: %d/%d (documents=%d)",
                    position,
                    len(ordered_paths),
                    len(documents),
                )
        logger.info("Loaded %d knowledge documents", len(documents))
        return documents
