"""Confluence-exported HTML document loader."""

from pathlib import Path

from corporate_kb.loaders.base import make_document, parse_front_matter
from corporate_kb.loaders.normalizer import DocumentNormalizer
from corporate_kb.models import Document


class HtmlDocumentLoader:
    def __init__(self, normalizer: DocumentNormalizer | None = None) -> None:
        self._normalizer = normalizer or DocumentNormalizer()

    def load(self, path: Path, knowledge_root: Path) -> Document:
        metadata, body = parse_front_matter(path.read_text(encoding="utf-8"))
        content = self._normalizer.html_to_markdown(body)
        return make_document(
            path=path,
            knowledge_root=knowledge_root,
            source_type="html",
            content=content,
            front_matter=metadata,
        )
