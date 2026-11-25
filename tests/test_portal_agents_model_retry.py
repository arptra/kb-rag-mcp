import asyncio
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from test_portal_agents_engine import Model, call

from corporate_kb.gigacode_runner import GigaCodePolicyStop, GigaCodeTransientError
from portal_agents import engine
from portal_agents.model_errors import ModelGatewayError, gateway_exception


def failure(status, **headers):
    request = httpx.Request("POST", "https://model.test")
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("private upstream body", request=request, response=response)


@pytest.mark.parametrize("failures", [1, 2])
async def test_subagent_retries_only_failed_model_step(monkeypatch, failures):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    model_inputs = []
    planner_calls = []
    tools_run = []
    events = []

    @tool
    async def read_document(doc: str) -> str:
        """Return verified details."""
        tools_run.append(doc)
        return "verified " + doc

    def respond(messages, names):
        user = messages[-1]
        if "task" in names:
            if isinstance(user, HumanMessage):
                return call("task", subagent_type="general-purpose", description="Read R1")
            assert isinstance(user, ToolMessage) and "verified R1" in user.text
            return AIMessage(content="answer R1")
        if not names:
            planner_calls.append([m.text for m in messages if isinstance(m, HumanMessage)])
            return AIMessage(content="Check the document identified as R1.")
        if isinstance(user, HumanMessage):
            return call("read_document", doc="R1")
        assert isinstance(user, ToolMessage)
        model_inputs.append(user.text)
        if len(model_inputs) <= failures:
            raise failure(500)
        return AIMessage(content=user.text)

    async def report(name, text):
        events.append((name, text))

    result = await engine.run_turn(Model(respond=respond), "Investigate", [read_document],
                                   on_event=report)
    assert result == "answer R1"
    assert tools_run == ["R1"] and model_inputs == ["verified R1"] * (failures + 1)
    if failures == 2:
        assert len(planner_calls) == 1 and "Read R1" in " ".join(planner_calls[0])
        assert "Investigate" not in " ".join(planner_calls[0])
    else:
        assert planner_calls == [] and [e[0] for e in events].count("model_retry") == 1


@pytest.mark.parametrize("planner_result", ["usable", "http500", "length", "unchanged", "tool"])
async def test_rephrasing_reuses_evidence_and_keeps_the_original_request(
    monkeypatch, planner_result,
):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    question = "Check the release of service PAY-7, using the earlier date."
    previous = "Use November 17 for the comparison."
    evidence = "Document AC-168 reports release 4.0."
    draft = "Find which PAY-7 release matches the requested date."
    observations = []
    invocations = []
    actions = []

    @tool
    async def read_document(doc: str) -> str:
        """Read a release report."""
        actions.append(doc)
        return evidence

    def respond(messages, names):
        invocations.append((messages, names))
        if len(invocations) == 1:
            return call("read_document", doc="AC-168")
        if len(invocations) in (2, 3):
            observations.append([m.text for m in messages])
            assert names and any(m.text == evidence for m in messages)
            raise failure(500)
        if len(invocations) == 4:
            assert names == []
            assert any(isinstance(m, HumanMessage) and question in m.text for m in messages)
            assert all(previous not in m.text and evidence not in m.text for m in messages)
            if planner_result == "http500":
                raise failure(500)
            if planner_result == "length":
                return AIMessage(content=draft, response_metadata={"finish_reason": "length"})
            if planner_result == "unchanged":
                return AIMessage(content=question)
            if planner_result == "tool":
                return call("read_document", doc="EXTRA")
            return AIMessage(content=draft)
        assert len(invocations) == 5 and set(names) == set(invocations[1][1])
        assert any(isinstance(m, HumanMessage) and question in m.text for m in messages)
        assert any(isinstance(m, HumanMessage) and previous in m.text for m in messages)
        assert any(isinstance(m, ToolMessage) and m.text == evidence
                   and m.tool_call_id == "call_read_document" for m in messages)
        assert [m.text for m in messages] != observations[-1]
        assistant_text = [m.text for m in messages if isinstance(m, AIMessage)]
        if planner_result == "usable":
            assert any(draft in text for text in assistant_text)
        else:
            assert all(draft not in text and question not in text for text in assistant_text)
        return AIMessage(content="The verified release is 4.0.")

    result = await engine.run_turn(Model(respond=respond), question, [read_document],
                                   history=[HumanMessage(previous), AIMessage("Understood.")])
    assert result == "The verified release is 4.0."
    assert actions == ["AC-168"] and observations[0] == observations[1]


@pytest.mark.parametrize("other_error", [failure(429), httpx.ReadTimeout("busy")])
@pytest.mark.parametrize("other_first", [True, False])
async def test_mixed_transient_errors_preserve_original_request(
    monkeypatch, other_first, other_error,
):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    first, second = (other_error, failure(500)) if other_first else (failure(500), other_error)
    calls = []

    def respond(messages, names):
        calls.append(([m.text for m in messages], names))
        if len(calls) == 1:
            raise first
        if len(calls) == 2:
            raise second
        return AIMessage(content="Done")

    assert await engine.run_turn(Model(respond=respond), "What changed?", []) == "Done"
    assert calls[0] == calls[1] == calls[2]


