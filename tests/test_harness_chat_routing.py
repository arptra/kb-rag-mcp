"""Exercise automatic turn routing through HTTP, including the actual model adapter."""

# ruff: noqa: RUF001 -- Russian user messages exercise the production chat path.
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from test_harness_api_recovery import client_for, finish

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence
from agent_harness.control.settings import ControlStore
from agent_harness.graph import HarnessGraph


async def send(client: httpx.AsyncClient, question: str, **extra: Any) -> dict[str, Any]:
    response = await client.post("/api/runs", json={
        "question": question, "mode": "quick", "routing": "auto", "index_ids": ["a"], **extra,
    })
    assert response.status_code == 202, response.text
    return await finish(client, "/api/runs/" + response.json()["id"])


def completion(message: dict[str, Any], reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": reason}]})


def decision(route: str, answer: str = "") -> httpx.Response:
    return completion({"tool_calls": [{"type": "function", "function": {
        "name": "RouteTurn", "arguments": json.dumps({"route": route, "answer": answer}),
    }}]}, "tool_calls")


@pytest.mark.parametrize("first_reply", ["json", "arguments", "length", "schema"])
async def test_route_repairs_only_invalid_structure_then_answers_without_retrieval(
    tmp_path: Path, first_reply: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []
    question = "Почему лёд легче воды?"
    answer = "В твёрдом состоянии молекулы воды располагаются дальше друг от друга."

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return decision("chat", answer)
        if first_reply == "json":
            return completion({"content": '{"route":"damaged-output"'})
        if first_reply == "schema":
            return completion({"content": '{"unknown":"damaged-output"}'})
        return completion({"tool_calls": [{"type": "function", "function": {
            "name": "RouteTurn", "arguments": '{"route":"damaged-output"',
        }}]}, "length" if first_reply == "length" else "tool_calls")

    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        monkeypatch.setattr(source, "indexes", catalog)
        run = await send(client, question, mode="quick" if first_reply == "json" else "autonomous")
        assert (run["status"], run["answer"]) == ("completed", answer)
        assert run["metrics"]["model_calls"] == len(requests) == 2
        assert [r["messages"][-1] for r in requests] == [
            {"role": "user", "content": json.dumps({"message": question}, ensure_ascii=False)},
        ] * 2
        assert all([t["function"]["name"] for t in r["tools"]] == ["RouteTurn"] for r in requests)
        assert requests[0]["messages"][0] != requests[1]["messages"][0]
        assert "damaged-output" not in str(requests[1]) and not source.queries
        catalog.assert_not_awaited()


@pytest.mark.parametrize("constraint", ["calls", "time"])
async def test_invalid_route_respects_research_and_answer_reserves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constraint: str,
) -> None:
    calls = 0

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return completion({"content": '{"route":'})

    async with client_for(tmp_path, gateway) as (client, source):
        if constraint == "calls":
            store = ControlStore(HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path))
            controls = store.load()
            for profile in controls.profiles["quick"].values():
                profile.model_calls = 3
            store.save(controls, expected_revision=controls.revision)
        else:
            monkeypatch.setattr(HarnessGraph, "_reserve_deadline", lambda *_: 0)
        run = await send(client, "Почему лёд легче воды?")
        assert calls == 1 and run["stop_reason"] == "routing_unavailable"
        assert run["answer"].endswith("?") and not source.queries


@pytest.mark.parametrize(("reply", "reason", "stop"), [
    ({"content": "", "refusal": "refused"}, "stop", "model_policy_stop"),
    ({"content": ""}, "content_filter", "model_policy_stop"),
    ({"content": "У меня нет личного мнения."}, "stop", "routing_unavailable"),
    ({"content": "Сожалею, этой информацией я не располагаю."}, "stop", "routing_unavailable"),
])
async def test_source_free_routing_does_not_repair_provider_notices(
    tmp_path: Path, reply: dict[str, Any], reason: str, stop: str,
) -> None:
    count = 0

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return completion(reply, reason)

    async with client_for(tmp_path, gateway) as (client, source):
        run = await send(client, "Почему лёд легче воды?", mode="autonomous")
        assert count == 1 and run["stop_reason"] == stop and not source.queries


