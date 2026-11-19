from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("jsonschema")

from jsonschema.exceptions import ValidationError
from langchain_core.caches import InMemoryCache
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from corporate_kb.agents.gigacode_model import GigaCodeChatModel
from corporate_kb.gigacode_runner import (
    CancellationSignal,
    GigaCodeCancelled,
    GigaCodeJsonResult,
    GigaCodeModelProfile,
)


@tool
def lookup_service(service: str) -> str:
    """Read the owner and current revision of a named service."""
    return f"{service}: owner=payments-team, revision=abc123"


@tool
def lookup_issue(issue_key: str) -> str:
    """Read the current status of an issue."""
    return f"{issue_key}: In Progress"


def _request(prompt: str) -> dict[str, Any]:
    """Find the request object without depending on the surrounding instructions."""
    decoder = json.JSONDecoder()
    for position, character in enumerate(prompt):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(prompt[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"messages", "tools"} <= value.keys():
            return value
    raise AssertionError("The runner did not receive a structured chat request")


def _result(payload: dict[str, Any]) -> GigaCodeJsonResult:
    return GigaCodeJsonResult(
        payload=payload,
        analyzed_files=(),
        session_id="fake-chat-session",
        model="fake-gigacode",
        duration_ms=12,
        usage={"input_tokens": 30, "output_tokens": 10},
    )


def _call(name: str = "lookup_service", **args: Any) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"name": name, "args": args}]}


class FakeRunner:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def run_model_json(self, *, prompt: str, **kwargs: Any) -> GigaCodeJsonResult:
        with self.lock:
            self.requests.append(_request(prompt))
            assert self.responses, "Unexpected additional model invocation"
            return _result(self.responses.popleft())


def _model(runner: Any, **kwargs: Any) -> GigaCodeChatModel:
    return GigaCodeChatModel(
        runner=runner,
        launch_profile=GigaCodeModelProfile(
            name="test-isolated",
            cli_version="0.99.0-test",
            arguments=("--test-model-only",),
            isolation_notes="Fake runner: no CLI, network, or built-in tools are used.",
        ),
        **kwargs,
    )


def test_plain_chat_does_not_expose_tools_and_keeps_runner_metadata() -> None:
    runner = FakeRunner({"content": "Сервис принимает платежи.", "tool_calls": []})
    response = _model(runner).invoke("Что делает сервис?")

    assert isinstance(response, AIMessage)
    assert response.content == "Сервис принимает платежи."
    assert response.tool_calls == []
    assert runner.requests[0]["tools"] == []
    assert response.response_metadata["gigacode"]["session_id"] == "fake-chat-session"


class ServiceFacts(BaseModel):
    """A service summary supported by a positive number of evidence sources."""

    service: str
    owner: str
    source_count: int = Field(ge=1)


def test_structured_output_returns_an_actual_pydantic_instance() -> None:
    facts = {"service": "payments", "owner": "payments-team", "source_count": 2}
    runner = FakeRunner(_call("ServiceFacts", **facts))
    structured = _model(runner).with_structured_output(ServiceFacts, method="function_calling")

    result = structured.invoke("Summarize the verified facts about payments")

    assert isinstance(result, ServiceFacts)
    assert result.model_dump() == facts
    function = runner.requests[0]["tools"][0]["function"]
    assert function["name"] == "ServiceFacts"
    assert function["parameters"]["properties"]["source_count"]["minimum"] == 1
    assert runner.requests[0]["tool_choice"] in {"any", "required", "ServiceFacts"}


def test_structured_output_include_raw_keeps_parsed_value_and_original_message() -> None:
    facts = {"service": "limits", "owner": "risk-team", "source_count": 3}
    runner = FakeRunner(_call("ServiceFacts", **facts))
    structured = _model(runner).with_structured_output(ServiceFacts, include_raw=True)

    result = structured.invoke("Summarize the verified facts about limits")

    assert isinstance(result["parsed"], ServiceFacts)
    assert result["parsed"].model_dump() == facts
    assert result["parsing_error"] is None
    raw = result["raw"]
    assert isinstance(raw, AIMessage)
    assert raw.tool_calls[0]["name"] == "ServiceFacts"
    assert raw.tool_calls[0]["args"] == facts
    assert raw.tool_calls[0]["id"]
    assert raw.response_metadata["gigacode"]["session_id"] == "fake-chat-session"


