"""Choose whether a chat turn needs live system data before any source is opened."""

# ruff: noqa: RUF001 -- Russian chat and gateway instructions.
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_harness.contracts import ModelDecisionError, ModelToolFormatError
from agent_harness.model_connection import check_model_reply

RouteDecide = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[dict[str, Any]]]


class InvalidRouteDecision(ModelDecisionError):
    """Only the requested routing schema was invalid. No authorization was granted."""


class RouteTurn(BaseModel):
    """Choose research when the request needs private system facts or an action."""

    model_config = ConfigDict(extra="forbid", strict=True)
    route: Literal["chat", "research"]
    answer: str = Field(default="", max_length=4000)


_ROUTE_TOOL = convert_to_openai_tool(RouteTurn)
_PARSER = PydanticOutputParser[RouteTurn](pydantic_object=RouteTurn)
_PROMPT = (
    "Выбери маршрут для всего сообщения и используй RouteTurn либо JSON того же формата. "
    "chat: беседа, общий вопрос, помощь с предоставленным текстом. Сразу дай ответ "
    "на языке собеседника. research: текущее или прошлое состояние нашей АС, её архитектура, "
    "документы, задачи, жалобы, код, инструменты, просьба что-то выполнить или проверить. "
    "Для research оставь answer пустым. Если previous_turn=research, короткое продолжение "
    "вроде «а когда?» или «и почему?» относится к прежнему исследованию. "
    "Явную смену темы оцени заново. Предыдущая публичная беседа помогает понять продолжение. "
    "Приветствие перед задачей не отменяет задачу. При сомнении в необходимости источника "
    "выбирай research. В этой операции нет доступа к источникам или действиям."
)
_HELLO = r"(?:привет(?:ик)?|здравствуй(?:те)?|добр(?:ый (?:день|вечер)|ое утро)|здарова)"
_HOW = r"(?:как (?:у тебя |у вас )?дела|как (?:ты|поживаешь|поживаете)|ты (?:тут|здесь))"
_HELLO_EN = r"(?:hi|hello|hey|good (?:morning|afternoon|evening))"
_HOW_EN = r"(?:how are you(?: doing)?|how['’]s it going|are you (?:there|here))"


def local_reply(question: str) -> str | None:
    """Instant replies only when the *whole* message is an ordinary social turn."""
    words = re.sub(r"[\s,!.?;:…—–👋🙂😊]+", " ", question.casefold()).strip()
    if re.fullmatch(_HELLO, words):
        return "Привет! Чем могу помочь?"
    if re.fullmatch(rf"(?:{_HELLO} )?{_HOW}", words):
        return "Привет! Я на связи. Чем могу помочь?"
    if words in {"спасибо", "большое спасибо", "спасибо большое", "благодарю"}:
        return "Пожалуйста!"
    if words in {"пока", "до свидания", "до встречи"}:
        return "До встречи!"
    if re.fullmatch(_HELLO_EN, words):
        return "Hi! How can I help?"
    if re.fullmatch(rf"(?:{_HELLO_EN} )?{_HOW_EN}", words):
        return "Hi! I'm here and ready to help."
    if words in {"thanks", "thank you", "thank you very much"}:
        return "You're welcome!"
    if words in {"bye", "goodbye", "see you"}:
        return "See you!"
    return None


# A record reference, link, or explicit request to consult an internal source is already
# actionable. Spend a routing call only where an ordinary conversation is plausible.
_LOOKUP = re.compile(
    r"https?://|\b[A-Za-z][A-Za-z0-9]+-\d+\b|\b(?:mcp|jira|confluence|репозитори\w*|"
    r"индекс\w*|жир[еуы]|конфлюенс\w*|(?:нашей?|нашу|наша) (?:ас|систем\w*))\b",
    re.I,
)
_CONVERSATION = re.compile(
    rf"\b(?:{_HELLO}|{_HELLO_EN}|{_HOW}|{_HOW_EN}|ты|тебе|тобой|тебя|спасибо|поговорим|"
    r"давай (?:поговорим|поболтаем)|расскажи|придумай|напиши|переведи|перефразируй|"
    r"сформулируй|сочини|объясни (?:простыми словами|разниц\w*)|что такое|"
    r"what is|can you|how are|tell me|translate|rewrite|brainstorm|write (?:a|an))\b",
    re.I,
)
_NEW_TOPIC = re.compile(
    rf"\b(?:{_HELLO}|{_HELLO_EN}|поговорим|поболтаем|другая тема|смени\w* тему|переведи|"
    r"перефразируй|сочини|new topic|change the subject|translate|rewrite)\b", re.I,
)
_DIRECT_LOOKUP = re.compile(
    rf"^\s*(?:(?:{_HELLO}|{_HELLO_EN})\b[\s,!:.—-]+)?"
    r"(?:пожалуйста[\s,]+|please\s+)?"
    r"(?:найди|проверь|открой|прочти|прочитай|сверь|покажи|узнай|"
    r"check|open|read|find|look up|show)\s+"
    r"(?:(?:в|из|статус|карточку|содержимое|status of|in|the)\s+){0,2}"
    r"(?:(?:заявк[уи]|задач[уи]|тикет[а]?|issue|ticket)\s+)?"
    r"(?:[a-z][a-z0-9]+-\d+\b|https?://|(?:jira|confluence|mcp)\b)", re.I,
)


