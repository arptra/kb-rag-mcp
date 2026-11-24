"""Exercise focused and widening retrieval through the normal graph and model interface."""
# ruff: noqa: RUF001 -- test questions and documents use Russian text.

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_harness_runtime import OWNER, finished, settings

from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime

QUESTION = "Какой режим обработки сейчас выбран для заказов?"
ALTERNATE = "журнал переключений профиля"
FACT = "На всех рабочих узлах активирован профиль Парус."


class Records:
    supports_passage_retrieval = True

    def __init__(self, ids: list[str]) -> None:
        self.ids = ids
        self.queries: list[tuple[str, str]] = []

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": index, "name": "Архив"} for index in self.ids]

    async def search(self, *, index_id: str, query: str, **kwargs: Any) -> list[Evidence]:
        self.queries.append((index_id, query))
        if index_id == "k01" and query == ALTERNATE:
            return [Evidence(id="entry", title="Приложение Б", text=FACT, index_id=index_id)]
        return [Evidence(id="training-" + index_id, title="Режим обработки заказов",
                         text="Расписание обучения сотрудников.", index_id=index_id)]

    async def read(self, *, index_id: str, document_id: str, **kwargs: Any) -> Evidence:
        return Evidence(id=document_id, index_id=index_id, title="Материалы",
                        text=FACT if document_id == "entry" else "Расписание обучения сотрудников.")


class Reviewer:
    supports_search_assessment = True

    def __init__(self, *, grounded: bool = True) -> None:
        self.grounded = grounded
        self.reviews: list[dict[str, Any]] = []

    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        if any(tool["function"]["name"] == "assess_passages" for tool in kwargs["tools"]):
            data = json.loads(kwargs["messages"][-1]["content"])
            self.reviews.append(data)
            hit = next((p["number"] for p in data["passages"] if FACT in p["text"]), 0)
            if not self.grounded:
                args = {"passage": 1, "quote": "Выдуманный результат без подтверждения."}
            elif hit:
                args = {"passage": hit, "quote": FACT}
            else:
                args = {"query": ALTERNATE, "indexes": ["outside-user-scope", "k01"],
                        "irrelevant_passages": [p["number"] for p in data["passages"]]}
            return {"tool_calls": [{"name": "assess_passages", "args": args}]}
        return {"content": FACT + " [entry]" if self.grounded else
                "Доступно расписание обучения [training-k00].", "tool_calls": []}


@pytest.mark.parametrize("mode", ["quick", "investigate", "autonomous"])
async def test_rewrite_checks_body_and_finishes_without_fanning_out(
    tmp_path: Path, mode: str,
) -> None:
    source, model = Records([f"k{i:02}" for i in range(12)]), Reviewer()
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        request = RunRequest.model_validate({"question": QUESTION, "mode": mode})
        result = await finished(runtime, (await runtime.submit(OWNER, request))["id"])
        assert result["status"] == "completed" and FACT in result["answer"], result["warnings"]
        assert source.queries == [("k00", QUESTION), ("k01", QUESTION), ("k01", ALTERNATE)]
        assert result["coverage"]["k11"]["status"] == "deferred"
        assert len(model.reviews) == 2
        assert model.reviews[-1]["passages"][0]["text"] == FACT
    finally:
        await runtime.stop()


async def test_single_opaque_index_can_be_queried_again(tmp_path: Path) -> None:
    source, model = Records(["k01"]), Reviewer()
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        request = RunRequest(question=QUESTION)
        result = await finished(runtime, (await runtime.submit(OWNER, request))["id"])
        assert result["status"] == "completed" and "Парус" in result["answer"]
        assert source.queries == [("k01", QUESTION), ("k01", ALTERNATE)]
    finally:
        await runtime.stop()


async def test_latest_completed_change_compares_waves_and_repairs_planned_citation(
    tmp_path: Path,
) -> None:
    question = "Какой релиз последним установили в рабочей системе?"
    alternate = "акты переключения продуктивного трафика"
    answer = "Последней установили версию 4.2 18 сентября 2026 года"
    entries = {
        "earlier": Evidence(id="earlier", index_id="k00", title="Протокол № 14",
                            text="10 августа 2026 года в рабочую среду установили версию 3.8."),
        "installed": Evidence(id="installed", index_id="k06", title="Смена № 27",
                              text="18 сентября 2026 года завершена установка версии 4.2. "
                                   "Переключение трафика подтверждено дежурным."),
        "proposal": Evidence(id="proposal", index_id="k06", title="Окно № 31",
                             text="На 20 октября 2026 года запланирована версия 4.9. "
                                  "Работы ещё не согласованы и не выполнялись."),
    }

    class Chronicle(Records):
        async def search(self, *, index_id: str, query: str, **kwargs: Any) -> list[Evidence]:
            self.queries.append((index_id, query))
            if index_id == "k06" and query == alternate:
                return [entries["installed"], entries["proposal"]]
            return [entries["earlier"]] if index_id == "k00" else []

        async def read(self, *, index_id: str, document_id: str, **kwargs: Any) -> Evidence:
            return entries[document_id]

    class TimelineReviewer(Reviewer):
        drafts = 0

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            if any(tool["function"]["name"] == "assess_passages" for tool in kwargs["tools"]):
                data = json.loads(kwargs["messages"][-1]["content"])
                self.reviews.append(data)
                passages = {p["text"]: p["number"] for p in data["passages"]}
                if all(e.text in passages for e in entries.values()):
                    actual = entries["installed"].text
                    args = {"passage": passages[actual], "quote": actual}
                else:
                    args = {"gap": "Нужны более поздние записи о выполненных работах.",
                            "query": alternate, "indexes": ["k06"]}
                return {"tool_calls": [{"name": "assess_passages", "args": args}]}
            self.drafts += 1
            citation = "proposal" if self.drafts == 1 else "installed"
            return {"content": f"{answer} [{citation}]."}

    source, model = Chronicle([f"k{i:02}" for i in range(12)]), TimelineReviewer()
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        run = await runtime.submit(OWNER, RunRequest(question=question, mode="quick"))
        result = await finished(runtime, run["id"])
        assert source.queries == [("k00", question), ("k01", question), ("k06", alternate)]
        assert len(model.reviews) == 2
        assert {p["text"] for p in model.reviews[0]["passages"]} == {entries["earlier"].text}
        comparison = {p["text"] for p in model.reviews[1]["passages"]}
        assert comparison == {e.text for e in entries.values()}
        assert result["coverage"]["k11"]["status"] == "deferred"
        assert model.drafts == 2
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"] == answer + " [installed]."
    finally:
        await runtime.stop()


