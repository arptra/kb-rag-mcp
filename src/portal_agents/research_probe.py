"""Exercise a draft model with a disposable search and a document available only by its ID."""
# ruff: noqa: RUF001 -- Russian interface text.
from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from random import SystemRandom
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import tool

from portal_agents.engine import Authorize, run_turn
from portal_agents.model_errors import ModelGatewayError, gateway_exception

_TIME_LIMIT = 180
_CALL_LIMIT = 8
_QUESTION = (
    "В тестовом индексе найди журнал «Проверка исследования» с помощью search_index. "
    "Открой найденный документ через read_document по его идентификатору. "
    "Какой контрольный код у последней по дате завершённой записи? Назови один код."
)
_CODE = re.compile(r"(?<![\w-])QA-[0-9A-F]{12}(?![\w-])", re.IGNORECASE)


class _CheckStopped(ValueError):
    """A diagnostic limit or verification failed; the message is safe to display."""


async def research_events(
    connection: AbstractAsyncContextManager[BaseChatModel], authorize: Authorize,
    log_failure: Callable[[ModelGatewayError], str],
) -> AsyncIterator[tuple[str, str]]:
    expected_document = "doc-" + uuid4().hex
    random = SystemRandom()
    earlier, latest, planned = [f"QA-{n:012X}" for n in random.sample(range(1 << 48), 3)]
    entries = [
        f"2025-12-23 | Завершена | {earlier}",
        f"2026-01-09 | Завершена | {latest}",
        f"2026-02-02 | Запланирована | {planned}",
    ]
    random.shuffle(entries)
    journal = "Журнал проверочных работ\nДата | Статус | Код\n" + "\n".join(entries)
    found = opened = False
    calls = 0
    events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    def stage() -> str:
        if opened:
            return "3/3 · Проверка вывода."
        if found:
            return "2/3 · Чтение найденного документа."
        return "1/3 · Поиск тестовой записи."

    @tool
    async def search_index(query: str) -> dict[str, str]:
        """Search the diagnostic index and return the matching document ID."""
        nonlocal found
        if not query.strip():
            return {"error": "Введите поисковый запрос."}
        found = True
        events.put_nowait(("progress", stage()))
        return {"document_id": expected_document, "title": "Проверка исследования"}

    @tool
    async def read_document(document_id: str) -> dict[str, str]:
        """Read the document with the exact ID returned by search_index."""
        nonlocal opened
        if not found or document_id != expected_document:
            return {"error": "Используйте идентификатор документа из результатов поиска."}
        opened = True
        events.put_nowait(("progress", stage()))
        return {"content": journal}

    async def progress(kind: str, _: str) -> None:
        nonlocal calls
        if kind == "model":
            if calls >= _CALL_LIMIT:
                raise _CheckStopped("Достигнут предел обращений к модели.")
            calls += 1
            message = f"{stage()} Обращение к модели {calls} из {_CALL_LIMIT}."
            events.put_nowait(("progress", message))

    async def execute() -> None:
        try:
            async with asyncio.timeout(_TIME_LIMIT), connection as model:
                answer = await run_turn(model, _QUESTION, [search_index, read_document],
                                        on_event=progress, authorize=authorize, recursion_limit=50)
            if not found or not opened:
                raise _CheckStopped("Модель завершила проверку до получения документа.")
            # Only the choice among this fixture's records is under test, independently of links.
            answer_text = re.sub(r"https?://[^\s<>()]+", "", answer, flags=re.IGNORECASE)
            selected = {code.upper() for code in _CODE.findall(answer_text)} & {
                earlier, latest, planned,
            }
            if selected != {latest}:
                if len(selected) > 1:
                    reason = "В ответе перечислены коды нескольких записей из журнала."
                elif earlier in selected:
                    reason = "Выбрана более ранняя завершённая запись."
                elif planned in selected:
                    reason = "Выбрана запланированная запись."
                else:
                    reason = "В ответе отсутствует код записи из журнала."
                raise _CheckStopped("Подключение и чтение работают. " + reason)
            events.put_nowait(("success", "Агент нашёл журнал и вернул код последней "
                                          "завершённой записи. "
                                          f"Обращений к модели — {calls}."))
            return
        except _CheckStopped as exc:
            detail = str(exc)
        except TimeoutError:
            detail = f"Истекло время проверки ({_TIME_LIMIT} сек.)."
        except PermissionError:
            detail = "Доступ к проверке или ответу ограничен."
        except Exception as exc:
            failure = gateway_exception(exc)
            if failure is None:
                detail = "Проверка прервалась до получения результата."
            else:
                reference = log_failure(failure)
                detail = f"{failure} Номер ошибки {reference}."
        events.put_nowait(("error", f"{stage()} {detail}"))

    task = asyncio.create_task(execute())
    try:
        yield "progress", stage()
        while True:
            try:
                kind, text = await asyncio.wait_for(events.get(), timeout=10)
            except TimeoutError:
                if task.done():
                    yield "error", f"{stage()} Проверка прервалась до получения результата."
                    return
                yield "waiting", ""
                continue
            yield kind, text
            if kind in {"success", "error"}:
                return
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
