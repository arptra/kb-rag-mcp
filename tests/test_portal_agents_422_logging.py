import json
import re
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from test_gpt2giga_transport import native_reply
from test_portal_agents_connections import ADMIN, READER, browser
from test_portal_agents_read_recovery import _corporate_portal, _corporate_transport


def _one_record(caplog, event):
    [record] = [r.getMessage() for r in caplog.records if r.getMessage().startswith(event + " ")]
    reference = re.search(r"\bref=(\S+)", record)
    assert reference and reference.group(1) != "-"
    return reference.group(1), record


def _validation(path):
    return httpx.Response(422, json={"detail": [
        {"type": "value_error", "loc": path, "msg": "PRIVATE_PROVIDER_MESSAGE",
         "input": {"Authorization": "PRIVATE_CREDENTIAL"}},
    ]}, headers={"x-request-id": "PRIVATE_NON_UUID_HEADER"})


def _safe(logs, body):
    for marker in ("PRIVATE_PROVIDER_MESSAGE", "PRIVATE_CREDENTIAL", "PRIVATE_NON_UUID_HEADER",
                   "PRIVATE_SCHEMA_KEY", "PRIVATE_QUESTION", "model.test"):
        assert marker not in logs and marker not in body


async def test_bad_portal_input_has_its_own_reference_without_contacting_model(
    settings_factory, monkeypatch, caplog,
):
    provider = Mock(side_effect=AssertionError("No provider call is needed for invalid input"))
    _corporate_transport(monkeypatch, provider)
    async with browser(_corporate_portal(settings_factory)) as client:
        response = await client.post("/agents/api/chat", headers=READER, json={
            "chat": "PRIVATE_CREDENTIAL", "question": "PRIVATE_QUESTION",
            "PRIVATE_SCHEMA_KEY": True,
        })
    assert response.status_code == 422
    reference, record = _one_record(caplog, "portal_422")
    assert "operation=chat" in record and "provider_called=false" in record
    assert reference in response.text and "model_422 " not in caplog.text
    provider.assert_not_called()
    _safe(caplog.text, response.text)


async def test_recovered_corporate_native_422_has_one_log_and_a_visible_reference(
    settings_factory, monkeypatch, caplog,
):
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        if payload.get("functions"):
            calls.append("native")
            return _validation(["body", "functions", 0, "parameters", "properties",
                                "PRIVATE_SCHEMA_KEY"])
        frame = json.loads(payload["messages"][-1]["content"])
        if not calls:
            calls.append("defer")
            decision = {"defer": True}
        elif not frame["observations"]:
            calls.append("choose")
            decision = {"tool": "list_indexes", "args": {}}
        else:
            calls.append("answer")
            [result] = frame["observations"]
            assert result["tool"] == "list_indexes"
            [index] = json.loads(result["result"])
            decision = {"answer": f"Доступен индекс {index['id']}."}
        return native_reply({"role": "assistant", "content": json.dumps(decision)}, "stop")

    _corporate_transport(monkeypatch, provider)
    async with browser(_corporate_portal(settings_factory)) as client:
        response = await client.post("/agents/api/chat", headers=READER, json={
            "chat": str(uuid4()), "question": "Какие индексы доступны? PRIVATE_QUESTION",
        })
    response.raise_for_status()
    events = [json.loads(line) for line in response.text.splitlines()]
    assert calls == ["defer", "native", "choose", "answer"]
    assert events[-1]["event"] == "answer" and "Доступен индекс" in events[-1]["text"]
    reference, record = _one_record(caplog, "model_422")
    assert "operation=chat" in record and re.search(r"\btrace=(?!-)\w+", record)
    diagnostics = json.loads(record.partition("diagnostics=")[2])
    assert diagnostics["request"] and diagnostics["validation"]
    assert "functions" in json.dumps(diagnostics["validation"])
    assert any(e["event"] == "model_retry" and reference in e["text"] for e in events)
    assert "portal_422 " not in caplog.text
    _safe(caplog.text, response.text)


@pytest.mark.parametrize("operation", ["chat", "probe"])
async def test_plain_corporate_422_reports_the_same_reference_once(
    settings_factory, monkeypatch, caplog, operation,
):
    def reject(request):
        payload = json.loads(request.content)
        assert not payload.get("functions")
        return _validation(["body", "messages"])

    provider = Mock(side_effect=reject)
    _corporate_transport(monkeypatch, provider)
    async with browser(_corporate_portal(settings_factory)) as client:
        if operation == "probe":
            response = await client.post("/agents/api/model/test", headers=ADMIN,
                                         json={"provider": "api"})
            assert response.is_error
        else:
            response = await client.post("/agents/api/chat", headers=READER, json={
                "chat": str(uuid4()), "question": "Уточни версию PRIVATE_QUESTION",
            })
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]["event"] == "error"
    reference, record = _one_record(caplog, "model_422")
    assert f"operation={operation}" in record and "status=422" in record
    assert reference in response.text and "portal_422 " not in caplog.text
    assert provider.call_count == 1
    _safe(caplog.text, response.text)
