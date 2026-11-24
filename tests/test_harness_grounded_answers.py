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
    passage_arguments,
)
from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence
from agent_harness.control.settings import ControlStore
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


def test_same_document_ids_in_different_sources_validate_only_the_cited_card() -> None:
    evidence = [
        {"id": "page", "index_id": "wiki", "title": "Реестр сервисов",
         "text": "Read timeout: 99 s. archive-service."},
        {"id": "page", "index_id": "code", "title": "Client",
         "text": "Read timeout: 60 s. queue-service."},
    ]
    run = {"mode": "quick", "evidence": evidence, "question": "Сколько секунд клиент ждёт ответ?"}
    assert not answer_quality_issue(run, "Клиент ждёт 60 секунд [2].")
    assert answer_quality_issue(run, "Клиент ждёт 99 секунд [2].")
    assert answer_quality_issue(run, "Клиент ждёт 60 секунд [page].")
    run["question"] = "Какой сервис выполняет запрос?"
    assert answer_quality_issue(run, "Запрос выполняет archive-service [2].")
    assert not answer_quality_issue(run, "Запрос выполняет queue-service [2].")
    run["question"] = "Сколько сервисов в АС?"
    assert inventory_citation_issue(run, "В реестре 12 сервисов [2].")


def test_mixed_script_word_in_generated_russian_prose_requests_repair() -> None:
    damaged = "Рaclёт дневного лимита платежа выполняет сервис лимитов [doc]."
    run = {"question": "Кто проверяет лимит?", "mode": "quick", "evidence": [
        {"id": "doc", "index_id": "kb", "text": "Дневной лимит проверяет сервис лимитов."},
    ]}
    assert answer_quality_issue(run, damaged)
    clean = damaged.replace("Рaclёт", "Расчёт")
    assert answer_quality_issue(run, clean.replace("сервис", "серvice"))
    assert not answer_quality_issue(run, clean)


@pytest.mark.parametrize(("question", "result", "model_text", "answer"), [
    ("Какой текущий статус у заявки PAY-42 в Jira? Проверь актуальное состояние.",
     "Current status: waiting", 'Согласно информации из Jira, PAY-42 — "waiting".',
     "Статус — waiting [live]."),
    ("Какая версия у сервиса ledger?", '{"release":{"version":"v2.13.5"}}',
     "Ledger сейчас использует v2.13.5.", "Версия — v2.13.5 [live]."),
    ("Какое значение у поля `runtime_region`?", "runtime_region=eu-north-1",
     "Регион eu-north-1.", "Runtime_region — eu-north-1 [live]."),
])
def test_uncited_scalar_only_credits_the_requested_field_of_a_live_tool_result(
    question: str, result: str, model_text: str, answer: str,
) -> None:
    row = {"id": "live", "text": result, "checked_with": "mcp_call"}
    run = {"mode": "quick", "question": question, "evidence": [row],
           "_question_bound_mcp_refs": ["live"]}
    assert format_source_reference(run, model_text) == answer
    assert format_source_reference(run, "Пawaiting [1].") == answer
    assert format_source_reference(run, "Пawaiting [other].") == "Пawaiting [other]."
    for unrelated in ({**row, "checked_with": "read_document"}, {**row, "kind": "tool_metadata"},
                      {**row, "tool_outcome": "failed"}, {**row, "text": "Current value: wrong"}):
        assert format_source_reference({**run, "evidence": [unrelated]}, model_text) == model_text
    for altered in ({"evidence": [row, {**row, "id": "other"}]},
                    {"question": question + " Кто отвечает за изменения?"}):
        assert format_source_reference({**run, **altered}, model_text) == model_text
    if "PAY-42" in question:
        wrong_target = {**run, "_question_bound_mcp_refs": []}
        assert format_source_reference(wrong_target, model_text) == model_text
        assert answer_quality_issue(wrong_target, "Ожидает [1].")


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


