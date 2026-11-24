"""Autopilot drafts must contain a finding and refer to retained source material."""

# ruff: noqa: RUF001  # Russian conversation samples.
from typing import Any

import pytest

from agent_harness.answer_quality import (
    answer_quality_issue,
    format_source_reference,
    render_quoted_findings,
    reviewed_excerpt,
)

DOCUMENT = "Pay/ops:v2.5+current#L14"
CHECKOUT = "api/ref123/checkout"


def test_reviewed_excerpt_is_bound_to_scope_revision_and_an_accepted_source() -> None:
    quote = "Requests are retried once after 20 seconds."
    entry = {"index_id": "wiki", "id": "entry", "revision": "42", "text": quote,
             "checked_with": "read_document"}
    support = {"index_id": "wiki", "id": "entry", "revision": "42", "quote": quote,
               "quote_is_answer": True}
    run = {"mode": "autonomous", "question": "What is the retry delay?", "index_ids": ["wiki"],
           "evidence": [entry], "_retrieval_support": support}
    assert reviewed_excerpt(run) == f"{quote} [entry]"
    assert reviewed_excerpt({**run, "index_ids": ["another"]}) is None
    for unresolved in ({"_retrieval_gap": "Missing current value"}, {"_uncertain_calls": ["wait"]},
                       {"_pending_source_checks": ["wiki"]}, {"_inflight_action": {"key": "x"}},
                       {"research_plan": {"gaps": ["Check the source"]}},
                       {"sandbox_profile_id": "java"}):
        assert reviewed_excerpt({**run, **unresolved}) is None
    for change in ({"revision": "43"}, {"text": "The configuration is now different."},
                   {"retrieval_relevance": "irrelevant"}, {"checked_with": None},
                   {"kind": "tool_metadata"}, {"tool_outcome": "failed"}):
        assert reviewed_excerpt({**run, "evidence": [{**entry, **change}]}) is None


def test_explicit_reference_or_exact_source_sentence_can_be_formatted() -> None:
    doc = {"id": "conf:104", "title": "Relay", "read_document": True, "index_id": "kb",
           "text": "Получатель ждёт две секунды. Далее запрос отменяется."}
    run = {"mode": "autonomous", "evidence": [doc]}
    for text in ("Таймаут указан в карточке 'Relay'.", "Далее запрос отменяется."):
        result = format_source_reference(run, text)
        assert "[conf:104]" in result
        assert not answer_quality_issue(run, result)
    for text in ("Таймаут указан в карточке 'Other'.", "Далее запрос продолжается.",
                 "Таймаут указан в карточке 'Relay' [absent]."):
        assert format_source_reference(run, text) == text
    ambiguous = "Сведения приведены в карточке «Relay»."
    for evidence in ([doc, {**doc, "id": "second"}], [{**doc, "read_document": False}]):
        assert format_source_reference({**run, "evidence": evidence}, ambiguous) == ambiguous


def test_structured_answer_matches_only_literals_in_read_documents() -> None:
    import json

    evidence = {"id": "ref", "index_id": "kb", "kind": "source", "read_document": True,
                "text": "Лимит на один запрос — десять записей."}
    finding = {"source_id": "ref", "quote": evidence["text"]}
    run = {"evidence": [evidence]}
    assert render_quoted_findings(run, json.dumps({"findings": [finding]})) == (
        evidence["text"] + " [ref]"
    )
    for invalid in ({**finding, "source_id": "absent"}, {**finding, "quote": "Сорок записей."}):
        assert render_quoted_findings(run, json.dumps({"findings": [finding, invalid]})) is None
    for source in ({**evidence, "read_document": False}, {**evidence, "kind": "tool_metadata"}):
        assert render_quoted_findings({"evidence": [source]}, json.dumps({"findings": [
            finding,
        ]})) is None


@pytest.fixture
def run() -> dict[str, Any]:
    return {"mode": "autonomous", "evidence": [
        {"id": DOCUMENT, "index_id": "wiki", "text": "Таймаут 30 с.", "kind": "source"},
        {"id": "PAY-183", "index_id": "jira", "text": "Сбой устранён."},  # Legacy source.
        {"id": "tool-list", "kind": "tool_metadata", "text": "list_issues"},
        {"id": CHECKOUT, "index_id": "repo:api", "revision": "ref123", "text": "Repository api"},
        {"id": "empty", "kind": "source", "text": "  "},
    ]}


