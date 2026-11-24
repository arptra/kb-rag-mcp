"""Exercise corrective LangGraph routes, including runs with no RAG indexes."""

import json
from pathlib import Path
from typing import Any

import pytest
from test_harness_autonomy import OWNER, Model, build, finished

from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.integrations import ARGUMENTS
from agent_harness.research import lookup_key, next_external_lookup, next_source_lookup


class Repository:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "repo:billing", "name": "Billing"},
                {"id": "repo:other", "name": "Other"}]

    async def describe(self, principal: Principal, index_ids: list[str]) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {
            "name": name, "parameters": ARGUMENTS[name].model_json_schema(),
        }} for name in ("read_repository", "search_repository")]

    async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
        self.calls.append(name)
        assert kwargs["index_ids"] == ["repo:billing"]
        assert args["repository_id"] == "billing"
        assert name == "search_repository" and args["query"] == "PaymentService"
        code = (self.root / "config.py").read_text()
        lines = [f"config.py:{n}: {line}" for n, line in enumerate(code.splitlines(), 1)
                 if args["query"] in line]
        return [Evidence(id="source-line", title="config.py", text="\n".join(lines),
                         index_id="repo:billing")]

    async def close_run(self, run_id: str) -> None:
        pass


@pytest.mark.asyncio
async def test_invalid_planner_then_ignored_branch_reads_repository_and_finishes(
    tmp_path: Path,
) -> None:
    (tmp_path / "config.py").write_text("PaymentService = {'max_attempts': 4}\n")
    repository = Repository(tmp_path)

    class UnreliableModel(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            context = json.loads(kwargs["messages"][-1]["content"])
            if len(self.requests) == 1:
                raise ValueError("private malformed provider payload")
            if len(self.requests) == 2:
                steps = context["prior_tool_attempts"]
                assert any(step["name"] == "research_branch" for step in steps)
                return {"content": "Сначала проверю настройки.", "tool_calls": []}
            checked = next(item for item in context["evidence"] if item["id"] == "source-line")
            assert "max_attempts': 4" in checked["text"]
            return {"content": "Для PaymentService заданы четыре попытки [source-line].",
                    "tool_calls": []}

    model = UnreliableModel([])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5)
    runtime.tools = repository
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Сколько попыток делает PaymentService?", mode="autonomous",
            index_ids=["repo:billing"],
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert result["answer"].endswith("[source-line].")
        assert repository.calls == ["search_repository"] and len(model.requests) == 3
        assert result["research_plan"]["branches"] == [{
            "number": 1, "reason": "Планировщик вернул некорректное действие.",
            "subquestion": "Где в коде используется PaymentService?",
            "target": "repo:billing", "tool": "search_repository", "status": "checked",
        }]
        assert runtime._graph
        config = runtime._graph.config(OWNER, submitted["id"])
        snapshots = runtime._graph.graph.aget_state_history(config)
        assert ("replan",) in [snapshot.next async for snapshot in snapshots]
        assert "private malformed" not in json.dumps(result)
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_inventory_rejects_irrelevant_cited_answer_and_reads_twelfth_index(
    tmp_path: Path,
) -> None:
    indexes = [f"index-{n:02}" for n in range(12)]
    reads: list[str] = []
    searches: set[str] = set()

    class Indexes:
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": index, "name": index} for index in indexes]

        async def search(self, *, index_id: str, **kwargs: Any) -> list[Evidence]:
            searches.add(index_id)
            return [Evidence(id=index_id, title="A report", index_id=index_id,
                             text='{"services": ["gateway", "ledger"]}' if index_id == indexes[-1]
                             else "PaymentService прослушивает порт 12000.")]

        async def read(self, *, index_id: str, **kwargs: Any) -> Evidence:
            reads.append(index_id)
            assert index_id == indexes[-1]
            return Evidence(id=index_id, index_id=index_id, title="Graph export",
                            text='{"service_count":2, "services":["gateway","ledger"]}')

    wrong = {"content": "Найдено 12000 сервисов [index-00].", "tool_calls": []}
    valid = {"content": "Реестр содержит два сервиса — gateway и ledger [index-11].",
             "tool_calls": []}
    model = Model([wrong, wrong, valid])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, document_reads=0)
    runtime.source = Indexes()
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question="Сколько сервисов в системе?", mode="autonomous", index_ids=indexes,
        ))
        result = await finished(runtime, submitted["id"])
        assert result["status"] == "completed", result["warnings"]
        assert searches == set(indexes) and reads == [indexes[-1]]
        assert result["answer"] == valid["content"]
        assert result["metrics"]["rejected_drafts"] == 2
        assert result["research_plan"]["current_branch"]["target"] == indexes[-1]
    finally:
        await runtime.stop()


