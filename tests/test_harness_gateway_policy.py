"""Corporate API policy signals, neutral diagnostics and durable run outcomes."""

# ruff: noqa: RUF001  # Russian provider responses.

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_harness_goal_research import OWNER, Source, finished

from agent_harness.config import HarnessSettings
from agent_harness.contracts import RunRequest
from agent_harness.model_connection import ModelConnection, ModelConnectionError, ModelRouter
from agent_harness.runtime import HarnessRuntime

REFUSAL = "PRIVATE_PROVIDER_RESPONSE"
REQUEST_ID = "6dbedfc8-6d39-4c3a-acab-ad4e7bb58131"
CALL = {"function": {"name": "read_document", "arguments": "{unfinished PRIVATE"}}


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
    (200, {"choices": [{"message": {"content": "У меня нет личного мнения."}}]}, "text"),
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
        assert REQUEST_ID in result["answer"] and "Выбор следующего действия" in result["answer"]
        assert not result["resumable"] and REFUSAL not in json.dumps(result)
        with pytest.raises(ValueError):
            await runtime.resume(OWNER, run["id"])
        learned = runtime.memory.retrieve(OWNER.owner_id, "Explain payments", {"a"}, {"a"})
        assert all(REFUSAL not in item["text"] and item["kind"] != "case" for item in learned)
    finally:
        await runtime.stop()


@pytest.mark.parametrize(("replies", "ok", "diagnostic"), [
    (["Unrelated boilerplate"], False, "OK"),
    (["OK", "blocked"], False, "blacklist"),
    (["OK", "I will read the sample."], False, "tool calling"),
    (["OK", "valid_call"], True, "инструмент"),
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
        else:
            assert [tool["function"]["name"] for tool in payload["tools"]] == ["read_sample"]
            if answer == "valid_call":
                message = {"tool_calls": [{"function": {"name": "read_sample",
                           "arguments": json.dumps({"id": "sample-1"})}}]}
        reason = "blacklist" if answer == "blocked" else "stop"
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
