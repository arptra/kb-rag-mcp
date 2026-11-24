"""Corporate API policy signals, neutral diagnostics and durable run outcomes."""

# Russian provider responses.

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_harness_api_recovery import client_for, finish
from test_harness_goal_research import OWNER, Source, finished

from agent_harness.config import HarnessSettings
from agent_harness.contracts import RunRequest
from agent_harness.model_connection import ModelConnection, ModelConnectionError, ModelRouter
from agent_harness.runtime import HarnessRuntime

REFUSAL = "PRIVATE_PROVIDER_RESPONSE"
REQUEST_ID = "6dbedfc8-6d39-4c3a-acab-ad4e7bb58131"
CALL = {"function": {"name": "read_document", "arguments": "{unfinished PRIVATE"}}


@pytest.mark.parametrize("outcome", [
    "assessment_restricted", "quote_is_answer", "writer_offline", "writer_restricted",
])
async def test_public_chat_preserves_reviewed_result_and_distinguishes_real_policy(
    tmp_path: Path, outcome: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_harness.contracts import Evidence

    quote = "Новые клиентские заявки поступают через единый приёмный сервис."
    calls = []

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assessment = any(t["function"]["name"] == "assess_passages"
                         for t in payload.get("tools", []))
        calls.append("review" if assessment else "write")
        if ((assessment and outcome == "assessment_restricted")
                or (not assessment and outcome == "writer_restricted")):
            return httpx.Response(200, headers={"x-request-id": REQUEST_ID}, json={
                "choices": [{"finish_reason": "blacklist", "message": {"content": REFUSAL}}],
            })
        if assessment:
            ctx = json.loads(payload["messages"][-1]["content"])
            passage = next(p["number"] for p in ctx["passages"] if p["text"] == quote)
            review = {"passage": passage, "quote": quote,
                      "quote_is_answer": outcome == "quote_is_answer"}
            return httpx.Response(200, json={"choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(review)}},
            ]})
        return httpx.Response(503)

    async def source_read(**kwargs: Any) -> Evidence:
        return Evidence(id="entry", title="Приём обращений", index_id="a", text=quote)

    async with client_for(tmp_path, gateway) as (client, source):
        monkeypatch.setattr(source, "supports_passage_retrieval", True, raising=False)
        monkeypatch.setattr(source, "read", source_read)
        created = await client.post("/api/runs", json={"question": "Как поступают новые заявки?"})
        result = await finish(client, "/api/runs/" + created.json()["id"])
    assert calls[0] == "review" and REFUSAL not in json.dumps(result)
    if outcome.endswith("restricted"):
        assert calls == (["review"] if outcome.startswith("assessment") else ["review", "write"])
        assert result["answer_kind"] == "restricted" and not result["resumable"]
        assert "blacklist" not in result["answer"] and quote not in result["answer"]
        stage = ("Проверка найденных фрагментов" if outcome.startswith("assessment")
                 else "Подготовка ответа")
        assert result["provider_diagnostic"]["stage"] == stage
        assert result["provider_diagnostic"]["request_id"] == REQUEST_ID
    else:
        assert result["answer"] == quote + " [entry]"
        assert result["answer_kind"] == "verified_excerpt"
        if outcome == "quote_is_answer":
            assert result["status"] == "completed" and calls == ["review"]
        else:
            assert result["status"] == "partial" and result["stop_reason"] == "model_transport"
            assert calls.count("write") >= 2 and calls.count("review") == 1


