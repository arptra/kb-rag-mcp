"""Content normalization shared by filesystem loaders."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify


class DocumentNormalizer:
    """Convert source content into stable Markdown-like text."""

    _navigation_hint = re.compile(
        r"(^|[-_\s])(nav|navigation|breadcrumb|sidebar|menu|toolbar|footer|header)([-_\s]|$)",
        re.IGNORECASE,
    )

    def normalize_markdown(self, text: str) -> str:
        lines = [
            line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ]
        normalized = "\n".join(lines).strip()
        return re.sub(r"\n{3,}", "\n\n", normalized)

    def html_to_markdown(self, html: str) -> str:
        """Remove export chrome and retain semantic HTML as Markdown."""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(["script", "style", "nav", "button", "noscript", "svg"]):
            tag.decompose()
        for tag in list(soup.find_all(True)):
            class_value = tag.get("class")
            if isinstance(class_value, str):
                classes = class_value
            elif class_value is None:
                classes = ""
            else:
                classes = " ".join(str(value) for value in class_value)
            hints = " ".join(
                [
                    str(tag.get("id", "")),
                    classes,
                    str(tag.get("role", "")),
                ]
            )
            if self._navigation_hint.search(hints):
                tag.decompose()
        body = soup.body or soup
        converted = markdownify(str(body), heading_style="ATX", bullets="-")
        return self.normalize_markdown(converted)
