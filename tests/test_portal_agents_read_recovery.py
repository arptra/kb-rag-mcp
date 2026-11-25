import json
import re
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from test_a2a import indexed
from test_gpt2giga_transport import native_reply
from test_portal_agents_connections import ADMIN, READER, ROOT, browser
from test_portal_agents_engine import Model
from test_portal_agents_tools import permitted, real_catalog

from portal_agents import connections, engine, gpt2giga_model, tools, workspace
from portal_agents.gpt2giga_model import _LOCAL_URL, _GigaChatBridge
from portal_agents.read_recovery import (
    READ_CALL_PREFIX,
    ReadDecisionError,
    in_read_recovery,
    read_recovery_step,
)
from portal_agents.tools import build_tools

FOLLOWUP = "Когда был предыдущий?"


@pytest.mark.parametrize(("status", "previous_read", "expected_calls"), [
    (500, False, ["native", "native", "select", "answer", "answer"]),
    (422, False, ["native", "select", "answer", "answer"]),
    (422, True, ["native", "native", "answer", "answer"]),
])
async def test_graph_reads_actual_scoped_indexes_when_native_tools_fail(
    settings_factory, monkeypatch, caplog, status, previous_read, expected_calls,
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
            if previous_read and len(calls) == 1:
                return native_reply({"role": "assistant", "content": "",
                                     "functions_state_id": "index_inventory",
                                     "function_call": {"name": "list_indexes", "arguments": {}}},
                                    "function_call")
            return httpx.Response(status, json={"detail": [
                {"loc": ["body", "functions", 0, "parameters"],
                 "type": "value_error", "msg": "PRIVATE_MODEL_ERROR"},
            ]})
        messages = payload["messages"]
        assert [msg["role"] for msg in messages] == ["system", "user"]
        frame = json.loads(messages[-1]["content"])
        assert frame["question"] == question and hidden not in messages[-1]["content"]
        assert {item["name"] for item in frame["available_tools"]} == {
            "list_connections", "list_indexes", "read_document", "search_index",
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
    assert calls == expected_calls
    assert "on_llm_error callback" not in caplog.text and "PRIVATE_MODEL_ERROR" not in caplog.text


def _corporate_portal(settings_factory):
    model_name = "DeepSeek-V4-Flash"
    return indexed(
        settings_factory, agents_enabled=True, a2a_enabled=False,
        admin_password=ADMIN["X-KB-Admin-Password"],
        mcp_http_bearer_token=READER["Authorization"].removeprefix("Bearer "),
        agents_model_base_url="https://model.test/corp/v1", agents_model_name=model_name,
    )


def _corporate_transport(monkeypatch, provider):
    chat_async = gpt2giga_model.chat_async

    async def local_exchange(client, *, chat, access_token):
        async with httpx.AsyncClient(base_url=client.base_url,
                                     transport=httpx.MockTransport(provider)) as upstream:
            return await chat_async(upstream, chat=chat, access_token=access_token)

    monkeypatch.setattr(gpt2giga_model, "chat_async", local_exchange)


async def test_corporate_chat_answers_workspace_and_mcp_inventory_from_live_settings(
    settings_factory, monkeypatch,
):
    portal = _corporate_portal(settings_factory)
    questions = {"какие тебе достпны песочницы": "workspaces",
                 "какие mcp тебе доступны": "mcp"}
    expected = {"workspaces": [], "mcp": []}
    native_calls = []
    secret = "private-jira-auth-value"

    def describe(kind, rows):
        if kind == "workspaces":
            return "\n".join(f"{row['name']} ({row['image']})" for row in rows) or "Песочниц нет."
        return "\n".join(f"{row['name']} · {', '.join(row['tools'])}" for row in rows) or "MCP нет."

    def provider(request):
        payload = json.loads(request.content)
        assert request.url.path == "/corp/v1/chat/completions"
        assert payload["model"] == "DeepSeek-V4-Flash"
        assert secret not in request.content.decode() and "private-mcp.test" not in str(payload)
        if payload.get("functions"):
            native_calls.append(True)
            return httpx.Response(500)
        frame = json.loads(payload["messages"][-1]["content"])
        kind = questions[frame["question"]]
        if not frame["observations"]:
            assert "list_connections" in {t["name"] for t in frame["available_tools"]}
            content = json.dumps({"tool": "list_connections", "args": {"kind": kind}})
        else:
            [observation] = frame["observations"]
            inventory = json.loads(observation["result"])
            assert observation["tool"] == "list_connections"
            assert inventory == {kind: expected[kind]}
            content = describe(kind, inventory[kind])
        return native_reply({"role": "assistant", "content": content}, "stop")

    _corporate_transport(monkeypatch, provider)
    monkeypatch.setattr(connections, "_images", AsyncMock(return_value=["gradle:8"]))
    probe = AsyncMock()
    monkeypatch.setattr(connections, "_check_workspace", probe)
    docker = AsyncMock(side_effect=AssertionError("Inventory must not start a container"))
    monkeypatch.setattr(workspace, "_docker", docker)
    remote = Mock(side_effect=AssertionError("Inventory must not contact an MCP server"))
    monkeypatch.setattr(tools, "_session", remote)

    async with browser(portal) as client:
        async def ask_inventory():
            for question, kind in questions.items():
                response = await client.post("/agents/api/chat", headers=READER,
                                             json={"chat": str(uuid4()), "question": question})
                response.raise_for_status()
                events = [json.loads(line) for line in response.text.splitlines()]
                assert events[-1]["event"] == "answer", events
                assert [e["text"] for e in events if e["event"] == "tool_start"] == [
                    "list_connections",
                ]
                assert events[-1]["text"] == describe(kind, expected[kind])
                assert "model_retry" not in {e["event"] for e in events}

        await ask_inventory()
        room = {"name": "Корпоративная Java", "image": "gradle:8"}
        (await client.put(ROOT + "/workspace", headers=ADMIN, json=room)).raise_for_status()
        added = await client.post(ROOT + "/servers", headers=ADMIN, json={
            "name": "Журнал задач", "url": "https://private-mcp.test/jira",
        })
        added.raise_for_status()
        server_id = added.json()["id"]
        (await client.put(f"{ROOT}/servers/{server_id}", headers=ADMIN, json={
            "tools": ["get_issue"], "token": secret,
        })).raise_for_status()
        expected["workspaces"] = [room]
        expected["mcp"] = [{"id": server_id, "name": "Журнал задач", "tools": ["get_issue"]}]
        await ask_inventory()
    assert native_calls == []
    probe.assert_awaited_once_with("gradle:8")
    docker.assert_not_awaited()
    remote.assert_not_called()


@pytest.mark.parametrize("reject_plain", [False, True])
async def test_corporate_release_question_through_search_and_document(
    settings_factory, monkeypatch, reject_plain,
):
    service, settings = _corporate_portal(settings_factory)
    (settings.knowledge_dir / "journal.md").write_text(
        "# Журнал эксплуатации ЕКП\n\n"
        "2026-08-21 — ЕКП 4.1, развёрнут в продуктиве.\n"
        "2026-10-14 — ЕКП 4.2, развёрнут в продуктиве.\n"
        "2026-12-01 — ЕКП 4.3, запланирован.\n"
    )
    service.build_index(force=True)
    question = "когда последний релиз ЕКП?"
    private = "PRIVATE_INVALID_ARG"
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        assert payload["model"] == "DeepSeek-V4-Flash"
        calls.append(payload.get("functions"))
        assert not calls[-1]
        if reject_plain:
            return httpx.Response(422, json={"detail": [
                {"loc": ["body", "messages"], "msg": "PRIVATE_VALIDATION"},
            ]})
        frame = json.loads(payload["messages"][-1]["content"])
        assert frame["question"] in {question, FOLLOWUP}
        second = frame["question"] == FOLLOWUP
        if second:
            prior_question, prior_answer = frame["recent_conversation"]
            assert prior_question == {"role": "user", "text": question}
            assert prior_answer["role"] == "assistant" and "ЕКП 4.2" in prior_answer["text"]
            assert private not in str(frame)
        observations = frame["observations"]
        if not observations:
            args = {}
            if second and not frame.get("correction"):
                args = {private: True}
            decision = {"tool": "list_indexes", "args": args}
        else:
            last = observations[-1]
            result = json.loads(last["result"])
            if last["tool"] == "list_indexes":
                index = result[0]["id"]
                decision = {"tool": "search_index", "args": {
                    "index_id": index, "query": "ЕКП развёрнут в продуктиве",
                }}
            elif last["tool"] == "search_index":
                hit = next(h for h in result["results"] if h["source_path"].endswith("journal.md"))
                decision = {"tool": "read_document", "args": {
                    "document_id": hit["document_id"], "index_id": result["index_id"],
                }}
            else:
                assert last["tool"] == "read_document"
                releases = re.findall(r"(\d{4}-\d\d-\d\d) — ЕКП ([\d.]+), развёрнут",
                                      result["content"])
                if second and not frame.get("correction"):
                    assert payload["max_tokens"] == 2048
                    return native_reply({"role": "assistant", "content": "обрывается"}, "length")
                if second:
                    assert payload["max_tokens"] == 4096
                date, version = sorted(releases)[-2 if second else -1]
                citation = result["source_path"]
                decision = {"answer": f"ЕКП {version} выпущен {date}. Источник — {citation}"}
        return native_reply({"role": "assistant", "content": json.dumps(decision)}, "stop")

    _corporate_transport(monkeypatch, provider)
    chat = str(uuid4())
    async with browser((service, settings)) as client:
        response = await client.post("/agents/api/chat", headers=READER,
                                     json={"chat": chat, "question": question})
        response.raise_for_status()
        events = [json.loads(line) for line in response.text.splitlines()]
        history = (await client.get("/agents/api/chats/" + chat, headers=READER)).json()
        if reject_plain:
            assert len(calls) == 1 and events[-1]["event"] == "error"
            assert history["messages"] == [] and "PRIVATE_VALIDATION" not in response.text
            return
        assert len(calls) == 4 and events[-1]["event"] == "answer", events
        assert [e["text"] for e in events if e["event"] == "tool_start"] == [
            "list_indexes", "search_index", "read_document",
        ]
        assert "4.2 выпущен 2026-10-14" in events[-1]["text"] and "journal.md" in events[-1]["text"]
        assert history["messages"][-1]["text"] == events[-1]["text"]

        continued = await client.post("/agents/api/chat", headers=READER,
                                      json={"chat": chat, "question": FOLLOWUP})
        continued.raise_for_status()
        events = [json.loads(line) for line in continued.text.splitlines()]
        assert calls == [None] * 10
        assert [e["text"] for e in events if e["event"] == "tool_start"] == [
            "list_indexes", "search_index", "read_document",
        ]
        assert events[-1]["event"] == "answer" and "4.1 выпущен 2026-08-21" in events[-1]["text"]
        assert private not in continued.text and "обрывается" not in continued.text
        history = (await client.get("/agents/api/chats/" + chat, headers=READER)).json()
        assert history["messages"][-1]["text"] == events[-1]["text"]


async def test_ambiguous_followup_can_ask_for_a_subject_without_running_tools(
    settings_factory, monkeypatch,
):
    portal = _corporate_portal(settings_factory)
    clarification = "Какую систему вы имеете в виду?"
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        assert not payload.get("functions")
        frame = json.loads(payload["messages"][-1]["content"])
        assert not frame.get("recent_conversation") and not frame["observations"]
        calls.append(frame["question"])
        content = json.dumps({"clarify": clarification}, ensure_ascii=False)
        return native_reply({"role": "assistant", "content": content}, "stop")

    _corporate_transport(monkeypatch, provider)
    async with browser(portal) as client:
        response = await client.post("/agents/api/chat", headers=READER,
                                     json={"chat": str(uuid4()), "question": FOLLOWUP})
    response.raise_for_status()
    events = [json.loads(line) for line in response.text.splitlines()]
    assert calls == [FOLLOWUP] and "tool_start" not in {e["event"] for e in events}
    assert events[-1]["event"] == "answer" and events[-1]["text"] == clarification


@tool
async def search_index(query: str) -> str:
    """Search the current documents."""
    pytest.fail("A model decision must go through the graph before executing")


@pytest.mark.parametrize(("content", "reason"), [
    ('{"answer":"invented index"}', "missing_evidence"), ('not valid JSON', "format"),
    ('{"tool":"arbitrary","args":{}}', "unknown_tool"),
    ('{"tool":"search_index","args":{"query":123}}', "arguments"),
    ('{"tool":"search_index","args":{"query":"x","private":true}}', "arguments"),
    ('{"clarify":"arbitrary statement"}', "format"), ('{"defer":1}', "format"),
])
async def test_unusable_decision_raises_only_a_safe_correction_code(content, reason):
    question = HumanMessage("Find a document")
    request = ModelRequest(model=Model(respond=lambda *_: AIMessage(content=content)),
                           messages=[question], tools=[search_index])
    with pytest.raises(ReadDecisionError) as error:
        await read_recovery_step(request, question)
    assert str(error.value) == error.value.reason == reason


async def test_ordinary_conversation_can_still_defer_to_the_normal_agent():
    question = HumanMessage("Tell me about yourself")
    request = ModelRequest(model=Model(respond=lambda *_: AIMessage(content='{"defer":true}')),
                           messages=[question], tools=[search_index])
    assert await read_recovery_step(request, question) is None


async def test_answer_with_separate_citation_uses_observed_source_metadata():
    question = HumanMessage("Which record was approved?")
    call_id = READ_CALL_PREFIX + "lookup"
    action = AIMessage(content="", response_metadata={"portal_read_recovery": True},
                       tool_calls=[{"id": call_id, "name": "search_index", "args": {"query": "Q"}}])
    source = {"results": [{"source_path": "ops-log.md", "excerpt": "Q-81 was approved."}]}
    observation = ToolMessage(content=json.dumps(source), tool_call_id=call_id, name="search_index")
    content = '{"answer":"Q-81","source":"https://fabricated.invalid/"}'
    request = ModelRequest(model=Model(respond=lambda *_: AIMessage(content=content)),
                           messages=[question, action, observation], tools=[search_index])
    result = await read_recovery_step(request, question)
    assert "Q-81" in result.text and "ops-log.md" in result.text
    assert "fabricated" not in result.text


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
    with pytest.raises(ReadDecisionError, match="missing_evidence"):
        await read_recovery_step(request, question)
    assert in_read_recovery([question, call, result], 0)
    assert not in_read_recovery([question, call.model_copy(update={"response_metadata": {}}),
                                 result], 0)
    assert not in_read_recovery([question, call, result, AIMessage(content="", tool_calls=[
        {"id": "native", "name": "search_index", "args": {"query": "new"}},
    ]), ToolMessage(content="other", name="search_index", tool_call_id="native")], 0)


async def test_compact_followup_limits_public_history_and_detects_a_repeated_read():
    history = [HumanMessage("obsolete")]
    for i in range(3):
        history += [HumanMessage(f"Question {i}"), AIMessage("a" * 3000 + str(i))]
    call_id = READ_CALL_PREFIX + "private_read"
    call = AIMessage(content="private intermediate reasoning",
                     response_metadata={"portal_read_recovery": True}, tool_calls=[
                         {"id": call_id, "name": "search_index", "args": {"query": "project"}},
                     ])
    history.extend([call, ToolMessage("private old result", tool_call_id=call_id),
                    HumanMessage(content="private summary",
                                 additional_kwargs={"lc_source": "summarization"})])
    question = HumanMessage("And this one?")
    current_result = ToolMessage("current result", tool_call_id=call_id)

    def repeat(messages, tools):
        frame = json.loads(messages[-1].text)
        recent = frame["recent_conversation"]
        assert len(recent) == 6 and all(len(item["text"]) <= 800 for item in recent)
        assert recent[0] == {"role": "user", "text": "Question 0"}
        assert recent[-1]["role"] == "assistant" and recent[-1]["text"].endswith("2")
        assert "private" not in json.dumps(recent) and "obsolete" not in json.dumps(recent)
        assert [obs["result"] for obs in frame["observations"]] == ["current result"]
        return AIMessage(content='{"tool":"search_index","args":{"query":"project"}}')

    request = ModelRequest(model=Model(respond=repeat), tools=[search_index],
                           messages=[*history, question, call, current_result])
    with pytest.raises(ReadDecisionError, match="duplicate"):
        await read_recovery_step(request, question)
