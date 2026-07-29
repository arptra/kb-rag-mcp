"""Semantic block parsing for normalized Markdown-like text."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+] |\d+[.)] )")


@dataclass(frozen=True, slots=True)
class Block:
    kind: str
    text: str
    heading_path: tuple[str, ...]


def parse_markdown_blocks(content: str, fallback_heading: str) -> list[Block]:
    """Parse headings, prose, lists, tables, and fenced code without splitting them."""
    lines = content.splitlines()
    headings: list[str] = []
    blocks: list[Block] = []
    paragraph: list[str] = []
    index = 0

    def path() -> tuple[str, ...]:
        return tuple(headings) or (fallback_heading,)

    def flush_paragraph() -> None:
        if paragraph:
            text = "\n".join(paragraph).strip()
            if text:
                blocks.append(Block(kind="text", text=text, heading_path=path()))
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            value = heading.group(2).strip()
            headings[level - 1 :] = [value]
            index += 1
            continue
        if line.lstrip().startswith(("```", "~~~")):
            flush_paragraph()
            fence = line.lstrip()[:3]
            code_lines = [line]
            index += 1
            while index < len(lines):
                code_lines.append(lines[index])
                if lines[index].lstrip().startswith(fence):
                    index += 1
                    break
                index += 1
            blocks.append(
                Block(kind="code", text="\n".join(code_lines).strip(), heading_path=path())
            )
            continue
        if line.strip().startswith("|"):
            flush_paragraph()
            table_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].rstrip())
                index += 1
            blocks.append(
                Block(kind="table", text="\n".join(table_lines).strip(), heading_path=path())
            )
            continue
        if _LIST.match(line):
            flush_paragraph()
            list_lines: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if _LIST.match(candidate) or (
                    candidate.startswith(("  ", "\t")) and candidate.strip()
                ):
                    list_lines.append(candidate.rstrip())
                    index += 1
                    continue
                break
            blocks.append(
                Block(kind="list", text="\n".join(list_lines).strip(), heading_path=path())
            )
            continue
        if not line.strip():
            flush_paragraph()
            index += 1
            continue
        paragraph.append(line.rstrip())
        index += 1

    flush_paragraph()
    return blocks
