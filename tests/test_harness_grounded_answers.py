"""Ordinary chat modes must carry real retrieval results through to the user."""
# ruff: noqa: RUF001  # Russian sample answers.

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_harness_api_recovery import client_for, finish

from agent_harness.answer_quality import (
    InvalidWriterAnswer,
    answer_quality_issue,
    cited_evidence_ids,
    decode_writer_answer,
    format_source_reference,
)
from agent_harness.contracts import Evidence
from agent_harness.research import inventory_citation_issue


def test_duration_and_card_number_are_checked_against_real_evidence() -> None:
    evidence = [{"id": "doc-A", "index_id": "a", "title": "Client", "text": "Read timeout: 1 min."}]
    run = {"question": "Сколько секунд клиент ждёт ответ?", "mode": "quick", "evidence": evidence}
    assert format_source_reference(run, "60 секунд") == "60 секунд [doc-A]"
    assert not answer_quality_issue(run, "Клиент ждёт 60 секунд [1].")
    assert cited_evidence_ids("Клиент ждёт 60 секунд [1].", evidence) == {"doc-A"}
    assert answer_quality_issue(run, "Клиент ждёт 99 секунд [1].")
    assert answer_quality_issue(run, "Клиент ждёт 60 секунд [2].")
    assert not answer_quality_issue(run, "Клиент ждёт подтверждения [doc-A].")
    other = {**evidence[0], "id": "doc-B", "text": "Read timeout: 99 s."}
    assert format_source_reference({**run, "evidence": [*evidence, other]}, "60 секунд") == (
        "60 секунд"
    )


@pytest.mark.parametrize("mode", ["quick", "investigate"])
def test_inventory_count_must_match_the_cited_registry_in_every_mode(mode: str) -> None:
    evidence = [{"id": "catalog", "index_id": "kb", "title": "Реестр сервисов",
                 "text": '{"service_count": 12}'}]
    run = {"mode": mode, "evidence": evidence, "question": "Сколько сервисов в АС?"}
    assert not inventory_citation_issue(run, "В реестре 12 сервисов [1].")
    assert inventory_citation_issue(run, "В реестре 80 сервисов [catalog].")


def test_json_metadata_is_neither_a_finding_nor_a_citation_in_prose() -> None:
    evidence = [{"id": "doc", "title": "Limits", "text": "Сервис публикует изменения."}]
    question = "Кто публикует изменения?"
    run = {"mode": "investigate", "question": question, "evidence": evidence}
    receipt = '{"id":"doc","citation":"[doc]"}'
    generated = receipt + "}\n\n`limits-service` публикует изменения."
    assert answer_quality_issue(run, generated)
    assert not cited_evidence_ids(generated, evidence)
    json_request = "Верни результат в формате JSON"
    payload = '{"components":["limits"],"source":"[1]"}'
    for response in (payload, f"```json\n{payload}\n```"):
        assert not answer_quality_issue({**run, "question": json_request}, response)
        assert cited_evidence_ids(response, evidence, question=json_request) == {"doc"}
    assert not answer_quality_issue(run, f"```js\nconst src = {receipt}\n```\n\nПример [doc].")


def test_writer_numbers_resolve_from_the_offered_list_when_ids_are_numeric() -> None:
    # Panel position 2 is absent from this model input. Literal source ID 4
    # belongs to position 1, forcing position 4 to cite its actual ID instead.
    visible = {1: "1", 4: "settings"}
    assert decode_writer_answer('{"answer":"Проверено","sources":[4,1,4]}', visible) == (
        "Проверено [settings] [1]"
    )
    assert decode_writer_answer("Проверено [settings].", visible) == "Проверено [settings]."
    for bad in ({"answer": "Сведения", "sources": [2]}, {"answer": "Сведения", "sources": [True]},
                {"answer": "Сведения [1].", "sources": [4]},
                {"answer": '{"id":"evil","citation":"[1]"}', "sources": [1]}):
        with pytest.raises(InvalidWriterAnswer):
            decode_writer_answer(json.dumps(bad), visible)
    with pytest.raises(InvalidWriterAnswer):
        decode_writer_answer('Ответ:\n{"answer":"Сведения","sources":[1]}', visible)