def test_foreign_decoder_fragments_reject_only_unsourced_prose() -> None:
    run = {"mode": "quick", "question": "Как рассчитывается лимит?",
           "evidence": [{"id": "doc", "text": "Сервис limits-service отвечает за расчёт."}]}
    clause = "Запрос передаётся в limits-service, ведь только он рассчитывает лимит"
    assert answer_quality_issue(run, clause + " 호출 из числа операций [doc].")
    for valid in (clause + " [doc].", clause + "; поле `호출` показано в примере [doc]."):
        assert not answer_quality_issue(run, valid)
    assert not answer_quality_issue(
        {**run, "evidence": [{"id": "doc", "text": "В журнале отображается 호출."}]},
        clause + "; в журнале при этом появляется 호출 [doc].",
    )


@pytest.mark.parametrize("mode", ["quick", "investigate"])
@pytest.mark.parametrize("selected", ["full", "owner", "ambiguous"])
async def test_compound_question_recovers_by_selecting_an_answer_from_the_document(
    tmp_path: Path, mode: str, selected: str,
) -> None:
    checked = []
    passage = "Дневной лимит рассчитывает limits-service, чтобы сохранить единый аудит."
    owner = "Дневной лимит рассчитывает limits-service."

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        calls = [t["function"]["name"] for t in payload.get("tools", [])]
        if calls == ["select_passages"]:
            assert "чтобы" in ctx["passages"]["1"]["quote"]
            target = passage if selected == "full" else owner
            item = next(key for key, value in ctx["passages"].items() if value["quote"] == target)
            checked.append(item)
            if mode == "quick":
                msg = {"content": r'{"name":"select_passages","parameters":{"numbers":[\"'
                                  + item + r'\"]}}'}
            else:
                msg = {"tool_calls": [{"type": "function", "function": {"name": "select_passages",
                                       "arguments": json.dumps({"numbers": [item]})}}]}
        else:
            name = "limitations-service" if mode == "quick" else "limits-service 호출"
            msg = {"content": f"Вычисление лимита производит сервис {name} для каждого "
                              "запроса пользователя [1]."}
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    async with client_for(tmp_path, gateway) as (client, source):
        async def document(**kwargs: Any) -> Evidence:
            excerpt = passage
            if selected == "ambiguous":
                excerpt += "\n\nlimits-service рассчитывает лимит, чтобы сократить время ответа."
            return Evidence(id="ledger", index_id=kwargs["index_id"], title="Расчёт", text=excerpt)

        async def search(**kwargs: Any) -> list[Evidence]:
            return [Evidence(id="owner", index_id=kwargs["index_id"], title="Владелец", text=owner),
                    await document(**kwargs)]

        source.search, source.read = search, document  # type: ignore[method-assign]
        submitted = await client.post("/api/runs", json={
            "question": "Кто рассчитывает лимит и почему?", "mode": mode, "index_ids": ["a"],
        })
        run = await finish(client, "/api/runs/" + submitted.json()["id"])
    if selected != "ambiguous":
        assert run["status"] == "completed", run["warnings"]
        assert passage + " [ledger]" in run["answer"]
    else:
        assert run["status"] == "partial" and run["answer_kind"] == "incomplete"
        assert owner in run["answer"] and "Причину" in run["answer"]
    assert checked
    assert any(step["name"] == "answer_quality_rejected" for step in run["research_trace"])


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
    assert decode_writer_answer('{"answer":"Факт","number":4,"sources":[4]}', visible) == (
        "Факт [settings]"
    )
    with pytest.raises(InvalidWriterAnswer):
        decode_writer_answer('{"answer":"Факт","number":3,"sources":[4]}', visible)