@pytest.mark.parametrize("status", [400, 413])
async def test_overlength_rebuilds_only_the_model_request(tmp_path: Path, status: int) -> None:
    from agent_harness.contracts import Evidence, Principal

    class ManySources(Source):
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": str(i), "name": f"Index {i}"} for i in range(12)]

        async def search(self, **args: Any) -> list[Evidence]:
            self.queries.append(args["query"])
            idx = args["index_id"]
            return [Evidence(id=idx, index_id=idx, title="Services", text="current data " * 500)]

    source = ManySources()
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(provider="openai", base_url="https://gateway.test"))
    sizes = []

    def reply(request: httpx.Request) -> httpx.Response:
        sizes.append(len(request.content))
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        assert ctx["question"] == "Explain the service routing"
        assert len(ctx["allowed_indexes"]) == 12
        if len(sizes) == 1:
            return httpx.Response(status, json={"error": {"code": "context_length_exceeded"}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "Current routes are recorded in the index [1]."}}]})

    router.transport = httpx.MockTransport(reply)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), source, router)
    await runtime.start()
    try:
        created = await runtime.submit(OWNER, RunRequest(question="Explain the service routing"))
        run = await finished(runtime, created["id"])
        assert run["status"] == "completed", run["warnings"]
        assert len(sizes) == 2 and sizes[1] < sizes[0] * .7
        assert len(source.queries) == 12 and run["metrics"]["model_calls"] == 2
        assert "context_resize" in {step["name"] for step in run["research_trace"]}
    finally:
        await runtime.stop()


@pytest.mark.parametrize(("code", "body", "signal"), [
    (200, {"choices": [{"finish_reason": "blacklist", "message": {
        "content": REFUSAL, "tool_calls": [CALL]}}]}, "blacklist"),
    (200, {"choices": [{"finish_reason": "content_filter"}]}, "content_filter"),
    (200, {"choices": [{"finish_reason": "refusal"}]}, "refusal"),
    (200, {"choices": [{"finish_reason": "stop", "message": {
        "refusal": REFUSAL, "tool_calls": [CALL]}}]}, "refusal"),
    (200, {"choices": [{"message": {"content": [{"type": "refusal", "refusal": REFUSAL}]}}]},
     "refusal"),
    (403, {"error": {"code": "content_policy_violation", "message": REFUSAL}},
     "content_policy_violation"),
    (400, {"error": {"code": "invalid_request", "innererror": {
        "code": "ResponsibleAIPolicyViolation", "message": REFUSAL}}},
     "responsibleaipolicyviolation"),
    (200, {"error": {"code": "content_filter", "message": REFUSAL}}, "content_filter"),
    (200, {"choices": [{"message": {
        "content": "Я не могу ответить на ваш вопрос из-за корпоративных правил."}}]}, "text"),
])
async def test_policy_stops_graph_once_and_keeps_private_refusal_out_of_state(
    tmp_path: Path, code: int, body: dict[str, Any], signal: str,
) -> None:
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(provider="openai", base_url="https://gateway.test/v1"))
    calls = 0

    def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        assert payload["tools"]
        # Present the goal with its evidence as one user message, including on
        # gateways which require alternating roles or only read the last message.
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        assert json.loads(payload["messages"][-1]["content"])["question"] == "Explain payments"
        return httpx.Response(code, json=body, headers={"x-request-id": REQUEST_ID})

    router.transport = httpx.MockTransport(gateway)
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path), Source(), router)
    await runtime.start()
    try:
        run = await runtime.submit(
            OWNER, RunRequest(question="Explain payments", mode="autonomous"),
        )
        result = await finished(runtime, run["id"])
        expected = "model_refusal_text" if signal == "text" else "model_policy_stop"
        assert result["stop_reason"] == expected and result["status"] == "partial", result
        assert result["metrics"]["tool_calls"] == 0
        assert result["metrics"]["model_calls"] == calls == 1
        assert result["research_trace"][-1]["name"] == expected
        assert result["answer_kind"] == "restricted" and REQUEST_ID not in result["answer"]
        expected_notice = ("правила провайдера" if signal == "text"
                           else "Корпоративный шлюз ограничил ответ")
        assert expected_notice in result["answer"]
        diagnostic = result["provider_diagnostic"]
        assert diagnostic["stage"] == "Выбор следующего действия"
        assert diagnostic["http_status"] == code and diagnostic["signal"] == signal
        assert diagnostic["field"] and diagnostic["request_id"] == REQUEST_ID
        assert diagnostic["tools_offered"] > 0 and diagnostic["estimated_input_tokens"] > 0
        assert not result["resumable"] and REFUSAL not in json.dumps(result)
        with pytest.raises(ValueError):
            await runtime.resume(OWNER, run["id"])
        learned = runtime.memory.retrieve(OWNER.owner_id, "Explain payments", {"a"}, {"a"})
        assert not learned
        assert runtime.memory.list(OWNER.owner_id, {"a"})[0]["stop_reason"] == expected
    finally:
        await runtime.stop()