@pytest.mark.parametrize("mode", ["quick", "investigate"])
async def test_native_writer_returns_a_grounded_answer_with_one_text_synthesis(
    tmp_path: Path, mode: str,
) -> None:
    writes = []

    def provider(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        if payload.get("tools"):
            text = "Проверю документы."
        else:
            assert "response_format" not in payload
            source = next(item for item in ctx["evidence"] if item["text"])
            writes.append(source)
            text = json.dumps({"answer": "Запрос проходит через платёжный сервис.",
                               "sources": [source["number"]]})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    async with client_for(tmp_path, provider) as (client, _):
        created = await client.post("/api/runs", json={
            "question": "Как проходит запрос?", "index_ids": ["a"], "mode": mode,
        })
        run = await finish(client, "/api/runs/" + created.json()["id"])
    assert len(writes) == 1 and run["status"] == "completed", run["warnings"]
    assert run["answer"] == "Запрос проходит через платёжный сервис. " + writes[0]["citation"]
    if mode == "quick":
        assert run["metrics"]["model_calls"] == 1


@pytest.mark.parametrize("mode", ["quick", "investigate"])
@pytest.mark.parametrize("draft", ["Нашёл документы по вашему запросу [facts].",
                                   "Таймаут 888 секунд [facts].", "Значение 888 [foreign].",
                                   '{"id":"facts","citation":"[facts]"}}\n\nКлиент ждёт ответа.'])
async def test_bad_writer_returns_actual_source_text_through_chat_api(
    tmp_path: Path, mode: str, draft: str,
) -> None:
    calls = []

    def provider(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": draft}}]})

    async def document(**kwargs: Any) -> Evidence:
        return Evidence(id="facts", document_id="facts", index_id=kwargs["index_id"],
                        title="Проверка таймаута", text="При чтении клиент ждёт 37 секунд.")

    async def search(**kwargs: Any) -> list[Evidence]:
        return [await document(**kwargs)]

    async with client_for(tmp_path, provider) as (client, source):
        source.search, source.read = search, document  # type: ignore[method-assign]
        created = await client.post("/api/runs", json={
            "question": "Сколько секунд клиент ждёт ответа?", "index_ids": ["a"], "mode": mode,
        })
        created.raise_for_status()
        run = await finish(client, "/api/runs/" + created.json()["id"])
    assert run["status"] == "partial" and run["stop_reason"] == "answer_quality"
    assert run["answer_kind"] == "search_results"
    assert "37 секунд" in run["answer"] and "facts" in run["answer"]
    assert draft not in run["answer"] and "888" not in run["answer"]
    if mode == "quick":
        assert len(calls) == 1
    else:
        assert any(s["name"] == "answer_quality_rejected" for s in run["research_trace"])


def test_technical_name_lookup_checks_the_reported_identifier_in_its_source() -> None:
    run = {"mode": "quick", "question": "Какое событие публикует billing-service?",
           "evidence": [
               {"id": "intro", "index_id": "kb", "title": "Introduction",
                "text": "The `billing-service` calculates the bill."},
               {"id": "events", "index_id": "kb", "title": "Events",
                "text": "After reconciliation, `billing-service` publishes `bill-posted-v3`."},
           ]}
    assert not answer_quality_issue(run, "Событие называется bill-posted-v3. [2]")
    assert answer_quality_issue(run, "Сервис увеличивает billing-service [1] [2].")
    assert answer_quality_issue(run, "bill-posted-v3 [1].")
    assert answer_quality_issue(run, "bill-posted-v30 [2].")
    owner = {**run, "question": "Как называется компания, которой принадлежит сервис?",
             "evidence": [{"id": "owner", "text": "Компания Альфа владеет `billing-service`."}]}
    assert not answer_quality_issue(owner, "Сервис принадлежит компании Альфа [1].")