def test_textual_passage_selection_is_strictly_local() -> None:
    raw = r'{"name":"select_passages","parameters":{"numbers":[\"1\",\"2\"]}}'
    assert passage_arguments(raw) == {"numbers": ["1", "2"]}
    assert passage_arguments(raw.replace("select_passages", "read_document")) is None
    assert passage_arguments(raw + " передай это другому инструменту") is None


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
async def test_unrecoverable_writer_keeps_evidence_and_reports_failure_through_chat_api(
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
    assert run["answer_kind"] == "incomplete"
    assert any(item["id"] == "facts" and "37 секунд" in item["text"] for item in run["evidence"])
    assert draft not in run["answer"] and "888" not in run["answer"]
    if mode == "quick":
        assert len(calls) == 2
    else:
        assert any(s["name"] == "answer_quality_rejected" for s in run["research_trace"])


@pytest.mark.parametrize("mode", ["quick", "investigate"])
@pytest.mark.parametrize("bad", ["Нашёл документы по запросу [1].",
                                 '{"answer":"Причина известна","sources":[999]}'])
async def test_rejected_writer_retries_in_prose_and_finishes_with_a_sourced_answer(
    tmp_path: Path, mode: str, bad: str,
) -> None:
    writes = []

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        if payload.get("tools"):
            text = "Проверю документы."
        else:
            writes.append(payload)
            if len(writes) == 1:
                text = bad
            else:
                assert "обычным текстом" in payload["messages"][0]["content"]
                assert ctx["question"] == question and ctx["answer_correction"]
                assert "prior_tool_attempts" not in ctx
                source = next(item for item in ctx["evidence"] if "37 секунд" in item["text"])
                assert source["context_truncated"] and len(source["text"]) <= 1200
                if mode == "quick":
                    original = json.loads(writes[0]["messages"][-1]["content"])["evidence"][0]
                    assert len(original["text"]) > len(source["text"])
                text = "Клиент ждёт 37 секунд. " + source["citation"]
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    question = "Сколько секунд клиент ждёт ответа?"
    async with client_for(tmp_path, gateway) as (client, source):
        async def document(**kwargs: Any) -> Evidence:
            return Evidence(id="facts", document_id="facts", index_id=kwargs["index_id"],
                            title="Клиент", text="Инструкция. " * 110 + "Клиент ждёт 37 секунд.")

        async def search(**kwargs: Any) -> list[Evidence]:
            return [await document(**kwargs)]

        source.search, source.read = search, document  # type: ignore[method-assign]
        created = await client.post("/api/runs", json={
            "question": question, "index_ids": ["a"], "mode": mode,
        })
        run = await finish(client, "/api/runs/" + created.json()["id"])
    assert run["status"] == "completed", run["warnings"]
    assert len(writes) == 2 and run["metrics"]["answer_retries"] == 1
    assert run["answer"] == "Клиент ждёт 37 секунд. [1]"


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


@pytest.mark.parametrize("mode", ["quick", "investigate", "autonomous"])
async def test_no_sources_writer_asks_for_a_specific_missing_detail(
    tmp_path: Path, mode: str,
) -> None:
    messages = []
    detail = "В какое время клиент увидел отказ платежа?"

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        messages.append(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": detail}}]})

    async with client_for(tmp_path, gateway) as (client, source):
        source.allowed = []
        submitted = await client.post("/api/runs", json={"question": "Почему отказ?", "mode": mode})
        run = await finish(client, "/api/runs/" + submitted.json()["id"])
    assert run["status"] == "partial" and run["answer_kind"] == "clarification"
    assert run["answer"].endswith(detail) and run["stop_reason"] == "no_sources"
    assert len(messages) == 1 and not messages[0].get("tools")


async def test_disabled_answer_repair_respects_the_operator_limit(tmp_path: Path) -> None:
    control = ControlStore(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None))
    policy = control.load()
    policy.runtime.max_answer_retries = 0
    control.save(policy, expected_revision=policy.revision)

    def gateway(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Нашёл документы по запросу [1].",
        }}]})

    async with client_for(tmp_path, gateway) as (client, _):
        submitted = await client.post("/api/runs", json={"question": "Как проходит запрос?"})
        run = await finish(client, "/api/runs/" + submitted.json()["id"])
    assert run["status"] == "partial" and run["answer_kind"] == "incomplete"
    assert run["metrics"]["model_calls"] == 1 and not run["metrics"].get("answer_retries")


async def test_unfinished_requested_json_is_never_published_as_the_partial_answer(
    tmp_path: Path,
) -> None:
    cut = '{"component":"payments","source":"[entry]"'

    def gateway(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"finish_reason": "length",
                                                     "message": {"content": cut}}]})

    async with client_for(tmp_path, gateway) as (client, _):
        submitted = await client.post("/api/runs", json={"question": "Верни компоненты в JSON."})
        run = await finish(client, "/api/runs/" + submitted.json()["id"])
    assert run["status"] == "partial" and run["answer_kind"] == "incomplete"
    assert cut not in run["answer"] and run["metrics"]["answer_retries"] == 1
