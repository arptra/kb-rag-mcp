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


async def test_subagent_retries_only_failed_model_step(monkeypatch):
    monkeypatch.setattr(engine, "_MODEL_BACKOFF", lambda _: 0)
    model_inputs = []
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
        if isinstance(user, HumanMessage):
            return call("read_document", doc="R1")
        assert isinstance(user, ToolMessage)
        model_inputs.append(user.text)
        if len(model_inputs) == 1:
            raise failure(500)
        return AIMessage(content=user.text)

    async def report(name, text):
        events.append((name, text))

    result = await engine.run_turn(Model(respond=respond), "Investigate", [read_document],
                                   on_event=report)
    assert result == "answer R1"
    assert tools_run == ["R1"] and model_inputs == ["verified R1"] * 2
    assert [e[0] for e in events].count("model_retry") == 1


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