@pytest.mark.parametrize("answer", [
    "К сожалению, иногда генеративные модели могут создавать некорректные ответы [PAY-183].",
    "Генеративные модели иногда могут ошибаться. Проверьте информацию самостоятельно [PAY-183].",
    "Как языковая модель, я не имею личного мнения [PAY-183].",
    "Данный ответ модели не отражает официальную позицию разработчика [PAY-183].",
    "Мнения, выраженные моделью, не отражают точку зрения разработчика [PAY-183].",
    "## План исследования\n\n1. Изучить материалы [PAY-183].\n2. Сопоставить версии.",
    "Следующие шаги:\n\n1. Поиск регламента [PAY-183].\n2. Проверка условий.",
    "Проверю записи в журнале и свяжу события [PAY-183]. Затем соберу выводы.",
    "## Выводы\n\nИсточники: [PAY-183].",
])
def test_boilerplate_or_plan_is_not_a_finding_even_with_a_real_reference(
    run: dict[str, Any], answer: str,
) -> None:
    assert "результата" in (answer_quality_issue(run, answer) or "")


@pytest.mark.parametrize("answer", [
    f"30 с [{DOCUMENT}].",
    "Сбой устранён [PAY-183].",
    "## Сбой устранён [PAY-183].",
    "Проверьте значение `status` в карточке инцидента [PAY-183].",
    "План восстановления действует до конца месяца [PAY-183].",
    f"Языковые модели могут читать вложения через API [{DOCUMENT}].",
    f"Генеративные модели могут ошибаться, но таймаут зафиксирован в регламенте [{DOCUMENT}].",
    "Точная причина неизвестна. Jira фиксирует завершение инцидента [PAY-183].",
    "## Итог\n\nИсправлено [PAY-183].\n\n## План\n\n1. Проверить смежный сервис.",
    "Статус — `row['status']` [PAY-183]. [Инструкция](https://wiki.test/help).",
    f"Время подтверждено [{DOCUMENT}, PAY-183].",
    f"В журнале поле содержит `[not-a-reference]` [{DOCUMENT}].",
    "В журнале указано [PAY-183].\n\n```json\n[\"pending\"]\n```",
])
def test_concise_findings_and_caveats_are_accepted(run: dict[str, Any], answer: str) -> None:
    assert answer_quality_issue(run, answer) is None


@pytest.mark.parametrize("answer", [
    f"Таймаут 30 с [{DOCUMENT.lower()}].",
    f"Таймаут 30 с [{DOCUMENT}] [invented].",
    "Сбой устранён [PAY-183, FAKE].",
    f"Причина подтверждена [{CHECKOUT}].",
    "Причина подтверждена [tool-list].",
    "Причина подтверждена [empty].",
])
def test_every_reference_must_match_a_nonempty_source(run: dict[str, Any], answer: str) -> None:
    assert "ссылка" in (answer_quality_issue(run, answer) or "")


@pytest.mark.parametrize("answer", [
    "Сбой устранён.",
    "Пример идентификатора: `[PAY-183]`.",
    "Сбой устранён.\n\n```text\n[PAY-183]\n```",
    "Инструкция доступна [PAY-183](https://other.test/).",
])
def test_links_and_code_do_not_satisfy_source_check(run: dict[str, Any], answer: str) -> None:
    assert "идентификатор" in (answer_quality_issue(run, answer) or "")


@pytest.mark.parametrize("mode", ["quick", "investigate"])
def test_other_modes_keep_their_completion_contract(mode: str) -> None:
    assert answer_quality_issue({"mode": mode}, "Короткий ответ.") is None
    assert answer_quality_issue({"mode": mode}, "План: проверить систему.") is None
    assert answer_quality_issue(
        {"mode": mode}, "К сожалению, иногда генеративные модели могут создавать неточные ответы.",
    )
    assert answer_quality_issue(
        {"mode": mode}, "Мнение модели не отражает позицию компании. Платёж завершён.",
    ) is None


@pytest.mark.parametrize("mode", ["quick", "investigate", "autonomous"])
@pytest.mark.parametrize("suffix", [
    "Результат генерации не является официальным мнением компании [PAY-183].",
    "Сгенерированный текст не является выражением официальной позиции [bogus].",
])
def test_corporate_notice_with_a_citation_is_still_only_a_notice(
    run: dict[str, Any], mode: str, suffix: str,
) -> None:
    draft = "К сожалению, иногда генеративные модели могут создавать некорректные ответы. " + suffix
    assert "результата" in (answer_quality_issue({**run, "mode": mode}, draft) or "")