@pytest.mark.parametrize("failures_before_refusal", [0, 2])
async def test_explicit_refusal_wins_over_an_error_looking_message(
    monkeypatch, failures_before_refusal,
):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    calls = []

    def respond(messages, names):
        calls.append(names)
        if len(calls) <= failures_before_refusal:
            raise failure(500)
        return AIMessage(content="OpenAIInvalidRequestError: HTTP 500",
                         response_metadata={"finish_reason": "content_filter"})

    with pytest.raises(PermissionError):
        await engine.run_turn(Model(respond=respond), "Check a fact.", [])
    assert len(calls) == failures_before_refusal + 1


@pytest.mark.parametrize("revoke_after_call", [2, 3])
async def test_rephrasing_and_final_call_each_require_access(monkeypatch, revoke_after_call):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    calls = []

    async def authorize():
        return len(calls) < revoke_after_call

    def respond(messages, names):
        calls.append(names)
        if len(calls) <= 2:
            raise failure(500)
        assert not names
        return AIMessage(content="Verify the release details.")

    with pytest.raises(PermissionError):
        await engine.run_turn(Model(respond=respond), "Check release details.", [],
                              authorize=authorize)
    assert len(calls) == revoke_after_call


@pytest.mark.parametrize(("exc", "attempts"), [
    (failure(500), 4), (GigaCodeTransientError(), 4),
    (failure(503, **{"Retry-After": "120"}), 1),
    (failure(503, **{"x-should-retry": "false"}), 1),
    (failure(400), 1), (failure(401), 1), (failure(403), 1),
])
async def test_exhaustion_counts_provider_attempts_and_respects_denials(monkeypatch, exc, attempts):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    calls = []

    def respond(*_):
        calls.append(True)
        raise exc

    with pytest.raises(ModelGatewayError) as error:
        await engine.run_turn(Model(respond=respond), "Hello", [])
    assert error.value.attempts == len(calls) == attempts
    assert "private" not in str(error.value)


async def test_cancel_during_retry_after_prevents_second_call():
    submitted = []
    waiting = asyncio.Event()

    def respond(*_):
        submitted.append(True)
        raise failure(429, **{"Retry-After": "20"})

    async def report(kind, text):
        if kind == "model_retry":
            waiting.set()

    task = asyncio.create_task(engine.run_turn(Model(respond=respond), "Hello", [],
                                               on_event=report))
    async with asyncio.timeout(2):
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(submitted) == 1


async def test_revoked_grant_during_wait_stops_new_model_call(monkeypatch):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    permitted = True
    calls = []

    async def authorize():
        return permitted

    def respond(*_):
        calls.append(True)
        raise failure(503)

    async def report(kind, text):
        nonlocal permitted
        if kind == "model_retry":
            permitted = False

    with pytest.raises(PermissionError):
        await engine.run_turn(Model(respond=respond), "Hello", [], on_event=report,
                              authorize=authorize)
    assert len(calls) == 1


@pytest.mark.parametrize("first", [
    AIMessage(content="partial", response_metadata={"finish_reason": "length"}),
    AIMessage(content="", invalid_tool_calls=[{"name": "read_document", "args": "{",
                                               "id": "bad", "error": "invalid JSON"}]),
    AIMessage(content=""),
])
async def test_incomplete_native_step_is_corrected_without_repeating_a_read(first):
    invocations = []
    reads = []

    @tool
    async def read_document() -> str:
        """Read the current production record."""
        reads.append(True)
        return "Version 8 is deployed."

    def respond(messages, names):
        invocations.append(messages)
        if len(invocations) == 1:
            return call("read_document")
        assert isinstance(messages[-1], ToolMessage) and "Version 8" in messages[-1].text
        if len(invocations) == 2:
            return first
        assert "concise complete answer" in messages[0].text
        assert all("partial" not in m.text for m in messages[1:])
        return AIMessage(content="Version 8 is deployed.")

    answer = await engine.run_turn(Model(respond=respond), "Which version is live?",
                                   [read_document])
    assert answer == "Version 8 is deployed." and len(invocations) == 3 and reads == [True]


@pytest.mark.parametrize("param,forbidden,expected", [
    ("tools", False, ["native", "read"]), ("tools", True, ["native"]),
    ("model", False, ["native"]), (None, False, ["native"]),
])
async def test_schema_recovery_never_replays_a_permanent_request(param, forbidden, expected):
    invocations = []

    @tool
    def list_indexes() -> list:
        """List published indexes."""
        pytest.fail("A deferred decision must never dispatch a tool")

    def respond(messages, names):
        invocations.append("native" if names else "read")
        if names:
            raise ModelGatewayError(422, category="upstream-http", validation_field=param,
                                    retry_forbidden=forbidden)
        return AIMessage(content='{"defer":true}')

    with pytest.raises(ModelGatewayError) as caught:
        await engine.run_turn(Model(respond=respond), "Look up the release.", [list_indexes])
    assert invocations == expected
    assert caught.value.status == 422 and caught.value.attempts == len(invocations)


def test_http_date_retry_after_and_non_transient_certificate_error():
    date = datetime.now(UTC) + timedelta(seconds=7)
    error = gateway_exception(failure(429, **{"Retry-After": format_datetime(date, usegmt=True)}))
    assert error.retryable and 5 < error.retry_after <= 7
    cert = ssl.SSLCertVerificationError("secret path")
    connection = httpx.ConnectError("private")
    connection.__cause__ = cert
    tls = gateway_exception(connection)
    assert not tls.retryable and "TLS" in str(tls) and "private" not in str(tls)
    assert gateway_exception(GigaCodePolicyStop()) is None