def test_catalog_fallback_only_inspects_live_permitted_mcp_and_its_returned_schema() -> None:
    definitions = [{"function": {"name": name, "parameters": {"properties": {
        "connection_id": {"enum": ["jira"]},
    }}}} for name in ("mcp_list_tools", "mcp_tool_schema", "mcp_call", "a2a_ask")]
    run = {"question": "Как обработали PAY-72?", "evidence": [],
           "index_ids": ["connection:other", "connection:jira"]}
    first = next_external_lookup(run, definitions, set())
    assert first == {"name": "mcp_list_tools", "args": {"connection_id": "jira"}}
    attempted = {lookup_key(first["name"], first["args"])}
    assert next_external_lookup(run, definitions, attempted) is None
    run["evidence"] = [{"index_id": "connection:jira", "kind": "tool_metadata",
                        "text": json.dumps({"kind": "tool_catalog_not_system_evidence",
                                            "tools": [{"name": "issue_lookup"}]})}]
    second = next_external_lookup(run, definitions, attempted)
    assert second == {"name": "mcp_tool_schema", "args": {
        "connection_id": "jira", "tool": "issue_lookup",
    }}
    attempted.add(lookup_key(second["name"], second["args"]))
    assert next_external_lookup(run, definitions, attempted) is None


@pytest.mark.parametrize("answer", [
    "Найдено 12000 сервисов [registry].", "12000 [registry].", "Число неизвестно [registry].",
    "Возможно, 2 сервиса [registry].",
])
def test_inventory_claim_must_match_its_cited_explicit_total(answer: str) -> None:
    from agent_harness.research import inventory_citation_issue

    run = {"question": "Сколько сервисов в системе?", "mode": "autonomous", "evidence": [
        {"index_id": "catalog", "id": "registry", "title": "Snapshot", "read_document": True,
         "text": '{"service_count":2}'},
    ]}
    assert inventory_citation_issue(run, answer)
    assert inventory_citation_issue(run, "Реестр содержит 2 сервиса [registry].") is None


def test_plain_words_in_unlinked_text_can_redirect_search_in_the_selected_index() -> None:
    evidence = [{"index_id": "one", "id": "detail", "title": "Обзор", "read_document": True,
                 "text": "Маяк обрабатывает поручения."}]
    forbidden = {**evidence[0], "index_id": "other", "text": "Внутренний материал."}
    call = next_source_lookup([forbidden, *evidence], ["one"], set(), "Кто обрабатывает поручения?")
    assert call == {"name": "search_knowledge", "args": {"index_id": "one",
                                                        "query": "Маяк поручения"}}


