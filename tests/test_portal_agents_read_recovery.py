import json

import httpx
import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from test_gpt2giga_transport import native_reply
from test_portal_agents_engine import Model
from test_portal_agents_tools import permitted, real_catalog

from portal_agents import engine
from portal_agents.gpt2giga_model import _LOCAL_URL, _GigaChatBridge
from portal_agents.read_recovery import READ_CALL_PREFIX, in_read_recovery, read_recovery_step
from portal_agents.tools import build_tools


async def test_graph_reads_actual_scoped_indexes_when_native_tools_fail(
    settings_factory, monkeypatch, caplog,
):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    catalog = real_catalog(settings_factory)
    public = catalog.index_directory()[0]["id"]
    hidden = catalog.create_index(name="Confidential second index").id
    tools = build_tools(catalog, authorize=permitted, index_ids=[public])
    question = "какие индексы тебе доступны?"
    events = []
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == "DeepSeek-V4-Flash"
        assert request.url.path == "/v1/chat/completions" and "tools" not in payload
        if payload.get("functions"):
            calls.append("native")
            assert "list_indexes" in {fn["name"] for fn in payload["functions"]}
            return httpx.Response(500, json={"message": "PRIVATE_MODEL_ERROR"})
        messages = payload["messages"]
        assert [msg["role"] for msg in messages] == ["system", "user"]
        frame = json.loads(messages[-1]["content"])
        assert frame["question"] == question and hidden not in messages[-1]["content"]
        assert {item["name"] for item in frame["available_tools"]} == {
            "list_indexes", "read_document", "search_index",
        }
        if not frame["observations"]:
            calls.append("select")
            assert payload["max_tokens"] == 512
            decision = {"tool": "list_indexes", "args": {}}
        else:
            calls.append("answer")
            assert payload["max_tokens"] == 2048
            [observation] = frame["observations"]
            assert observation["tool"] == "list_indexes"
            entries = json.loads(observation["result"])
            assert [row["id"] for row in entries] == [public]
            if calls.count("answer") == 1:
                return httpx.Response(503)
            decision = {"answer": "Доступен " + entries[0]["id"]}
        return native_reply({"role": "assistant", "content": json.dumps(decision)}, "stop")

    async def progress(kind, detail):
        events.append((kind, detail))

    async with (
        httpx.AsyncClient(base_url="https://corporate.test/v1",
                          transport=httpx.MockTransport(respond)) as upstream,
        httpx.AsyncClient(base_url=_LOCAL_URL,
                          transport=_GigaChatBridge(upstream, "DeepSeek-V4-Flash", None)) as client,
    ):
        model = ChatOpenAI(model="DeepSeek-V4-Flash", api_key="local", base_url=_LOCAL_URL,
                           http_async_client=client, disable_streaming=True,
                           use_responses_api=False, max_retries=0, extra_body={"max_tokens": 32768})
        answer = await engine.run_turn(model, question, tools, on_event=progress)
    assert answer == "Доступен " + public
    assert [detail for kind, detail in events if kind == "tool_start"] == ["list_indexes"]
    assert calls == ["native", "native", "select", "answer", "answer"]
    assert "on_llm_error callback" not in caplog.text and "PRIVATE_MODEL_ERROR" not in caplog.text


@tool
async def search_index(query: str) -> str:
    """Search the current documents."""
    pytest.fail("A model decision must go through the graph before executing")


@pytest.mark.parametrize("decision", [
    {"answer": "invented index"}, {"tool": "arbitrary", "args": {}},
    {"tool": "search_index", "args": {"query": 123}},
    {"tool": "search_index", "args": {"query": "x", "private": True}}, {"defer": True},
])
async def test_unverified_or_unauthorized_decision_defers_to_normal_agent(decision):
    question = HumanMessage("Find a document")
    request = ModelRequest(model=Model(respond=lambda *_: AIMessage(content=json.dumps(decision))),
                           messages=[question], tools=[search_index])
    assert await read_recovery_step(request, question) is None


async def test_recovery_accepts_only_current_paired_reads_then_yields_to_native_tool():
    call_id = READ_CALL_PREFIX + "example"
    call = AIMessage(content="", response_metadata={"portal_read_recovery": True},
                     tool_calls=[{"id": call_id, "name": "search_index", "args": {"query": "old"}}])
    result = ToolMessage(content="PRIVATE", tool_call_id=call_id, name="search_index")
    question = HumanMessage("New task")
    messages = [HumanMessage("Old task"), call, result, question, result]
    assert not in_read_recovery(messages, 3)

    def respond(msgs, names):
        assert "PRIVATE" not in msgs[-1].text
        return AIMessage(content='{"answer":"fabricated"}')

    request = ModelRequest(model=Model(respond=respond), messages=messages, tools=[search_index])
    assert await read_recovery_step(request, question) is None
    assert in_read_recovery([question, call, result], 0)
    assert not in_read_recovery([question, call.model_copy(update={"response_metadata": {}}),
                                 result], 0)
    assert not in_read_recovery([question, call, result, AIMessage(content="", tool_calls=[
        {"id": "native", "name": "search_index", "args": {"query": "new"}},
    ]), ToolMessage(content="other", name="search_index", tool_call_id="native")], 0)