async def test_rank_full_authorized_catalog_before_applying_source_cap(tmp_path: Path) -> None:
    class Catalog(Records):
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": i, "name": QUESTION if i == "k01" else ""} for i in self.ids]

    source = Catalog(["aaa", "bbb", "ccc", "k01"])
    runtime = HarnessRuntime(settings(tmp_path, max_indexes=1), source, Reviewer())
    await runtime.start()
    try:
        run = await runtime.submit(OWNER, RunRequest(question=QUESTION))
        result = await finished(runtime, run["id"])
        assert result["index_ids"] == ["k01"]
        assert source.queries == [("k01", QUESTION), ("k01", ALTERNATE)]
        assert result["answer"].startswith(FACT)
    finally:
        await runtime.stop()


async def test_invalid_support_widens_until_allowed_scope_is_covered(tmp_path: Path) -> None:
    source, model = Records([f"k{i:02}" for i in range(12)]), Reviewer(grounded=False)
    runtime = HarnessRuntime(settings(tmp_path), source, model)
    await runtime.start()
    try:
        allowed = source.ids[:-1]
        request = RunRequest(question=QUESTION, index_ids=allowed)
        result = await finished(runtime, (await runtime.submit(OWNER, request))["id"])
        assert {index for index, _ in source.queries} == set(allowed)
        assert all(item["status"] == "searched" for item in result["coverage"].values())
        assert len(model.reviews[0]["passages"]) <= 2
        assert not any(step["name"] == "source_supported" for step in result["research_trace"])
    finally:
        await runtime.stop()


@pytest.mark.parametrize("include_sandbox", [False, True])
async def test_colleague_rechecks_learned_document_in_mixed_scope(
    tmp_path: Path, include_sandbox: bool,
) -> None:
    class OtherTools:
        supports_read_only_mcp = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            indexes = [{"id": "connection:issues", "name": "Журнал задач", "kind": "mcp"}]
            if include_sandbox:
                indexes.append({"id": "sandbox-api:~", "name": "Среда", "kind": "sandbox"})
            return indexes

        async def describe(self, principal: Principal, ids: list[str], **kwargs: Any) -> list:
            tools = [{"type": "function", "function": {"name": "mcp_list_tools",
                      "parameters": {"properties": {"connection_id": {"enum": ["issues"]}}}}}]
            if "sandbox-api:~" in ids:
                tools.append({"type": "function", "function": {
                    "name": "check_sandbox_api", "parameters": {"properties": {}},
                }})
            return tools

        async def call(self, name: str, args: dict, **kwargs: Any) -> list:
            self.calls.append(name)
            if name == "check_sandbox_api":
                return [Evidence(id="health", index_id="sandbox-api:~", title="HTTP", text="200")]
            return []

        async def close_run(self, run_id: str) -> None:
            pass

    source, model, tools = Records([f"k{i:02}" for i in range(12)]), Reviewer(), OtherTools()
    runtime = HarnessRuntime(settings(tmp_path), source, model, tools=tools)
    await runtime.start()
    try:
        results = []
        request = RunRequest(question=QUESTION, mode="investigate")
        for owner in (OWNER, Principal("colleague")):
            source.queries.clear()
            run_id = (await runtime.submit(owner, request))["id"]
            async with asyncio.timeout(5):
                while (run := await runtime.get(owner, run_id))["phase"] != "finished":
                    await asyncio.sleep(0.01)
            assert run["status"] == "completed" and FACT in run["answer"], run["warnings"]
            results.append((list(source.queries), run["metrics"]["document_reads"],
                            run["metrics"]["model_calls"]))
        assert results == [
            ([("k00", QUESTION), ("k01", QUESTION), ("k01", ALTERNATE)], 2, 3),
            ([("k01", QUESTION)], 1, 2),
        ]
        assert tools.calls == (["check_sandbox_api"] * 2 if include_sandbox else [])
    finally:
        await runtime.stop()
