"""Render retained, authorized search hits directly in the conversation."""

# ruff: noqa: RUF001  # Russian user-facing text.
import re
from collections import defaultdict
from itertools import zip_longest
from typing import Any
from urllib.parse import urlsplit

from agent_harness.answer_quality import is_answer_source

_WORDS = re.compile(r"[\w-]{3,}")
_MARKUP = re.compile(r"([\\`*\[\]<>|~])")


def _plain(value: str) -> str:
    # Quoted source content may itself contain links, headings and lists.
    value = _MARKUP.sub(r"\\\1", value)
    value = re.sub(r"(?<!\w)_(?=\S)|(?<=\S)_(?!\w)", r"\\_", value)
    return re.sub(r"(?m)^(\s*)([#>+=-]|\d+[.)])(?=\s|$)", r"\1\\\2", value)


def _heading(item: dict[str, Any]) -> str:
    title = _plain(" ".join((item.get("title") or item["id"]).split()))
    raw = item.get("url") or ""
    try:
        url = urlsplit(raw)
        if (url.scheme in {"http", "https"} and url.hostname and url.username is None
                and not re.search(r"[\s<>\\\x00-\x1f\x7f]", raw)):
            title = f"[{title}](<{raw}>)"
    except ValueError:
        pass
    location = [item["index_id"]]
    if (path := item.get("heading_path")) and path != item.get("title"):
        location.append(path)
    if item.get("source_path") and not item.get("url"):
        location.append(item["source_path"])
    location_text = _plain(" · ".join(" ".join(s.split()) for s in location))
    ref = item["id"]
    citation = f" [{ref}]" if ref and not re.search(r"[\[\]\r\n]", ref) else ""
    return f"**{title}**\n\n{location_text}{citation}"


def source_passage(text: str, question: str, limit: int) -> str:
    """Keep a contiguous passage around query terms when a hit exceeds the display budget."""
    if len(text) <= limit:
        return text
    if limit < 4:
        return text[:limit]
    width = limit - 2
    terms = {w.casefold() for w in _WORDS.findall(question)}
    if not terms:
        return text[:limit - 1] + "…"
    end = len(text) - width
    offsets = range(0, end, max(1, width // 2))

    def score(start: int) -> int:
        words = set(_WORDS.findall(text[start:start + width].casefold()))
        return len(terms & words)

    start = max((*offsets, end), key=score)
    if start == 0:
        return text[:limit - 1] + "…"
    if start == end:
        return "…" + text[1 - limit:]
    return "…" + text[start:start + width] + "…"


def _ordered_sources(run: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in run.get("evidence", []):
        if is_answer_source(item) and item.get("id"):
            groups[item["index_id"]].append(item)
    selected = dict.fromkeys([*run.get("index_ids", []), *groups])
    columns = []
    for index in selected:
        if index in groups:
            # A direct RAG response already ranked its own chunks. For a failed
            # investigation put opened documents ahead of the initial search hits.
            columns.append(sorted(groups[index], key=lambda s: (
                not (s.get("read_document") or s.get("checked_with")),
                s.get("rank") or float("inf"),
            )))
    terms = set(_WORDS.findall(run.get("question", "").casefold()))
    if terms and len(columns) > 1:
        def relevance(group: list[dict[str, Any]]) -> int:
            top = group[0]
            label = f"{top['title']} {top.get('heading_path', '')}"
            return (2 * len(terms.intersection(_WORDS.findall(label.casefold())))
                    + len(terms.intersection(_WORDS.findall(top["text"].casefold()))))

        columns.sort(key=relevance, reverse=True)
    return [item for row in zip_longest(*columns) for item in row if item is not None]


def render_search_results(run: dict[str, Any], *, max_chars: int) -> str:
    sources = _ordered_sources(run)
    if not sources:
        statuses = {v.get("status") for v in run.get("coverage", {}).values()}
        if run.get("status") in {"cancelled", "timed_out"}:
            return "Поиск прерван до получения результатов."[:max_chars]
        if run.get("status") == "failed" and not statuses:
            return "Поиск не завершён. Проверьте доступ к выбранным источникам."[:max_chars]
        if statuses and statuses <= {"skipped"}:
            return "Для прямого поиска выберите базу знаний или граф сервисов."[:max_chars]
        if statuses and statuses <= {"failed", "pending", "skipped"}:
            return "Не удалось получить результаты из выбранных источников."[:max_chars]
        if statuses & {"failed", "skipped", "pending"}:
            return "В опрошенных источниках совпадений не найдено."[:max_chars]
        return "По выбранным источникам совпадений не найдено."[:max_chars]

    question = run.get("question", "")
    heading = "**Результаты поиска**"
    if run.get("mode") == "search":
        hit_count = sum(c.get("count", 0) for c in run.get("coverage", {}).values()
                        if c.get("status") == "searched")
        if hit_count > len(sources):
            heading += f"\n\nНайдено фрагментов: {hit_count}. Сохранено: {len(sources)}."
    blocks = [heading]
    remaining = max_chars - len(blocks[0])
    # Keep every hit intact when the direct RAG response fits in the chat.
    # Avoid allocating a second copy of an oversized research archive.
    total_text = sum(len(item["text"]) for item in sources)
    if total_text <= remaining:
        full_blocks = [
            _heading(item) + "\n\n> " + _plain(item["text"].strip()).replace("\n", "\n> ")
            for item in sources
        ]
        if sum(len(block) + 2 for block in full_blocks) <= remaining:
            return "\n\n".join([*blocks, *full_blocks])
    shown = 0
    for pos, item in enumerate(sources):
        heading = _heading(item)
        # Leave space to disclose omitted hits and give later indexes room.
        remaining_hits = len(sources) - pos
        reserved = 75 if remaining_hits > 1 or shown < pos else 0
        allowance = min(remaining - reserved, max(600, remaining // remaining_hits))
        room = allowance - len(heading) - 6
        if room < 32:
            # A long URL/section can exceed the per-hit share. Keep its source
            # identity and put the complete location in the evidence panel.
            heading = _plain(" ".join(item["title"].split())[:100])
            if not re.search(r"[\[\]\r\n]", item["id"]):
                heading += f" [{item['id']}]"
            room = allowance - len(heading) - 6
        if room < 32:
            continue
        text = item["text"].strip()
        excerpt = source_passage(text, question, room)
        quoted = "> " + _plain(excerpt).replace("\n", "\n> ")
        if len(quoted) > room + 2:
            quoted = quoted[:room].rstrip("\\") + "…"
        block = heading + "\n\n" + quoted
        blocks.append(block)
        remaining -= len(block) + 2
        shown += 1
    if shown < len(sources):
        blocks.append(f"Показано {shown} из {len(sources)}. Все материалы доступны в «Источниках».")
    return "\n\n".join(blocks)[:max_chars]