@pytest.mark.asyncio
async def test_unknown_remote_result_is_not_replayed_after_new_evidence(tmp_path: Path) -> None:
    repeated = {"name": "mcp_call", "args": {
        "connection_id": "jira", "tool": "lookup", "arguments": {"key": "PAY-72"},
    }}
    search = {"name": "search_knowledge", "args": {"index_id": "a", "query": "PAY-72"}}
    model = Model([{"tool_calls": [repeated]}, {"tool_calls": [search]},
                   {"tool_calls": [repeated]}, {"content": "Регламент найден [PAY-72]."}])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, tool_calls=7)

    class UncertainTool:
        def __init__(self) -> None:
            self.calls = 0

        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": "connection:jira", "name": "MCP"}]

        async def describe(
            self, principal: Principal, index_ids: list[str],
        ) -> list[dict[str, Any]]:
            return [{"function": {"name": "mcp_call", "parameters": {}}}]

        async def call(self, name: str, args: Any, **kwargs: Any) -> list[Evidence]:
            assert name == "mcp_call" and args == repeated["args"]
            self.calls += 1
            raise TimeoutError

        async def close_run(self, run_id: str) -> None:
            pass

    tools = UncertainTool()
    runtime.tools = tools
    await runtime.start()
    try:
        requested = await runtime.submit(OWNER, RunRequest(
            question="Проверь обработку платежа", mode="autonomous",
            index_ids=["a", "connection:jira"],
        ))
        result = await finished(runtime, requested["id"])
        assert tools.calls == 1
        assert any(item.get("checked_with") == "search_knowledge" for item in result["evidence"])
        assert any(step["name"] == "mcp_call" and "неизвестен" in step["result"]
                   for step in result["research_trace"])
    finally:
        await runtime.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("weak_step", ["invalid_plan", "invalid_budget", "repeated_search"])
async def test_missed_document_reforms_the_query_then_opens_the_actual_card(
    tmp_path: Path, weak_step: str,
) -> None:
    question = "Как Лотос принимает запросы и сколько ждёт?"
    overview = "Лотос принимает запросы по HTTP. Параметр read_timeout указан в карточке relay."
    searches: list[str] = []
    reads: list[str] = []

    class Cards:
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": "cards", "name": "Cards"}]

        async def search(self, *, query: str, index_id: str, **_: Any) -> list[Evidence]:
            searches.append(query)
            if query == question:
                return [Evidence(id="overview", title="Обзор", text=overview, index_id=index_id)]
            assert query == "relay"
            return [Evidence(id="k-2026-42", title="Relay",
                             text="Настройки приёма запросов.", index_id=index_id)]

        async def read(self, *, document_id: str, index_id: str, **_: Any) -> Evidence:
            reads.append(document_id)
            text = {"overview": overview, "k-2026-42": "read_timeout = 17 секунд."}[document_id]
            return Evidence(id=document_id, title=document_id, text=text, index_id=index_id)

    def call(name: str, **args: Any) -> dict[str, Any]:
        return {"content": "", "tool_calls": [{"name": name, "args": args}]}

    class UnreliablePlanner(Model):
        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.requests.append(kwargs)
            context = json.loads(kwargs["messages"][-1]["content"])
            if len(self.requests) == 1:
                return call("read_document", index_id="cards", document_id="invented-id")
            if len(self.requests) == 2:
                assert any(item["id"] == "k-2026-42" for item in context["evidence"])
                if weak_step == "repeated_search":
                    return call("search_knowledge", index_id="cards", query="relay")
                if weak_step == "invalid_budget":
                    return call("request_budget", source_calls=5)
                return call("update_research_plan", nonsense=True)
            card = next(e for e in context["evidence"] if e["id"] == "k-2026-42")
            assert card["read_document"] and "17" in card["text"]
            return {"content": "Время ожидания Лотоса — 17 секунд [k-2026-42].", "tool_calls": []}

    model = UnreliablePlanner([])
    runtime = build(tmp_path, model, model_calls=6, source_calls=5, tool_calls=8, document_reads=1)
    runtime.source = Cards()
    await runtime.start()
    try:
        submitted = await runtime.submit(OWNER, RunRequest(
            question=question, mode="autonomous", index_ids=["cards"],
        ))
        result = await finished(runtime, submitted["id"])
        assert searches == [question, "relay"]
        assert reads == ["overview", "invented-id", "k-2026-42"]
        assert result["answer"].endswith("17 секунд [k-2026-42].")
        assert result["status"] == "completed", result["warnings"]
        assert result["metrics"]["model_calls"] == 3
        assert "relay" in result["research_plan"]["branches"][0]["subquestion"]
    finally:
        await runtime.stop()
