"""Read-only literal search over the evidence retained in one Autopilot run."""

import asyncio
from contextvars import Context
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langsmith import tracing_context
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from agent_harness.answer_quality import is_answer_source


class ArchiveQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=2, max_length=160, pattern=r"^[^\r\n]+$")


ARCHIVE_TOOL = {
    "type": "function", "function": {
        "name": "search_collected_materials",
        "description": (
            "Search literal, case-sensitive text across the retained excerpts from this run, "
            "including observations omitted from the model window. Returns exact passages, "
            "source IDs and offsets. Follow up with a source tool for independent verification."
        ),
        "parameters": ArchiveQuery.model_json_schema(),
    },
}


@cache
def deep_agents_installed() -> bool:
    # Keep the base portal and existing Harness installations usable before setup.
    try:
        return Version(version("deepagents")) >= Version("0.7.39")
    except PackageNotFoundError:
        return False


def available(run: dict[str, Any]) -> bool:
    if run["mode"] != "autonomous" or not run.get("research_plan", {}).get("current_branch"):
        return False
    sources = [e for e in run["evidence"] if is_answer_source(e) and not e.get("tool_outcome")]
    return deep_agents_installed() and (
        len(sources) > 8 or any(len(e["text"]) > 3200 for e in sources)
    )


class _ArchiveState(TypedDict, total=False):
    files: dict[str, dict[str, str]]
    query: str
    matches: list[dict[str, Any]]


@cache
def _reader() -> CompiledStateGraph[_ArchiveState, None, _ArchiveState, _ArchiveState]:
    # The isolated child graph supplies the files channel required by StateBackend.
    # It has no checkpointer, model, host filesystem, writes or networking tools.
    from deepagents.backends import StateBackend

    def search(state: _ArchiveState) -> dict[str, Any]:
        query = state["query"]
        result = StateBackend().grep(query, "/sources", max_count=4)
        if result.error:
            raise ValueError("Archive search failed")
        matches = []
        for match in result.matches or []:
            path, line = match["path"], match["line"]
            body = state["files"][path]["content"]
            # Deep Agents reports one-indexed lines, splitting only on LF.
            prefix = body.split("\n", line - 1)[:line - 1]
            offset = sum(len(part) + 1 for part in prefix) + match["text"].index(query)
            start = max(0, offset - 50)
            end = min(len(body), start + 260)
            matches.append({"path": path, "line": line, "offset": start, "end_offset": end,
                            "excerpt": body[start:end]})
        return {"matches": matches}

    builder = StateGraph(_ArchiveState)
    builder.add_node("search", search)
    builder.add_edge(START, "search")
    builder.add_edge("search", END)
    return builder.compile(checkpointer=False, name="search-retained-materials")


async def search_materials(
    run: dict[str, Any], args: dict[str, Any], allowed: set[str],
) -> list[dict[str, Any]]:
    query = ArchiveQuery.model_validate(args).query.strip()
    if len(query) < 2 or not available(run):
        raise ValueError("Archive search unavailable")
    scope = set(run["index_ids"]) & allowed
    sources = {f"/sources/{n}.txt": e for n, e in enumerate(run["evidence"])
               if e["index_id"] in scope and is_answer_source(e) and not e.get("tool_outcome")}
    files = {path: {"content": e["text"], "encoding": "utf-8"} for path, e in sources.items()}
    # A fresh execution context keeps the parent's durable SQLite checkpoint
    # configuration and callbacks out of this entirely ephemeral search.
    async def run_local() -> list[dict[str, Any]]:
        with tracing_context(enabled=False):
            graph = await asyncio.to_thread(_reader)
            result = await graph.ainvoke({"files": files, "query": query})
            hits: list[dict[str, Any]] = result["matches"]
            return hits

    hits = await asyncio.create_task(run_local(), context=Context())
    matches = []
    for match in hits:
        source = sources[match.pop("path")]
        matches.append({**match, "index_id": source["index_id"], "id": source["id"]})
    return matches