async def test_transient_failure_retries_only_the_route_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools: list[list[str]] = []
    answer = "Вода расширяется при замерзании."

    def gateway(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tools.append([tool["function"]["name"] for tool in body["tools"]])
        return httpx.Response(503) if len(tools) == 1 else decision("chat", answer)

    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        monkeypatch.setattr(source, "indexes", catalog)
        run = await send(client, "Почему льду требуется больше места?")
        assert (run["status"], run["answer"]) == ("completed", answer)
        assert run["metrics"]["transport_retries"] == 1 and run["metrics"]["model_calls"] == 2
        assert tools == [["RouteTurn"], ["RouteTurn"]]
        assert not source.queries
        catalog.assert_not_awaited()


async def test_restart_during_a_route_decision_reconnects_to_the_same_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    requests: list[dict[str, Any]] = []
    answer = "Снег рассеивает видимый свет."

    class StalledResponse(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            entered.set()
            await asyncio.Event().wait()  # The service shuts down during this request.
            yield b""

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return (httpx.Response(200, stream=StalledResponse()) if len(requests) == 1
                else decision("chat", answer))

    question = "Почему снег белый?"
    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        monkeypatch.setattr(source, "indexes", catalog)
        response = await client.post("/api/runs", json={
            "question": question, "mode": "autonomous", "routing": "auto", "index_ids": ["a"],
        })
        assert response.status_code == 202
        path = "/api/runs/" + response.json()["id"]
        await asyncio.wait_for(entered.wait(), 3)
        assert (await client.get(path)).json()["phase"] == "routing"
        assert not source.queries
        catalog.assert_not_awaited()
    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        monkeypatch.setattr(source, "indexes", catalog)
        run = await finish(client, path)
        assert run["status"] == "completed" and run["answer"] == answer, run
        assert run["resume_count"] == 1
        assert len(requests) == 2
        for body in requests:
            assert [tool["function"]["name"] for tool in body["tools"]] == ["RouteTurn"]
            assert json.loads(body["messages"][-1]["content"]) == {"message": question}
        assert not source.queries
        catalog.assert_not_awaited()


@pytest.mark.parametrize("mode", ["quick", "autonomous"])
async def test_open_question_answers_in_one_model_call_without_source_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    question = "Почему лёд плавает?"
    answer = "При замерзании плотность воды уменьшается, поэтому лёд плавает."
    calls: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return decision("chat", answer)

    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        read = AsyncMock(wraps=source.read)
        monkeypatch.setattr(source, "indexes", catalog)
        monkeypatch.setattr(source, "read", read)
        run = await send(client, question, mode=mode)
        assert run["status"] == "completed" and run["answer_kind"] == "conversation"
        assert run["answer"] == answer
        assert len(calls) == run["metrics"]["model_calls"] == 1
        assert [tool["function"]["name"] for tool in calls[0]["tools"]] == ["RouteTurn"]
        assert json.loads(calls[0]["messages"][-1]["content"]) == {"message": question}
        assert run["requested_index_ids"] == ["a"] and run["index_ids"] == []
        assert not source.queries and not run["evidence"] and not run["memory_used"]
        catalog.assert_not_awaited()
        read.assert_not_awaited()


@pytest.mark.parametrize(("content", "reason"), [
    ('{"route":"research"', "stop"),
    ('{"route":"research"}', "length"),
    ('{"route":"typo"}', "stop"),
    ('{"route":"chat","answer":""}', "stop"),
])
async def test_invalid_model_route_asks_for_clarification_without_retrieval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, reason: str,
) -> None:
    def gateway(_: httpx.Request) -> httpx.Response:
        return completion({"content": content}, reason)

    async with client_for(tmp_path, gateway) as (client, source):
        catalog = AsyncMock(wraps=source.indexes)
        monkeypatch.setattr(source, "indexes", catalog)
        run = await send(client, "Что ты об этом думаешь?")
        assert run["status"] == "partial" and run["stop_reason"] == "routing_unavailable"
        assert run["answer"].endswith("?") and run["answer"] != content
        assert not source.queries and not run["evidence"] and run["metrics"]["tool_calls"] == 0
        catalog.assert_not_awaited()


async def test_greeting_and_thanks_do_not_interrupt_research_or_leak_its_documents_to_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    greeting = "Привет, как дела?"
    issue = "Привет! На каком этапе PAY-42?"
    why = "А почему?"
    private_fact = "Ожидает решения внутренней комиссии по расчётам."
    route_contexts: list[dict[str, Any]] = []
    writer_contexts: list[str] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        names = [tool["function"]["name"] for tool in body.get("tools", [])]
        content = body["messages"][-1]["content"]
        if names == ["RouteTurn"]:
            route_contexts.append(json.loads(content))
            return decision("research")
        writer_contexts.append(content)
        return completion({"content": private_fact + " [entry]"})

    async with client_for(tmp_path, gateway) as (client, source):
        async def issue_search(**kwargs: Any) -> list[Evidence]:
            source.queries.append(kwargs["query"])
            return [Evidence(id="entry", index_id="a", title="PAY-42", text=private_fact)]

        monkeypatch.setattr(source, "search", issue_search)
        hello = await send(client, greeting)
        assert hello["answer_kind"] == "conversation" and not route_contexts and not source.queries
        research = await send(client, issue, parent_run_id=hello["id"])
        assert research["status"] == "completed", research["warnings"]
        assert research["answer"] == private_fact + " [entry]"
        assert research["evidence"][0]["text"] == private_fact
        assert route_contexts[0]["message"] == issue
        assert route_contexts[0]["previous_question"] == greeting
        assert source.queries and private_fact in writer_contexts[-1]
        searched, model_calls = len(source.queries), len(route_contexts) + len(writer_contexts)
        thanks = await send(client, "спасибо", parent_run_id=research["id"])
        assert thanks["answer_kind"] == "conversation" and thanks["metrics"]["model_calls"] == 0
        assert len(source.queries) == searched
        assert len(route_contexts) + len(writer_contexts) == model_calls
        followup = await send(client, why, parent_run_id=thanks["id"])
        assert followup["status"] == "completed", followup["warnings"]
        assert followup["conversation_id"] == hello["conversation_id"]
        assert route_contexts[-1] == {"message": why, "previous_turn": "research"}
        assert len(source.queries) > searched and private_fact in writer_contexts[-1]