def needs_model_route(
    question: str, *, followup: bool = False, after_chat: bool = False, automatic: bool = False,
) -> bool:
    """Give ambiguous conversational turns to the model, retain ongoing research by default."""
    if automatic or after_chat:
        return not _DIRECT_LOOKUP.match(question)
    if _LOOKUP.search(question):
        return False
    return bool((_NEW_TOPIC if followup else _CONVERSATION).search(question))


async def route_turn(
    question: str, *, previous_question: str | None, decide: RouteDecide,
    previous_answer: str | None = None, research_followup: bool = False,
    repair_format: bool = False,
) -> str | None:
    """Return a complete conversational answer or hand the request to research."""
    data = {"message": question}
    if research_followup:
        data["previous_turn"] = "research"
    elif previous_question:
        data["previous_question"] = previous_question[:600]
        if previous_answer:
            data["previous_answer"] = previous_answer[:1800]
    prompt = _PROMPT
    if repair_format:
        prompt += (' Верни один вызов RouteTurn или один завершённый JSON. Примеры формы: '
                   '{"route":"chat","answer":"<готовый ответ>"} или {"route":"research"}.')
    try:
        decision = await decide(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": json.dumps(data, ensure_ascii=False)}], [_ROUTE_TOOL],
        )
    except ModelToolFormatError as exc:
        raise InvalidRouteDecision("Не удалось прочитать структуру решения.") from exc
    reason = decision.get("finish_reason")
    check_model_reply(decision, reason)
    text = decision.get("content")
    if (not decision.get("tool_calls") and isinstance(text, str) and text.strip()
            and text.lstrip()[0] not in "{[`"):
        # A prose response may be a provider notice. Only retry structured output.
        raise ModelDecisionError("Не удалось определить маршрут сообщения.")
    if reason in {"max_tokens", "max_output_tokens", "length"}:
        raise InvalidRouteDecision("Ответ при выборе маршрута оборвался.")
    if decision.get("finish_reason") not in {
        None, "", "stop", "end_turn", "tool_calls", "tool_use",
    } or decision.get("incomplete_tool_calls"):
        raise ModelDecisionError("Ответ при выборе маршрута оборвался.")
    calls = decision.get("tool_calls") or []
    try:
        if calls:
            if (not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict)
                    or calls[0].get("name") != RouteTurn.__name__):
                raise InvalidRouteDecision("Не удалось определить маршрут сообщения.")
            candidate = calls[0].get("args")
        else:
            content = decision.get("content")
            if not isinstance(content, str):
                raise InvalidRouteDecision("Не удалось определить маршрут сообщения.")
            # LangChain's parser can repair incomplete streaming JSON. Full validation
            # here ensures an unfinished response cannot initiate a source lookup.
            block = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content.strip(), re.I | re.S)
            body = block[1] if block else content.strip()
            candidate = json.loads(body)
        if isinstance(candidate, dict) and isinstance(candidate.get("answer"), str):
            check_model_reply({"content": candidate["answer"]}, "stop")
        route = RouteTurn.model_validate(candidate) if calls else _PARSER.parse(body)
    except (OutputParserException, ValidationError, json.JSONDecodeError):
        raise InvalidRouteDecision("Не удалось определить маршрут сообщения.") from None
    if route.route == "chat":
        if answer := route.answer.strip():
            return answer
        raise InvalidRouteDecision("Для обычной беседы нужен текст ответа.")
    return None
