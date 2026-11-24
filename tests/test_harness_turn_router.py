"""Routing has no source adapter and can only return a reply or hand off to research."""

# ruff: noqa: RUF001 -- Russian conversational examples.
import json
from typing import Any

import pytest

from agent_harness.contracts import ModelDecisionError
from agent_harness.turn_router import local_reply, needs_model_route, route_turn


@pytest.mark.parametrize("question", ["Привет, как дела?! 👋", "Добрый день!", "Hey, how are you?"])
def test_only_a_complete_social_turn_has_a_local_answer(question: str) -> None:
    assert local_reply(question)
    assert local_reply(question + " Проверь задачу PAY-55.") is None


@pytest.mark.parametrize("question", [
    "Объясни простыми словами, что такое Jira",
    "Переведи на английский: «привет, проверь задачу PAY-42»",
    "Сделай покороче", "Ты тут? Есть вопрос", "Поговорим о погоде?",
])
def test_ambiguous_auto_requests_are_model_decisions(question: str) -> None:
    assert needs_model_route(question, automatic=True)
    assert needs_model_route(question, after_chat=True)
    assert not needs_model_route("Привет! Проверь статус заявки PAY-42", automatic=True)


@pytest.mark.parametrize("choice", ["chat", "research"])
async def test_port_receives_one_local_schema_and_the_relevant_conversation(choice: str) -> None:
    async def decide(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        assert [tool["function"]["name"] for tool in tools] == ["RouteTurn"]
        assert json.loads(messages[1]["content"]) == {
            "message": "Покороче?", "previous_question": "Что такое план?",
            "previous_answer": "План — это последовательность действий.",
        }
        args = {"route": choice,
                "answer": "Последовательность действий." if choice == "chat" else ""}
        return {"tool_calls": [{"name": "RouteTurn", "args": args}]}

    answer = await route_turn(
        "Покороче?", previous_question="Что такое план?",
        previous_answer="План — это последовательность действий.", decide=decide,
    )
    assert answer == ("Последовательность действий." if choice == "chat" else None)


async def test_research_followup_does_not_reveal_previous_values_to_the_router() -> None:
    async def decide(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        assert json.loads(messages[1]["content"]) == {"message": "А когда?",
                                                     "previous_turn": "research"}
        return {"content": '{"route": "research"}'}

    assert await route_turn(
        "А когда?", previous_question="private question", previous_answer="private answer",
        research_followup=True, decide=decide,
    ) is None


@pytest.mark.parametrize("bad", [
    {"content": '{"route":"research"'},
    {"content": '{"route":"research"}', "finish_reason": "length"},
    {"content": '{"route":"other"}'}, {"content": '{"route":"chat","answer":"  "}'},
    {"content": '{"route":"chat","answer":"У меня нет личного мнения."}'},
    {"tool_calls": [{"name": "mcp_call", "args": {}}]},
])
async def test_malformed_decision_does_not_authorize_research(bad: dict[str, Any]) -> None:
    async def decide(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        return bad

    with pytest.raises(ModelDecisionError):
        await route_turn("What is a queue?", previous_question=None, decide=decide)
