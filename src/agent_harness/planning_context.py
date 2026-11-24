"""Select observations for the next tool decision without changing persisted evidence."""

from __future__ import annotations

import re
from typing import Any

from agent_harness.research import service_inventory_question

_WORDS = re.compile(r"[\w-]{3,}")
_MAX_OBSERVATIONS = 8
_EXCERPT_CHARS = 3200
_CATALOG = re.compile(r"\b(catalog|catalogue|inventory|registry|graph|реестр|каталог|граф)\b")


def discovery_connection(
    question: str, indexes: list[str], names: dict[str, str], permitted: dict[str, set[str]],
) -> str | None:
    """Choose at most one selected MCP whose catalog can directly guide this question."""
    # A discovery-enabled connection can have no individually pinned tool names yet.
    options = [index for index in indexes if index in permitted]
    query = question.casefold()
    for index in options:
        label = names.get(index, "").removeprefix("MCP · ").strip()
        for name in (index.removeprefix("connection:"), label):
            if name and re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", query):
                return index
    if service_inventory_question(question):
        for index in options:
            capabilities = " ".join([index, names.get(index, ""), *permitted[index]])
            words = re.sub(r"(?<=[a-z])(?=[A-Z])|_", " ", capabilities).casefold()
            if _CATALOG.search(words):
                return index
    if options and (len(indexes) == 1 or re.search(r"\b(mcp|мср)\b", query)):
        return options[0]
    return None


def archive_excerpts(
    run: dict[str, Any], exchange: list[dict[str, Any]], selected: set[str],
) -> list[dict[str, Any]]:
    search = next((step for step in reversed(exchange)
                   if step.get("name") == "search_collected_materials"), {})
    if not search.get("archive_matches"):
        return []
    retained = {(e["index_id"], e["id"]): e["text"] for e in run["evidence"]
                if e["index_id"] in selected}
    result = []
    for match in search["archive_matches"][:4]:
        text = retained.get((match["index_id"], match["id"]))
        start, end = match["offset"], match["end_offset"]
        if (text is not None and 0 <= start < end <= len(text) and end - start <= 260
                and text[start:end] == match["excerpt"]):
            result.append(match)
    return result


def new_read(run: dict[str, Any], exchange: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Find a targeted read completed since the last model response."""
    for step in reversed(run.get("research_trace", [])):
        name = step.get("name")
        if name == "model_response":
            break
        if name in {"read_document", "read_repository"}:
            receipt = next((entry for entry in reversed(exchange) if entry.get("name") == name), {})
            refs = receipt.get("evidence_refs", [])
            if refs:
                index_id, evidence_id = refs[0]
                return index_id, evidence_id
            break
    return None


def planning_evidence(
    run: dict[str, Any], exchange: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    evidence = run["evidence"]
    plan = run.get("research_plan", {})
    branch = plan.get("current_branch") or {}
    focus = " ".join([branch.get("subquestion", ""), branch.get("target", ""),
                      *plan.get("gaps", []), plan.get("next_action", "")])
    focus_terms = list(dict.fromkeys(_WORDS.findall(focus.casefold())))[:24]
    question_terms = list(dict.fromkeys(_WORDS.findall(run["question"].casefold())))[:24]
    # Each completed tool batch identifies the actual retained results, including
    # documents that replaced a search hit in place. Newest batch comes first.
    fresh: dict[tuple[str, str], int] = {}
    batches = [step["evidence_refs"] for step in exchange if step.get("evidence_refs")]
    for age, batch in enumerate(reversed(batches[-2:])):
        for index, doc_id in batch:
            fresh.setdefault((index, doc_id), 2 - age)

    def score(text: str) -> int:
        text = text.casefold()
        return (3 * sum(term in text for term in focus_terms)
                + sum(term in text for term in question_terms))

    def key(item: dict[str, Any]) -> tuple[int, int, int]:
        relevance = score(f"{item['title']} {item['id']}") * 3 + score(item["text"])
        return relevance, fresh.get((item["index_id"], item["id"]), 0), int(bool(
            item.get("read_document") or item.get("checked_with")))

    ranked = sorted(reversed(evidence), key=key, reverse=True)
    recent = sorted(
        (item for item in ranked if (item["index_id"], item["id"]) in fresh),
        key=lambda item: fresh[(item["index_id"], item["id"])], reverse=True,
    )
    # Reserve space for the latest result, a complete active tool contract, and
    # leading evidence from distinct sources before filling spare positions.
    contracts = [item for item in (recent + ranked) if item.get("kind") == "tool_metadata"]
    by_source: dict[str, dict[str, Any]] = {}
    for item in ranked:
        if item.get("kind") != "tool_metadata":
            by_source.setdefault(item["index_id"], item)
    candidates = [*recent[:2], *contracts[:1], *ranked[:2], *by_source.values(), *ranked]
    result = []
    selected: set[tuple[str, str]] = set()
    for item in candidates:
        ref = item["index_id"], item["id"]
        if ref in selected:
            continue
        selected.add(ref)
        text = item["text"]
        if item.get("kind") != "tool_metadata" and len(text) > _EXCERPT_CHARS:
            # Overlap windows so a service identifier or reference crossing a
            # boundary remains readable. Keep actual source text and offsets.
            offsets = range(0, len(text) - _EXCERPT_CHARS, _EXCERPT_CHARS // 2)
            last = len(text) - _EXCERPT_CHARS
            failed = item.get("tool_outcome") == "failed"
            start = max((*offsets, last), key=lambda i: (
                score(text[i:i + _EXCERPT_CHARS]), i if failed else -i,
            ))
            item = {**item, "text": text[start:start + _EXCERPT_CHARS],
                    "context_truncated": True, "context_offset": start,
                    "retained_chars": len(text)}
        result.append(item)
        if len(result) >= _MAX_OBSERVATIONS:
            break
    return result
