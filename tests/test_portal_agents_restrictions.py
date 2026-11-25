import json
import sqlite3
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from test_portal_agents_connections import READER, browser
from test_portal_agents_engine import Model, call
from test_portal_agents_read_recovery import _corporate_portal

from corporate_kb.gigacode_runner import GigaCodePolicyStop
from portal_agents import http
from portal_agents.model_errors import ModelGatewayError


def _checkpoints(path):
    with sqlite3.connect(path) as db:
        return (db.execute("SELECT * FROM checkpoints ORDER BY 1,2,3").fetchall(),
                db.execute("SELECT * FROM writes ORDER BY 1,2,3,4,6").fetchall())


@pytest.mark.parametrize("signal", [
    "content_filter", "blacklist", "refusal", "refusal_block", "runner", "gateway", "policy422",
])
async def test_provider_restriction_is_terminal_and_removes_only_the_restricted_turn(
    settings_factory, monkeypatch, signal,
):
    portal = _corporate_portal(settings_factory)
    state = portal[1].agents_state_dir
    chat = str(uuid4())
    attempts = []
    forbidden = "PRIVATE_RESTRICTED_REQUEST"

    def provider(messages, names):
        if isinstance(messages[-1], HumanMessage):
            if messages[-1].text == forbidden:
                return call("list_indexes")
            assert not any(forbidden in m.text for m in messages)
            return AIMessage(content="Документ описывает политику выпуска версий.")
        assert isinstance(messages[-1], ToolMessage)
        attempts.append(signal)
        if signal == "runner":
            raise GigaCodePolicyStop()
        if signal == "gateway":
            raise ModelGatewayError(500, category="policy")
        if signal == "policy422":
            raise ModelGatewayError(422, category="policy")
        if signal == "refusal":
            return AIMessage(content="", additional_kwargs={"refusal": "PRIVATE_REASON"})
        if signal == "refusal_block":
            return AIMessage(content=[{"type": "refusal", "refusal": "PRIVATE_REASON"}])
        return AIMessage(content="PRIVATE_REASON", response_metadata={"finish_reason": signal})

    @asynccontextmanager
    async def model(*args, **kwargs):
        yield Model(respond=provider)

    monkeypatch.setattr(http, "create_model", model)
    async with browser(portal) as client:
        async def send(question, thread=chat):
            response = await client.post("/agents/api/chat", headers=READER,
                                         json={"chat": thread, "question": question})
            response.raise_for_status()
            return [json.loads(line) for line in response.text.splitlines()]

        assert (await send("Какая политика выпуска?"))[-1]["event"] == "answer"
        db = state / "checkpoints.sqlite3"
        visible = await client.get("/agents/api/chats/" + chat, headers=READER)
        before = _checkpoints(db)
        assert all(before)
        for thread in (chat, str(uuid4())):
            events = await send(forbidden, thread)
            final = events[-1]
            assert final["event"] == "restricted" and "Уточните" in final["text"]
            assert not {"error", "answer", "model_retry"} & {item["event"] for item in events}
            assert "PRIVATE_REASON" not in str(events)
            assert ("reference" in final) == (signal == "policy422")
            assert _checkpoints(db) == before
        after = await client.get("/agents/api/chats/" + chat, headers=READER)
        assert after.json() == visible.json()
        assert (await send("Что дальше?"))[-1]["event"] == "answer"
    assert attempts == [signal, signal]


@pytest.mark.parametrize("failure", [PermissionError("private grants"),
                                     ModelGatewayError(403, category="access"),
                                     ModelGatewayError(404)])
async def test_access_and_configuration_errors_stay_distinct_from_model_policy(
    settings_factory, monkeypatch, failure,
):
    def provider(messages, names):
        raise failure

    @asynccontextmanager
    async def model(*args, **kwargs):
        yield Model(respond=provider)

    monkeypatch.setattr(http, "create_model", model)
    async with browser(_corporate_portal(settings_factory)) as client:
        response = await client.post("/agents/api/chat", headers=READER,
                                     json={"chat": str(uuid4()), "question": "Какие индексы?"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["event"] == "error" and "restricted" not in {e["event"] for e in events}
    assert "private grants" not in response.text