def test_structured_output_rejects_unsupported_method_before_running() -> None:
    runner = FakeRunner()

    with pytest.raises(ValueError, match="function_calling"):
        _model(runner).with_structured_output(ServiceFacts, method="json_schema")

    assert runner.requests == []


@pytest.mark.parametrize("name", ["none", "auto", "any", "required"])
def test_tool_binding_rejects_names_reserved_for_tool_choice_modes(name: str) -> None:
    runner = FakeRunner()
    definition = {
        "type": "function",
        "function": {
            "name": name,
            "description": "A source lookup with an ambiguous reserved name.",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    with pytest.raises(ValueError, match="reserved tool_choice modes"):
        _model(runner).bind_tools([definition])

    assert runner.requests == []


def test_launcher_configuration_is_excluded_from_repr_and_trace_serialization() -> None:
    profile = GigaCodeModelProfile(
        name="verified-test-launcher",
        cli_version="test-version",
        arguments=("--private-config", "SYNTHETIC_PRIVATE_LAUNCH_ARGUMENT"),
        isolation_notes="SYNTHETIC_PRIVATE_DEPLOYMENT_NOTES",
    )
    model = GigaCodeChatModel(runner=FakeRunner(), launch_profile=profile)

    for representation in (
        repr(model),
        json.dumps(model._serialized),
        json.dumps(model.model_dump()),
    ):
        assert "SYNTHETIC_PRIVATE_LAUNCH_ARGUMENT" not in representation
        assert "SYNTHETIC_PRIVATE_DEPLOYMENT_NOTES" not in representation
        assert "GigaCodeModelProfile" not in representation


def test_global_model_cache_cannot_skip_runner_or_cross_launcher_instances() -> None:
    first_runner = FakeRunner(
        {"content": "First invocation", "tool_calls": []},
        {"content": "Fresh invocation", "tool_calls": []},
    )
    other_runner = FakeRunner({"content": "Other deployment", "tool_calls": []})
    first_model = _model(first_runner)
    other_model = _model(other_runner)
    previous_cache = get_llm_cache()
    set_llm_cache(InMemoryCache())
    try:
        assert first_model.invoke("Same question").content == "First invocation"
        assert other_model.invoke("Same question").content == "Other deployment"
        assert first_model.invoke("Same question").content == "Fresh invocation"
    finally:
        set_llm_cache(previous_cache)

    assert len(first_runner.requests) == 2
    assert len(other_runner.requests) == 1
    assert first_model.cache is False


def test_explicit_model_cache_is_rejected() -> None:
    with pytest.raises(ValueError):
        _model(FakeRunner(), cache=True)


def test_tool_call_round_trip_preserves_call_id_and_tool_error_status() -> None:
    runner = FakeRunner(
        _call(service="payments"),
        {"content": "Источник недоступен; владельца подтвердить не удалось.", "tool_calls": []},
    )
    model = _model(runner).bind_tools([lookup_service])
    question = HumanMessage(content="Кто владелец payments?")

    first = model.invoke([question])
    assert len(first.tool_calls) == 1
    call = first.tool_calls[0]
    assert call["name"] == "lookup_service"
    assert call["args"] == {"service": "payments"}
    assert call["id"]

    reply = ToolMessage(
        content="Service catalog is unavailable",
        name="lookup_service",
        tool_call_id=call["id"],
        status="error",
    )
    final = model.invoke([question, first, reply])

    assert final.tool_calls == []
    serialized = runner.requests[1]["messages"]
    tool_message = next(item for item in serialized if item.get("role") == "tool")
    assert tool_message["tool_call_id"] == call["id"]
    assert tool_message["status"] == "error"
    assert tool_message["content"] == reply.content
    assistant = next(item for item in serialized if item.get("role") == "assistant")
    assert call["id"] in json.dumps(assistant)


@pytest.mark.parametrize(
    "payload",
    [
        _call("not_bound", service="payments"),
        _call(service=42),
        _call(),
        {"content": "", "tool_calls": [{"name": "lookup_service", "args": []}]},
        {"content": "", "tool_calls": [{"name": "lookup_service", "args": "{}"}]},
        {"content": None, "tool_calls": []},
        {"content": "broken", "tool_calls": "lookup_service"},
    ],
    ids=[
        "unknown-tool",
        "wrong-type",
        "missing-argument",
        "array-args",
        "string-args",
        "nontext-content",
        "nonarray-calls",
    ],
)
def test_invalid_model_output_is_rejected(payload: dict[str, Any]) -> None:
    runner = FakeRunner(payload)
    with pytest.raises((ValueError, ValidationError)):
        _model(runner).bind_tools([lookup_service]).invoke("Read payments")


def test_unbound_tool_calls_are_rejected() -> None:
    runner = FakeRunner(_call(service="payments"))
    with pytest.raises(ValueError):
        _model(runner).invoke("Read payments")


@pytest.mark.parametrize(
    "choice",
    [
        "any",
        "required",
        "lookup_service",
        {"type": "function", "function": {"name": "lookup_service"}},
    ],
)
def test_required_tool_choices_reject_an_answer_without_a_call(choice: Any) -> None:
    runner = FakeRunner({"content": "Unsupported assertion", "tool_calls": []})
    with pytest.raises(ValueError):
        _model(runner).bind_tools([lookup_service], tool_choice=choice).invoke("Read payments")


@pytest.mark.parametrize(
    "choice",
    [
        "auto",
        "any",
        "required",
        "lookup_service",
        {"type": "function", "function": {"name": "lookup_service"}},
    ],
)
def test_supported_tool_choices_accept_the_selected_call(choice: Any) -> None:
    runner = FakeRunner(_call(service="payments"))
    response = (
        _model(runner).bind_tools([lookup_service], tool_choice=choice).invoke("Read payments")
    )
    assert response.tool_calls[0]["name"] == "lookup_service"


def test_tool_choice_none_forbids_calls() -> None:
    runner = FakeRunner(_call(service="payments"))
    with pytest.raises(ValueError):
        _model(runner).bind_tools([lookup_service], tool_choice="none").invoke("Answer directly")


def test_named_tool_choice_rejects_a_different_bound_tool() -> None:
    runner = FakeRunner(_call("lookup_issue", issue_key="PAY-123"))
    model = _model(runner).bind_tools([lookup_service, lookup_issue], tool_choice="lookup_service")
    with pytest.raises(ValueError):
        model.invoke("Read payments")


@pytest.mark.parametrize("parallel", [True, False])
def test_parallel_tool_limit_is_enforced(parallel: bool) -> None:
    payload = {
        "content": "",
        "tool_calls": [
            {"name": "lookup_service", "args": {"service": "payments"}},
            {"name": "lookup_service", "args": {"service": "limits"}},
        ],
    }
    runner = FakeRunner(payload)
    # Either the model's hard limit or the caller's no-parallel policy must reject two calls.
    model = _model(runner, max_tool_calls=1 if parallel else 16).bind_tools(
        [lookup_service],
        parallel_tool_calls=parallel,
    )
    with pytest.raises((ValueError, ValidationError)):
        model.invoke("Read both services")


@pytest.mark.parametrize("kwargs", [{"stop": ["END"]}, {"temperature": 0.2}])
def test_unsupported_generation_options_fail_before_running(kwargs: dict[str, Any]) -> None:
    runner = FakeRunner()
    with pytest.raises((ValueError, NotImplementedError)):
        _model(runner).invoke("Describe payments", **kwargs)
    assert runner.requests == []


def test_multimodal_input_is_rejected_before_running() -> None:
    runner = FakeRunner()
    message = HumanMessage(
        content=[{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}]
    )
    with pytest.raises((ValueError, NotImplementedError)):
        _model(runner).invoke([message])
    assert runner.requests == []


def test_input_budget_is_enforced_before_running() -> None:
    runner = FakeRunner()
    with pytest.raises(ValueError):
        _model(runner, max_input_chars=1000).invoke("x" * 2000)
    assert runner.requests == []


def test_concurrent_bindings_do_not_share_tool_definitions() -> None:
    runner = FakeRunner(*({"content": "ok", "tool_calls": []} for _ in range(3)))
    model = _model(runner)
    service_model = model.bind_tools([lookup_service])
    issue_model = model.bind_tools([lookup_issue])

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(bound.invoke, "Check state")
            for bound in (service_model, issue_model, model)
        ]
        assert all(future.result(timeout=5).content == "ok" for future in futures)

    names = [
        frozenset(item["function"]["name"] for item in request["tools"])
        for request in runner.requests
    ]
    assert set(names) == {frozenset(), frozenset({"lookup_service"}), frozenset({"lookup_issue"})}


@pytest.mark.asyncio
async def test_async_invoke_returns_the_model_answer() -> None:
    runner = FakeRunner({"content": "Готово", "tool_calls": []})
    result = await _model(runner).ainvoke("Опиши систему")
    assert result.content == "Готово"


@pytest.mark.asyncio
async def test_async_cancellation_waits_for_runner_shutdown() -> None:
    started = threading.Event()
    cancel_seen = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    class BlockingRunner:
        def run_model_json(
            self, *, cancel: CancellationSignal, **kwargs: Any
        ) -> GigaCodeJsonResult:
            started.set()
            try:
                deadline = time.monotonic() + 5
                while not cancel.is_set():
                    if time.monotonic() >= deadline:
                        raise AssertionError("The adapter did not propagate cancellation")
                    time.sleep(0.005)
                cancel_seen.set()
                if not release.wait(timeout=5):
                    raise AssertionError("The test did not release runner shutdown")
                raise GigaCodeCancelled("Fake runner stopped")
            finally:
                stopped.set()

    task = asyncio.create_task(_model(BlockingRunner()).ainvoke("Wait for cancellation"))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        assert await asyncio.to_thread(cancel_seen.wait, 3)
        assert not task.done(), "Cancellation returned before the runner stopped"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert stopped.is_set()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_langgraph_executes_an_external_tool_and_returns_to_model() -> None:
    pytest.importorskip("langgraph")
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    runner = FakeRunner(
        _call(service="payments"), {"content": "Owner: payments-team", "tool_calls": []}
    )
    model = _model(runner).bind_tools([lookup_service])
    graph = StateGraph(MessagesState)
    graph.add_node("agent", lambda state: {"messages": [model.invoke(state["messages"])]})
    graph.add_node("tools", ToolNode([lookup_service]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    result = graph.compile().invoke({"messages": [HumanMessage(content="Who owns payments?")]})

    assert result["messages"][-1].content == "Owner: payments-team"
    tool_result = next(
        message for message in result["messages"] if isinstance(message, ToolMessage)
    )
    assert "revision=abc123" in tool_result.content
    assert len(runner.requests) == 2


def test_deepagents_accepts_the_model_and_calls_an_external_tool() -> None:
    deepagents = pytest.importorskip("deepagents")
    runner = FakeRunner(
        _call(service="payments"), {"content": "Owner: payments-team", "tool_calls": []}
    )
    agent = deepagents.create_deep_agent(
        model=_model(runner),
        tools=[lookup_service],
        system_prompt="Answer a service-owner question using lookup_service.",
    )

    result = agent.invoke(
        {"messages": [HumanMessage(content="Who owns payments?")]},
        config={"recursion_limit": 12},
    )

    assert result["messages"][-1].content == "Owner: payments-team"
    assert any(
        isinstance(message, ToolMessage) and "revision=abc123" in message.content
        for message in result["messages"]
    )
    assert len(runner.requests) == 2