@pytest.mark.parametrize(("replies", "ok", "diagnostic"), [
    (["Unrelated boilerplate"], False, "OK"),
    (["OK", "blocked"], False, "blacklist"),
    (["OK", "I will read the sample."], False, "tool calling"),
    (["OK", "valid_call", "I will read it."], False, "итогового ответа"),
    (["OK", "valid_call", "[sample-1]"], False, "точное значение sample_value из документа."),
    (["OK", "valid_call", "value_only"], False, "не вернула ссылку [sample-1]."),
    (["OK", "valid_call", "submit_value"], True, "инструмент"),
    (["OK", "valid_call", "submit_wrong_id"], False, "id документа sample-1."),
    (["OK", "valid_call", "submit_wrong_value"], False, "точное значение sample_value"),
    (["OK", "valid_call", "submit_cutoff"], False, "не завершила итоговый ответ"),
    (["OK", "valid_call", "submit_duplicate"], False, "один итоговый вызов"),
    (["OK", "valid_call", "verified_value"], True, "инструмент"),
])
async def test_gateway_check_distinguishes_text_and_tool_support(
    tmp_path: Path, replies: list[str], ok: bool, diagnostic: str,
) -> None:
    router = ModelRouter(tmp_path, None)
    requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        answer = replies[len(requests) - 1]
        message: dict[str, Any] = {"content": answer}
        if len(requests) == 1:
            assert "tools" not in payload
        elif len(requests) == 2:
            assert [tool["function"]["name"] for tool in payload["tools"]] == ["read_sample"]
            if answer == "valid_call":
                message = {"tool_calls": [{"function": {"name": "read_sample",
                           "arguments": json.dumps({"id": "sample-1"})}}]}
        else:
            assert [tool["function"]["name"] for tool in payload["tools"]] == ["submit_sample"]
            match = re.search(r"S-[0-9a-f]{8}", payload["messages"][-1]["content"])
            assert match
            if answer == "verified_value":
                message = {"content": f"{match.group()} [sample-1]"}
            elif answer == "value_only":
                message = {"content": match.group()}
            elif answer.startswith("submit_"):
                args = {"id": "other" if answer == "submit_wrong_id" else "sample-1",
                        "value": "WRONG" if answer == "submit_wrong_value" else match.group()}
                call = {"function": {"name": "submit_sample", "arguments": json.dumps(args)}}
                message = {"tool_calls": [call, call] if answer == "submit_duplicate" else [call]}
        reason = {"blocked": "blacklist", "submit_cutoff": "length"}.get(answer, "stop")
        choice = {"message": message, "finish_reason": reason}
        return httpx.Response(200, json={"choices": [choice]})

    router.transport = httpx.MockTransport(gateway)
    result = await router.probe(
        ModelConnection(provider="openai", base_url="https://gateway.test/v1"), autopilot=True,
    )
    assert result["available"] is ok and diagnostic in result["message"]
    assert len(requests) == len(replies)
    if len(replies) == 2 and not ok:
        assert "Простой запрос прошёл" in result["message"]
    assert router.store.load().provider == "gigacode"


async def test_unknown_stop_never_becomes_a_finished_answer_or_tool_action(tmp_path: Path) -> None:
    router = ModelRouter(tmp_path, None)
    router.transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"finish_reason": "unknown_vendor_stop", "message": {
            "content": "Looks complete", "tool_calls": [CALL],
        }}],
    }))
    with pytest.raises(ModelConnectionError, match="неизвестную причину"):
        await router._completion(ModelConnection(provider="openai", base_url="https://gateway.test"),
                                 [], [])
