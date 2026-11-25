import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_portal_agents_engine import Model
from test_portal_agents_tools import permitted, real_catalog

from portal_agents.document_reads import document_reads
from portal_agents.engine import run_turn
from portal_agents.tools import build_tools


def _knowledge(settings_factory, monkeypatch, *, ambiguous=False):
    catalog = real_catalog(settings_factory)
    index = catalog.index_directory()[0]["id"]
    service = catalog.service_for(index)
    root = service.settings.knowledge_dir
    (root / "shift-log.md").write_text("# Журнал смены\n\nAZR-318 передан в сопровождение.")
    if ambiguous:
        (root / "billing.md").write_text("# Сверка\n\nAZR-318 учитывается при сверке.")
    service.build_index(force=True)
    kb = catalog.tools_for(index)
    read = Mock(wraps=kb.read_current_document)
    monkeypatch.setattr(kb, "read_current_document", read)
    tools = build_tools(catalog, authorize=permitted, index_ids=[index])
    return index, read, tools


def _propose(names, name, **args):
    if names:
        return AIMessage(content="", tool_calls=[{"id": str(uuid4()), "name": name, "args": args}])
    return AIMessage(content=json.dumps({"tool": name, "args": args}))


@pytest.mark.parametrize("prefer_reads", [False, True])
@pytest.mark.parametrize("search_first", [False, True])
async def test_reference_found_in_one_document_drives_one_real_graph_read(
    settings_factory, monkeypatch, prefer_reads, search_first,
):
    index, read, tools = _knowledge(settings_factory, monkeypatch)
    question = "Что случилось по заявке AZR-318?"
    start = []
    previous = [
        HumanMessage("another investigation"),
        AIMessage(content="", tool_calls=[{"id": "old", "name": "search_index",
                                            "args": {"query": "AZR-318", "index_id": index}}]),
        ToolMessage(json.dumps({"index_id": index, "results": [
            {"document_id": "old-id", "excerpt": "AZR-318", "title": "old private result"},
        ]}), tool_call_id="old", name="search_index"),
        AIMessage("Resolved"),
    ]

    def respond(messages, names):
        if names:
            no_reads = isinstance(messages[-1], HumanMessage)
        else:
            frame = json.loads(messages[-1].text)
            no_reads = not frame["observations"]
            if frame["observations"]:
                latest = frame["observations"][-1]
                if latest["tool"] == "read_document" and latest["status"] == "success":
                    content = json.loads(latest["result"])["content"]
                    return AIMessage(content=json.dumps({"answer": content}))
        if no_reads and search_first:
            return _propose(names, "search_index", query="Журнал смены", index_id=index)
        return _propose(names, "read_document", document_id="AZR-318", index_id=index)

    async def progress(event, message):
        if event == "tool_start":
            start.append(message)

    answer = await run_turn(Model(respond=respond), question, tools, prefer_chat_reads=prefer_reads,
                            history=previous, on_event=progress, recursion_limit=14)
    assert "передан в сопровождение" in answer
    ids = [c.kwargs["document_id"] for c in read.call_args_list]
    assert len(ids) == 2 and ids[0] == "AZR-318" and ids[1] not in {"AZR-318", "old-id"}
    assert start.count("read_document") == 2 and start.count("search_index") == 1


@pytest.mark.parametrize("prefer_reads", [False, True])
async def test_ambiguous_matches_are_offered_and_repeating_does_not_read_either_document(
    settings_factory, monkeypatch, prefer_reads,
):
    index, read, tools = _knowledge(settings_factory, monkeypatch, ambiguous=True)
    candidates = []

    def repeat(messages, names):
        if len(messages) == 2 and not names:
            frame = json.loads(messages[-1].text)
            if frame["observations"]:
                last = frame["observations"][-1]
                if last["tool"] == "search_index":
                    rows = json.loads(last["result"])["results"]
                    candidates.extend(row["document_id"] for row in rows
                                      if "AZR-318" in row["excerpt"])
        return _propose(names, "read_document", document_id="AZR-318", index_id=index)

    answer = await run_turn(Model(respond=repeat), "Проверь AZR-318", tools,
                            prefer_chat_reads=prefer_reads, recursion_limit=14)
    assert len(set(candidates)) == 2 and "Сверка" in answer and "Журнал смены" in answer
    assert "По какому документу" in answer
    read.assert_called_once_with(document_id="AZR-318")


async def test_different_invented_ids_cannot_consume_the_run(settings_factory, monkeypatch):
    index, read, tools = _knowledge(settings_factory, monkeypatch)
    attempted = []
    searched = []

    def invent(messages, names):
        reference = f"unknown-{len(attempted)}"
        attempted.append(reference)
        return _propose(names, "read_document", document_id=reference, index_id=index)

    async def progress(kind, name):
        if kind == "tool_start" and name == "search_index":
            searched.append(name)

    answer = await run_turn(Model(respond=invent), "Найди старый протокол", tools,
                            prefer_chat_reads=True, on_event=progress, recursion_limit=32)
    assert len(attempted) == read.call_count == 3
    assert len(searched) == 2 and "Как называется нужный документ?" in answer


async def test_transient_read_failure_is_not_recorded_as_a_missing_document(
    settings_factory, monkeypatch,
):
    index, _, tools = _knowledge(settings_factory, monkeypatch)
    tool = next(t for t in tools if t.name == "read_document")
    previous = ToolMessage("temporary outage", tool_call_id="first", status="error",
                           name="read_document")
    request = SimpleNamespace(tool=tool, state={"messages": [HumanMessage("read doc"), previous]},
                              tool_call={"id": "second", "name": "read_document",
                                         "args": {"index_id": index, "document_id": "AZR-318"}})
    handler = AsyncMock(return_value=previous)
    result = await document_reads.awrap_tool_call(request, handler)
    assert result is previous and not result.response_metadata
    handler.assert_awaited_once_with(request)
