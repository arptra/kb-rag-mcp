"""Check transport policy signals and ordinary unusable completions independently."""
# ruff: noqa: RUF001 -- realistic Russian replies.

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from agent_harness.contracts import ModelDecisionError
from agent_harness.model_connection import (
    ModelConnection,
    ModelContextTooLarge,
    ModelPolicyStop,
    ModelRouter,
    ModelTransientError,
)


async def complete(tmp_path: Path, payload: dict[str, Any], *, status: int = 200) -> dict[str, Any]:
    router = ModelRouter(tmp_path, None)
    router.store.save(ModelConnection(provider="openai", base_url="https://gateway.test"))
    router.transport = httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    return await router.decide(messages=[{"role": "user", "content": "Проверь настройки"}],
                               tools=[], cancel=Event(), max_input_chars=2000)


def completion(message: dict[str, Any], reason: str = "stop") -> dict[str, Any]:
    return {"choices": [{"finish_reason": reason, "message": message}]}


async def test_opinion_notice_is_an_unusable_draft_on_success_only(tmp_path: Path) -> None:
    reply = completion({"content": "У меня нет личного мнения."})
    with pytest.raises(ModelDecisionError):
        await complete(tmp_path, reply)
    with pytest.raises(ModelTransientError):
        await complete(tmp_path, reply, status=503)
    truncated = await complete(tmp_path, completion(reply["choices"][0]["message"], "length"))
    assert truncated["finish_reason"] == "length"


async def test_runner_opinion_and_explicit_denial_have_distinct_outcomes(tmp_path: Path) -> None:
    class Runner:
        answer = "У меня нет личного мнения."

        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            return {"content": self.answer, "tool_calls": []}

    runner = Runner()
    router = ModelRouter(tmp_path, runner)
    with pytest.raises(ModelDecisionError):
        await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=2000)
    runner.answer = "Я не могу ответить на этот вопрос из-за корпоративных правил."
    with pytest.raises(ModelPolicyStop):
        await router.decide(messages=[], tools=[], cancel=Event(), max_input_chars=2000)


async def test_provider_fields_inside_an_answer_remain_source_text(tmp_path: Path) -> None:
    parts = ["Формат события blacklist в документации: ",
             json.dumps({"finish_reason": "content_filter", "refusal": True})]
    reply = completion({"content": [{"type": "text", "text": t} for t in parts], "refusal": "  "})
    assert (await complete(tmp_path, reply))["content"] == "".join(parts)


async def test_direct_gateway_error_code_precedes_http_retry(tmp_path: Path) -> None:
    with pytest.raises(ModelPolicyStop) as stopped:
        await complete(tmp_path, {"error": "blacklist"}, status=429)
    assert stopped.value.field == "error" and stopped.value.http_status == 429
    with pytest.raises(ModelTransientError):
        await complete(tmp_path, {"error": "The blacklist service is temporarily busy"}, status=429)


@pytest.mark.parametrize(("wire", "expected"), [
    (completion({"content": "Я не могу ответить на ваш вопрос из-за корпоративных правил."}),
     ModelPolicyStop),
    (completion({"content": "У меня нет личного мнения. "
                            "Я не могу ответить на этот вопрос из-за корпоративных правил."}),
     ModelPolicyStop),
    (completion({"content": [{"type": "text", "text": "Partial"},
                             {"type": "refusal", "refusal": "private"}]}, "length"),
     ModelPolicyStop),
    ({"choices": [{"error": {"code": "blacklist"}, "message": {"content": "discard"}}]},
     ModelPolicyStop),
    ({"error": {"code": "server_error"}, "choices": [{"finish_reason": "content_filter"}]},
     ModelPolicyStop),
    ({"error": {"code": "server_error"}}, ModelTransientError),
    ({"error": {"code": "context_length_exceeded"}}, ModelContextTooLarge),
])
async def test_typed_upstream_outcomes_win_over_incomplete_results(
    tmp_path: Path, wire: dict[str, Any], expected: type[Exception],
) -> None:
    with pytest.raises(expected):
        await complete(tmp_path, wire)


async def test_temporary_cli_provider_failure_recovers_without_activating_again(
    settings_factory: Any, tmp_path: Path,
) -> None:
    from test_agent_activation import wait_state
    from test_gigacode_runner_lifecycle import launcher
    from test_harness_goal_research import OWNER, Source, finished

    from agent_harness.config import HarnessSettings
    from agent_harness.contracts import RunRequest
    from agent_harness.runtime import HarnessRuntime
    from corporate_kb.agents.activation import GigaCodeAgentController
    from corporate_kb.harness_integration import GigaCodeDecisionModel

    cli = launcher(tmp_path, '''
prompt = sys.stdin.read()
journal = Path(__file__).with_name("calls")
calls = journal.read_text().splitlines() if journal.exists() else []
phase = "activate" if "This is a readiness check" in prompt else "decide"
journal.write_text("\\n".join([*calls, phase]))
if phase == "activate":
    result = {"ready": True}
elif "decide" not in calls:
    print(json.dumps({"type": "stream_event", "event": {"type": "error",
                      "error": {"type": "overloaded_error"}}}), flush=True)
    raise SystemExit(1)
else:
    result = {"content": "Requests arrive through payment-service [entry].", "tool_calls": []}
print(json.dumps({"type": "result", "is_error": False, "result": result}), flush=True)
''')
    controller = GigaCodeAgentController(settings_factory(gigacode_command=str(cli)))
    controller.enable()
    await asyncio.to_thread(wait_state, controller, "ready")
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path / "harness"),
                             Source(), GigaCodeDecisionModel(controller, owns_controller=False))
    await runtime.start()
    try:
        for expected_calls in (2, 1):
            run = await runtime.submit(OWNER, RunRequest(question="Describe incoming requests"))
            result = await finished(runtime, run["id"])
            assert result["status"] == "completed", result["warnings"]
            assert result["answer"] == "Requests arrive through payment-service [entry]."
            assert result["metrics"]["model_calls"] == expected_calls
            assert controller.status()["available"]
        assert (tmp_path / "calls").read_text().splitlines() == ["activate", "decide",
                                                                  "decide", "decide"]
    finally:
        await runtime.stop()
        controller.stop()
