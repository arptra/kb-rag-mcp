"""Recover a missing local document ID from this turn's actual RAG search results."""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

from portal_agents.read_recovery import READ_CALL_PREFIX, read_results
from portal_agents.tools import DOCUMENT_NOT_FOUND

_MISSING = "portal_missing_document"
_SEARCH = "portal_document_reference_search"
_MAX_MISSES = 3
_MAX_CHOICES = 5


def _current_turn(messages: Sequence[AnyMessage]) -> Sequence[AnyMessage]:
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i + 1:]
    return ()


def _candidates(observations: list[dict], index: str, reference: str) -> tuple[dict, bool]:
    documents: dict[str, dict] = {}
    searched = False
    # Match a complete reference, so that ABC-12 does not also select ABC-120.
    token = re.compile(r"(?<![\w-])" + re.escape(reference) + r"(?![\w-])", re.IGNORECASE)
    identifier = len(reference) >= 4 and any(c.isdigit() for c in reference)
    for obs in observations:
        if obs["tool"] != "search_index" or obs["status"] != "success":
            continue
        try:
            page = json.loads(obs["result"])
        except (ValueError, RecursionError):
            continue
        if (not isinstance(page, dict) or page.get("index_id") != index
                or not isinstance(page.get("results"), list)):
            continue
        searched |= str(obs["args"].get("query", "")).strip().casefold() == reference.casefold()
        for hit in page["results"]:
            if not isinstance(hit, dict):
                continue
            doc = hit.get("document_id")
            if not isinstance(doc, str) or not doc or doc == reference:
                continue
            entry = documents.setdefault(doc, {"document_id": doc, "index_id": index,
                                               "title": str(hit.get("title") or doc)[:200],
                                               "matches": False})
            fields = [str(hit.get(key) or "") for key in ("title", "source_path", "excerpt")]
            exact = (reference in (hit.get("page_id"), hit.get("chunk_id"))
                     or any(reference.casefold() == field.casefold() for field in fields[:2]))
            entry["matches"] |= exact or (identifier and any(token.search(f) for f in fields))
    return documents, searched


def _visible_choices(documents: dict) -> list[dict]:
    ranked = sorted(documents.values(), key=lambda d: not d["matches"])
    return [{k: v for k, v in row.items() if k != "matches"} for row in ranked[:_MAX_CHOICES]]


def _local_call(name: str, args: dict, **metadata: Any) -> ModelResponse:
    return ModelResponse(result=[AIMessage(
        content="", response_metadata={"portal_read_recovery": True, **metadata},
        tool_calls=[{"id": READ_CALL_PREFIX + uuid4().hex, "name": name, "args": args}],
    )])


class _DocumentReads(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        if request.tool is None or not (request.tool.metadata or {}).get("portal_rag_document"):
            return await handler(request)
        args = request.tool_call["args"]
        default_index = request.tool.get_input_schema().model_fields["index_id"].default
        index = args.get("index_id", default_index)
        reference = args.get("document_id")
        if (not isinstance(index, str) or not isinstance(reference, str)
                or not reference.strip() or len(reference) > 4000):
            return await handler(request)
        turn = _current_turn(request.state["messages"])
        key = [index, reference]
        failed = any(isinstance(msg, ToolMessage) and msg.response_metadata.get(_MISSING) == key
                     for msg in turn)
        if not failed:
            result = await handler(request)
            if (not isinstance(result, ToolMessage) or result.status != "error"
                    or result.content != DOCUMENT_NOT_FOUND):
                return result
        evidence = read_results(turn, include_native=True)
        documents, _ = _candidates(evidence, index, reference)
        message = {"error": "document_not_found", "instruction": DOCUMENT_NOT_FOUND,
                   "candidates": _visible_choices(documents)}
        return ToolMessage(content=json.dumps(message, ensure_ascii=False), name="read_document",
                           tool_call_id=request.tool_call["id"], status="error",
                           response_metadata={_MISSING: key})

    async def awrap_model_call(self, request, handler):
        turn = _current_turn(request.messages)
        if not turn or not isinstance(turn[-1], ToolMessage):
            return await handler(request)
        key = turn[-1].response_metadata.get(_MISSING)
        if (not key and len(turn) >= 2 and turn[-1].name == "search_index"
                and turn[-1].status == "success" and isinstance(turn[-2], AIMessage)):
            key = turn[-2].response_metadata.get(_SEARCH)
        if not key:
            return await handler(request)
        index, reference = key
        available = {t.name for t in request.tools if isinstance(t, BaseTool)}
        if "read_document" not in available:
            return await handler(request)
        observations = read_results(turn, include_native=True)
        docs, searched = _candidates(observations, index, reference)
        matches = [row for row in docs.values() if row["matches"]]
        misses = [marker for m in turn if isinstance(m, ToolMessage)
                  and (marker := m.response_metadata.get(_MISSING)) and marker[0] == index]
        if len(matches) == 1:
            doc = matches[0]["document_id"]
            reads = [obs for obs in observations if obs["tool"] == "read_document"
                     and obs["args"].get("index_id", index) == index]
            original = next((obs["args"] for obs in reversed(reads)
                             if obs["args"].get("document_id") == reference), {})
            page = {k: original[k] for k in ("offset", "length") if k in original}
            already_read = any(
                obs["status"] == "success" and obs["args"].get("document_id") == doc
                and obs["args"].get("offset", 0) == page.get("offset", 0)
                and obs["args"].get("length", 6000) == page.get("length", 6000) for obs in reads
            )
            if not already_read and [index, doc] not in misses:
                return _local_call("read_document", {"index_id": index, "document_id": doc, **page})
        if (not matches and not searched and len(misses) < _MAX_MISSES
                and "search_index" in available):
            return _local_call("search_index", {"index_id": index, "query": reference},
                               **{_SEARCH: key})
        if misses.count(key) >= 2 or len(misses) >= _MAX_MISSES:
            if matches:
                titles = ", ".join("«" + " ".join(item["title"].split()) + "»"
                                   for item in matches[:_MAX_CHOICES])
                prompt = f"Поиск нашёл материалы {titles}. По какому документу продолжить?"
            else:
                prompt = "Как называется нужный документ? Можно прислать ссылку."
            return ModelResponse(result=[AIMessage(content=prompt)])
        return await handler(request)


document_reads = _DocumentReads()
