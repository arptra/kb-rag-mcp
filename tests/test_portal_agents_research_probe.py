import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from test_gpt2giga_transport import native_reply
from test_portal_agents_connections import ADMIN, READER, browser
from test_portal_agents_engine import Model, call
from test_portal_agents_read_recovery import _corporate_portal, _corporate_transport

from corporate_kb.access.dev_pki import prepare_local_identity
from portal_agents import http, research_probe

_ENDPOINT = "/agents/api/model/test-research"


def journal_codes(result):
    lines = json.loads(result)["content"].splitlines()
    rows = [line.split(" | ") for line in lines if line[:4].isdigit()]
    assert len(rows) == 3
    earlier, latest = sorted((date, code) for date, status, code in rows if status == "Завершена")
    [planned] = [(date, code) for date, status, code in rows if status == "Запланирована"]
    assert earlier[0] < latest[0] < planned[0]
    assert len({row[-1] for row in rows}) == 3
    return earlier[1], latest[1], planned[1]


async def allowed():
    return True


@asynccontextmanager
async def connection(model):
    yield model


async def test_admin_checks_draft_tls_with_real_graph_and_fresh_fixture_without_saving(
    settings_factory, monkeypatch, tmp_path,
):
    portal = _corporate_portal(settings_factory)
    identity = prepare_local_identity(tmp_path / "certs")
    draft = {"provider": "api", "adapter": "gpt2giga", "base_url": "https://draft.test/v1",
             "model_name": "DeepSeek-V4-Flash", "ca_pem": identity.ca_cert.read_text(),
             "cert_pem": identity.client_cert.read_text(),
             "key_pem": identity.client_key.read_text()}
    create = http.create_model
    documents, codes = set(), set()

    @asynccontextmanager
    async def create_draft(*args, **kwargs):
        for key in ("ca", "cert", "key"):
            assert kwargs[f"{key}_file"].read_text().strip() == draft[f"{key}_pem"].strip()
        async with create(*args, **kwargs) as model:
            yield model

    def provider(request):
        assert request.url.host == "draft.test"
        payload = json.loads(request.content)
        frame = json.loads(payload["messages"][-1]["content"])
        assert {fn["name"] for fn in frame["available_tools"]} == {"search_index", "read_document"}
        assert "PRIVATE KEY" not in str(frame)
        observations = frame["observations"]
        if not observations:
            assert "последней по дате завершённой записи" in frame["question"]
            content = {"tool": "search_index", "args": {"query": "fixture"}}
        elif len(observations) == 1:
            document = json.loads(observations[0]["result"])["document_id"]
            assert document not in frame["question"] and document not in documents
            documents.add(document)
            content = {"tool": "read_document", "args": {"document_id": document}}
        else:
            earlier, latest, planned = journal_codes(observations[1]["result"])
            fixture = {earlier, latest, planned}
            assert fixture.isdisjoint(codes) and not any(c in frame["question"] for c in fixture)
            codes.update(fixture)
            content = {"answer": f"{latest} [Источник](https://invented.invalid/{earlier})"}
        return native_reply({"role": "assistant", "content": json.dumps(content)}, "stop")

    monkeypatch.setattr(http, "create_model", create_draft)
    _corporate_transport(monkeypatch, provider)
    async with browser(portal) as client:
        before = (await client.get("/agents/api/model/settings", headers=ADMIN)).json()
        assert (await client.post(_ENDPOINT, headers=READER, json=draft)).status_code == 403
        cross_site = {**ADMIN, "origin": "https://different.test"}
        assert (await client.post(_ENDPOINT, headers=cross_site, json=draft)).status_code == 403
        for _ in range(2):
            response = await client.post(_ENDPOINT, headers=ADMIN, json=draft)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("application/x-ndjson")
            events = list(map(json.loads, response.text.splitlines()))
            assert events[-1]["event"] == "success" and "— 3" in events[-1]["text"]
            for step in ("1/3", "2/3", "3/3"):
                assert step in response.text
            assert "PRIVATE KEY" not in response.text and not any(c in response.text for c in codes)
        assert (await client.get("/agents/api/model/settings", headers=ADMIN)).json() == before
    assert len(codes) == 6 and len(documents) == 2
    with sqlite3.connect(portal[1].agents_state_dir / "checkpoints.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM checkpoints").fetchone() == (0,)


@pytest.mark.parametrize(("mode", "expected"), [
    ("early", "1/3"), ("wrong_id", "2/3"), ("wrong_answer", "отсутствует код"),
    ("previous", "более ранняя"), ("planned", "запланированная запись"),
    ("multiple", "нескольких записей"), ("limit", "предел"),
])
async def test_diagnostic_reports_verified_stage_and_hides_model_text(monkeypatch, mode, expected):
    attempts = 0

    def respond(messages, _):
        nonlocal attempts
        attempts += 1
        if mode != "early" and attempts == 1:
            return call("search_index", query="fixture")
        if mode != "early" and attempts == 2:
            result = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
            document = json.loads(result.text)["document_id"]
            return call("read_document", document_id="wrong" if mode == "wrong_id" else document)
        if mode in {"previous", "planned", "multiple"}:
            result = next(m for m in reversed(messages) if isinstance(m, ToolMessage))
            earlier, latest, planned = journal_codes(result.text)
            choice = {"previous": earlier, "planned": planned, "multiple": f"{latest}, {earlier}"}
            return AIMessage("provider-private-text " + choice[mode])
        return AIMessage("provider-private-text")

    if mode == "limit":
        monkeypatch.setattr(research_probe, "_CALL_LIMIT", 2)
    events = [item async for item in research_probe.research_events(
        connection(Model(respond=respond)), allowed, lambda _: "reference",
    )]
    assert events[-1][0] == "error" and expected in events[-1][1]
    assert "provider-private-text" not in str(events)
    if mode in {"wrong_answer", "previous", "planned", "multiple"}:
        assert "Подключение и чтение работают." in events[-1][1]
    if mode == "limit":
        assert attempts == 2


async def test_diagnostic_timeout_and_disconnect_cancel_model_calls(monkeypatch):
    stopped = asyncio.Event()

    async def slow(*args, on_event, **kwargs):
        await on_event("model", "")
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(research_probe, "run_turn", slow)
    monkeypatch.setattr(research_probe, "_TIME_LIMIT", 0.1)
    events = [item async for item in research_probe.research_events(
        connection(None), allowed, lambda _: "ref",
    )]
    assert stopped.is_set() and events[-1][0] == "error" and "Истекло время" in events[-1][1]
    stopped.clear()
    check = research_probe.research_events(connection(None), allowed, lambda _: "ref")
    await anext(check)
    assert (await anext(check))[0] == "progress"
    await check.aclose()
    assert stopped.is_set()


async def test_provider_rejection_shows_safe_reference_and_step(settings_factory, monkeypatch):
    portal = _corporate_portal(settings_factory)
    _corporate_transport(monkeypatch, lambda _: httpx.Response(401, text="private provider error"))
    async with browser(portal) as client:
        response = await client.post(_ENDPOINT, headers=ADMIN, json={"provider": "api"})
    terminal = json.loads(response.text.splitlines()[-1])
    assert terminal["event"] == "error" and "1/3" in terminal["text"]
    assert "401" in terminal["text"] and "Номер ошибки" in terminal["text"]
    assert "private provider error" not in response.text
