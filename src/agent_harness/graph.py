"""Durable LangGraph orchestration with private threads and conservative tool recovery."""

# ruff: noqa: RUF001  # Russian user-facing status and diagnostics.

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, Any, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from agent_harness.answer_quality import (
    is_answer_source,
    requested_field,
    requested_field_label,
    reviewed_excerpt,
)
from agent_harness.budgets import BUDGET_TOOL, BudgetChanges, BudgetRequest
from agent_harness.contracts import (
    ConversationUnavailable,
    ModelDecisionError,
    Principal,
    sandbox_api_index,
)
from agent_harness.material_archive import ARCHIVE_TOOL, search_materials
from agent_harness.material_archive import available as archive_available
from agent_harness.mcp_catalog import (
    METADATA,
    all_mcp_tools_inspected,
    linked_read,
    manifest_needs_refresh,
    native_read_tools,
    question_record_id,
    question_record_lookup,
    quick_schema_reads,
    read_chat,
    record_metadata,
    repair_call,
    schema_key,
    text_read_call,
)
from agent_harness.model_connection import (
    RETRYABLE_FINISH_REASONS,
    ModelRouter,
    ModelTransientError,
)
from agent_harness.policy import TOOLS, validate_call
from agent_harness.research import (
    PLAN_TOOL,
    branch_question,
    evidence_signature,
    initial_search_query,
    initialize_plan,
    inventory_source_score,
    lookup_key,
    needs_source_check,
    next_external_lookup,
    next_source_lookup,
    service_inventory_question,
    simple_service_count_question,
    update_plan,
)
from agent_harness.retrieval_review import body_overlap
from agent_harness.store import timestamp

if TYPE_CHECKING:
    from agent_harness.runtime import HarnessRuntime

_EFFECTFUL = {"run_in_sandbox", "mcp_call", "a2a_ask"}


class GraphState(TypedDict, total=False):
    run: dict[str, Any]
    next_step: str
    knowledge_ids: list[str]
    bootstrap: list[str]
    external: list[dict[str, Any]]
    calls: list[Any]
    exchange: list[dict[str, Any]]
    attempted_lookups: list[str]
    finish_after_sandbox_check: bool
    recovery_lookup: bool
    quality_rounds: int
    branch_reason: str
    branch_waiting: bool
    force_lookup: bool
    agent_batch: bool


@dataclass
class ExecutionContext:
    principal: Principal
    run: dict[str, Any]
    cancel: Event
    deadline: float
    started: float = 0


class HarnessGraph:
    def __init__(self, engine: HarnessRuntime, checkpointer: AsyncSqliteSaver) -> None:
        self.engine = engine
        self.checkpointer = checkpointer
        builder = StateGraph(GraphState, context_schema=ExecutionContext)
        builder.add_node("prepare", self.prepare)
        builder.add_node("plan", self.plan)
        builder.add_node("collect", self.collect)
        builder.add_node("inspect", self.inspect)
        builder.add_node("decide", self.decide)
        builder.add_node("tools", self.tools)
        builder.add_node("reflect", self.reflect)
        builder.add_node("replan", self.replan)
        builder.add_node("synthesize", self.synthesize)
        builder.add_edge(START, "prepare")
        routed = ("prepare", "plan", "collect", "inspect", "decide", "tools", "reflect", "replan")
        for name in routed:
            builder.add_conditional_edges(
                name,
                self.route,
                {
                    "plan": "plan",
                    "collect": "collect",
                    "inspect": "inspect",
                    "decide": "decide",
                    "tools": "tools",
                    "reflect": "reflect",
                    "replan": "replan",
                    "synthesize": "synthesize",
                    "done": END,
                },
            )
        builder.add_edge("synthesize", END)
        self.graph = builder.compile(checkpointer=checkpointer, name="harness-investigation")

    @staticmethod
    def thread_id(principal: Principal, run_id: str) -> str:
        owner = hashlib.sha256(principal.owner_id.encode()).hexdigest()[:32]
        return f"harness:{owner}:{run_id}"

    def config(self, principal: Principal, run_id: str) -> RunnableConfig:
        return {
            "configurable": {"thread_id": self.thread_id(principal, run_id)},
            "recursion_limit": self.engine.control_store.load().runtime.graph_recursion_limit,
        }

    async def execute(self, context: ExecutionContext, *, resume: bool = False) -> None:
        context.started = context.deadline - self.engine._run_timeout(context.run)
        config = self.config(context.principal, context.run["id"])
        config["recursion_limit"] = self.engine._runtime_limits(context.run).graph_recursion_limit
        state: GraphState | None = {"run": copy.deepcopy(context.run)}
        if resume:
            context.run.pop("_question_bound_mcp_call", None)
            context.run.pop("_goal_review_requested", None)
            # A resumed graph searches the current corpus. A quote, negative judgment or
            # remembered page from its interrupted pass must be checked against that corpus.
            for field in ("_retrieval_support", "_retrieval_gap", "_source_assessments",
                          "_routing_reads", "_search_hit_ids"):
                context.run.pop(field, None)
            snapshot = await self.graph.aget_state(config)
            if not snapshot.values:
                raise ValueError("Контрольная точка не найдена. Создайте новый запрос.")
            checkpoint = dict(snapshot.values)
            context.run["_effects_before_resume"] = list(context.run.get("_completed_effects", []))
            interrupted = context.run.pop("_inflight_action", None)
            if interrupted:
                uncertain = context.run.setdefault("_uncertain_calls", [])
                if interrupted["key"] not in uncertain:
                    uncertain.append(interrupted["key"])
                if interrupted.get("mcp"):
                    unknown = context.run.setdefault("_uncertain_mcp_tools", [])
                    if interrupted["mcp"] not in unknown:
                        unknown.append(interrupted["mcp"])
            pending = "tools" in snapshot.next or bool(checkpoint.get("calls"))
            if pending:
                queued = checkpoint.get("calls") or []
                first = queued[0] if queued else None
                if (not interrupted and isinstance(first, dict) and first.get("name") in _EFFECTFUL
                        and isinstance(first.get("args"), dict)):
                    key = lookup_key(first["name"], first["args"])
                    uncertain = context.run.setdefault("_uncertain_calls", [])
                    if key not in uncertain and key not in context.run["_effects_before_resume"]:
                        uncertain.append(key)
                self.engine._warn(
                    context.run,
                    "Прерванный вызов инструмента не повторён: результат неизвестен. "
                    "Проверяем состояние заново перед следующими действиями.",
                )
            # A restored sandbox has been cleaned up. Re-plan using current tool
            # availability instead of replaying pending commands or stale clone IDs.
            context.run["evidence"] = [
                item for item in context.run["evidence"] if not item["index_id"].startswith("repo:")
            ]
            await self.graph.aupdate_state(
                config,
                {
                    "run": copy.deepcopy(context.run),
                    "calls": [],
                    "exchange": [],
                    "recovery_lookup": False,
                    "agent_batch": False,
                    "finish_after_sandbox_check": False,
                    "branch_waiting": False,
                    "force_lookup": False,
                    "next_step": "collect",
                },
                as_node="prepare",
            )
            state = None
        await self.graph.ainvoke(state, config=config, context=context, durability="sync")

    async def has_checkpoint(self, principal: Principal, run_id: str) -> bool:
        snapshot = await self.graph.aget_state(self.config(principal, run_id))
        return bool(snapshot.values)

    async def delete(self, principal: Principal, run_id: str) -> None:
        await self.checkpointer.adelete_thread(self.thread_id(principal, run_id))

    async def prune(self) -> None:
        known = self.engine.store.run_ids()
        async with self.checkpointer.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ) as cursor:
            rows = await cursor.fetchall()
        for (thread_id,) in rows:
            if str(thread_id).rsplit(":", 1)[-1] not in known:
                await self.checkpointer.adelete_thread(str(thread_id))

    @staticmethod
    def route(state: GraphState) -> str:
        return state.get("next_step", "done")

    def _context(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> ExecutionContext:
        context = runtime.context
        context.run.clear()
        context.run.update(copy.deepcopy(state["run"]))
        return context

    def _result(self, context: ExecutionContext, next_step: str, **values: Any) -> GraphState:
        visible = copy.deepcopy(context.run)
        # Terminal status is published only after the durable graph and sandbox
        # cleanup finish, so deletion cannot race an unfinished checkpoint write.
        if visible["status"] not in {"queued", "running", "cancelling"}:
            visible["status"] = "running"
        self.engine._checkpoint(context.principal, visible, context.cancel)
        return cast(
            GraphState, {"run": copy.deepcopy(context.run), "next_step": next_step, **values}
        )

    def _reserve_deadline(self, context: ExecutionContext) -> float:
        settings = self.engine._runtime_limits(context.run)
        if context.run.get("_control", {}).get("model_profile") == "deepseek_v4_flash":
            return self._deadline(context) - min(
                settings.final_reserve_seconds, self.engine._run_timeout(context.run) / 2,
            )
        return self._deadline(context) - min(
            settings.final_reserve_seconds, self.engine._run_timeout(context.run) / 4
        )

    def _deadline(self, context: ExecutionContext) -> float:
        return context.started + self.engine._run_timeout(context.run)

    async def prepare(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run, principal = context.run, context.principal
        catalog = await self.engine._indexes(principal, self.engine._source_timeout(run))
        available = {item["id"] for item in catalog}
        requested = list(dict.fromkeys(run["index_ids"]))
        if set(requested) - available:
            run["status"] = "failed"
            self.engine._warn(run, "Один или несколько выбранных индексов недоступны.")
            return self._result(context, "done")
        selected = requested or sorted(item["id"] for item in catalog
                                       if run["mode"] != "quick" or item["quick_search"])
        # The environment picker binds service checks to one profile even when the
        # caller selected all sources. Keep the selected environment inside the budget.
        api = sandbox_api_index(run.get("sandbox_profile_id", ""))
        selected = [item for item in selected if not item.startswith("sandbox-api:")]
        if run["mode"] != "quick" and api in available:
            selected.insert(0, api)
        if len(selected) > self.engine._runtime_limits(run).max_indexes:
            # Rank the entire permitted RAG selection before applying an operator cap.
            # A useful source at the end of the catalog still gets considered.
            direct = {item["id"]: item for item in catalog if item["direct_search"]}
            ranked = iter(await self.engine._rank_search_sources(
                principal, run, [direct[i] for i in selected if i in direct], allowed=available,
            ))
            selected = [next(ranked) if index in direct else index for index in selected]
            self.engine._warn(run, "Область поиска ограничена бюджетом числа индексов.")
        run["index_ids"] = selected[: self.engine._runtime_limits(run).max_indexes]
        if (len(run["index_ids"]) == 1 and question_record_id(run["question"])
                and requested_field_label(run["question"]) and any(
                    i["id"] == selected[0] and not i["direct_search"] and i["quick_search"]
                    for i in catalog)):
            run["_mcp_record_lookup"] = True
        if not run["index_ids"]:
            self.engine._warn(run, "Нет доступных источников; состояние системы не проверено.")
        return self._result(context, "plan", calls=[], exchange=[])

    async def plan(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        initialize_plan(context.run)
        context.run["phase"] = "planning"
        return self._result(context, "collect")

    async def collect(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run, principal, event = context.run, context.principal, context.cancel
        engine = self.engine
        run["phase"] = "searching"
        run["current_action"] = {"name": "search_knowledge", "started_at": timestamp()}
        engine._checkpoint(principal, run, event)
        budget = engine._budget(run)
        engine._semaphores[run["id"]] = asyncio.Semaphore(budget.parallelism)
        catalog = await engine._indexes(principal)
        available = {item["id"] for item in catalog}
        if set(run["index_ids"]) - available:
            raise ValueError("Source access changed during the investigation")
        parent = engine._parent_context(principal, run, available)
        run["parent_question"] = parent["question"] if parent else None
        query = initial_search_query(run)
        quick = read_chat(run)
        tool_indexes = [i["id"] for i in catalog if i["id"] in run["index_ids"]
                        and not i["direct_search"] and (not quick or i["quick_search"])]
        external = await engine.tools.describe(
            principal, tool_indexes, **({"read_only_mcp": True} if quick else {}),
        ) if (
            engine.tools and tool_indexes
        ) else []
        if quick:
            external = [t for t in external if t["function"]["name"] in {*METADATA, "mcp_call"}]
        external_ids = {item["id"] for item in catalog if not item["direct_search"]}
        knowledge = [item for item in run["index_ids"] if item not in external_ids]
        if quick and set(run["index_ids"]) - set(knowledge) - set(tool_indexes):
            engine._warn(
                run, "Для выбранных репозиториев и A2A используйте «Исследование»."
            )
        pending_only = bool(run.pop("_collect_pending", False))
        if not pending_only:
            run["coverage"] = {index: {"status": "pending", "count": 0} for index in knowledge}
        bootstrap = [index for index in knowledge if not pending_only
                     or run["coverage"].get(index, {}).get("status") == "pending"] \
            if engine.source else []
        if quick:
            sweep_deadline = self._deadline(context) - engine._run_timeout(run) * 2 / 3
        else:
            sweep_deadline = self._reserve_deadline(context)
        if ("graph:system" in bootstrap and not run.get("parent_run_id")
                and not run.get("resume_count") and simple_service_count_question(query)):
            bootstrap.remove("graph:system")
            await engine._search(principal, run, event, "graph:system", query,
                                 initial=True, deadline=sweep_deadline)
            if event.is_set():
                return self._result(context, "done")
            if run["mode"] == "autonomous" and engine._answer_snapshot_count(run):
                run["current_action"] = None
                return self._result(context, "done")
        direct = {item["id"]: item for item in catalog if item["direct_search"]}
        verified = await self._retrieve(context, [direct[i] for i in bootstrap], query,
                                        sweep_deadline, available, rag_only=not external)
        run["current_action"] = None
        searched = sum(row["status"] in {"searched", "empty"} for row in run["coverage"].values())
        engine._trace(run, "search_knowledge", {},
                      f"Поиск завершён. Проверено источников: {searched} из {len(knowledge)}.")
        engine._checkpoint(principal, run, event)
        if event.is_set():
            return self._result(context, "done")
        model = await engine._model_status()
        while model.get("state") == "authentication_required" and not event.is_set():
            await engine._refresh_authentication(run)
            engine.store.save(principal.owner_id, run)
            await asyncio.sleep(0.025)
            model = await engine._model_status()
        if event.is_set():
            return self._result(context, "done")
        await engine._refresh_authentication(run)
        if not model["available"]:
            run["status"] = "partial"
            run["stop_reason"] = "model_unavailable"
            engine._warn(run, model["reason"])
            return self._result(context, "done")
        if not run["evidence"] and not external and (quick or not knowledge):
            engine._warn(run, "Доступные источники не подтвердили сведения по вопросу.")
            return self._result(context, "synthesize", calls=[], exchange=[])
        if (verified and any(tool["function"]["name"] == "check_sandbox_api" for tool in external)
                and engine._followup_calls(run) < budget.source_calls
                and run["metrics"]["tool_calls"] < budget.tool_calls):
            return self._result(context, "tools", knowledge_ids=knowledge, external=external,
                                calls=[{"name": "check_sandbox_api", "args": {}}], exchange=[],
                                finish_after_sandbox_check=True)
        if verified and not external and run.get("_retrieval_support", {}).get("quote_is_answer"):
            if not engine._run_scope(run) <= {i["id"] for i in await engine._indexes(principal)}:
                raise ValueError("Source access changed during the investigation")
            excerpt = reviewed_excerpt(run)
            if not event.is_set() and excerpt and engine._answer(run, {"content": excerpt}):
                run["answer_kind"] = "verified_excerpt"
                return self._result(context, "done")
        return self._result(
            context,
            "synthesize" if verified or (quick and not external) else "inspect",
            external=external,
            knowledge_ids=knowledge,
            exchange=[],
            calls=[],
        )

    async def _retrieve(
        self, context: ExecutionContext, catalog: list[dict[str, Any]], query: str,
        deadline: float, available: set[str], *, rag_only: bool,
    ) -> bool:
        """Search a shortlist, widen on missing support and keep the original scope available."""
        if not catalog:
            return False
        run, engine, principal = context.run, self.engine, context.principal
        budget = engine._budget(run)
        remaining = await engine._rank_search_sources(principal, run, catalog, allowed=available)
        allowed = set(remaining)
        names = {entry["id"]: entry.get("name") or entry["id"] for entry in catalog}
        adaptive = (getattr(engine.model, "supports_search_assessment", False)
                    and getattr(engine.source, "supports_passage_retrieval", False)
                    and (await engine._model_status())["available"])
        remembered = None
        if adaptive and budget.document_reads and engine._followup_calls(run) < budget.source_calls:
            routes = await engine._initial_routes(principal, run, allowed, mcp=False)
            remembered = routes.get("read_document")
        # A plain ModelPort can answer questions without providing a retrieval assessment.
        # Without that decision boundary there is no benefit to serializing its initial search.
        width = min(budget.parallelism, 2) if adaptive else budget.parallelism
        if remembered and remaining[0] == remembered[0]["index_id"]:
            width = 1
        attempted = set()
        retry: tuple[str, str] | None = None
        while ((remaining or retry) and not context.cancel.is_set()
               and engine._now(run) < deadline):
            wave: list[tuple[str, str, bool]] = []
            if retry:
                index, alternate = retry
                retry = None
                if index in remaining:
                    remaining.remove(index)
                    wave.append((index, alternate, True))
                elif (engine._followup_calls(run) < budget.source_calls
                      and run["metrics"]["tool_calls"] < budget.tool_calls):
                    run["metrics"]["tool_calls"] += 1
                    wave.append((index, alternate, False))
            # Check a specific new lead on its own before inviting a wider set of sources.
            take = 0 if wave else width
            wave.extend((index, query, True) for index in remaining[:take])
            del remaining[:take]
            if not wave:
                break
            for index, phrase, _ in wave:
                attempted.add(lookup_key("search_knowledge", {"index_id": index, "query": phrase}))
            if adaptive:
                engine._trace(run, "source_routing", {},
                              "Проверяем источники: " + ", ".join(names[i] for i, _, _ in wave))
                engine._checkpoint(principal, run, context.cancel)
            await asyncio.gather(*(engine._search(principal, run, context.cancel, index, phrase,
                                                  deadline=deadline, initial=initial)
                                   for index, phrase, initial in wave))
            if not adaptive or context.cancel.is_set() or engine._now(run) >= deadline:
                continue
            indexes = [index for index, _, _ in wave]
            advice = await self._review_wave(context, indexes, remaining, deadline, remembered,
                                             single_pass=rag_only and len(catalog) <= 2,
                                             refined=any(phrase != query for _, phrase, _ in wave))
            if advice is None:
                break
            if advice["supported"]:
                for index in remaining:
                    if run["coverage"][index]["status"] == "pending":
                        run["coverage"][index] = {"status": "deferred", "count": 0}
                engine._trace(run, "source_supported", {"index_id": advice["index_id"]},
                              "Найдено подтверждение в источнике «" + names[advice["index_id"]]
                              + "». Переходим к ответу.")
                return True
            suggested = [i for i in dict.fromkeys(advice["indexes"]) if i in allowed]
            remaining = [i for i in suggested if i in remaining] + [
                i for i in remaining if i not in suggested
            ]
            alternate = advice["query"]
            if alternate:
                for index in suggested or indexes:
                    key = lookup_key("search_knowledge", {"index_id": index, "query": alternate})
                    if key not in attempted:
                        retry = (index, alternate)
                        engine._trace(run, "search_refinement", {"index_id": index},
                                      "Уточняем поисковый запрос по результатам проверки.")
                        break
            width = min(budget.parallelism, width * 2)
        if remaining and not context.cancel.is_set() and engine._now(run) >= deadline:
            engine._warn(run, "Время поиска исчерпано; зарезервирован итоговый ответ.")
        return False

    async def _review_wave(
        self, context: ExecutionContext, indexes: list[str], remaining: list[str], deadline: float,
        remembered: tuple[dict[str, str], str] | None, *, single_pass: bool, refined: bool,
    ) -> dict[str, Any] | None:
        run, engine = context.run, self.engine
        budget = engine._budget(run)
        hits = run.get("_search_hit_ids", {})
        refs = {(index, item_id) for index in indexes for item_id in hits.get(index, [])}
        items = [item for item in engine._ranked_evidence(run)
                 if (item["index_id"], item["id"]) in refs and is_answer_source(item)]
        documents = {(e["index_id"], e.get("document_id") or e["id"]) for e in items}
        if (single_pass and run["mode"] == "quick" and not remaining and len(documents) == 1
                and len(run["question"].split()) <= 3 and "?" not in run["question"]
                and len(set(items[0]["text"].casefold().split())
                        - set(run["question"].casefold().split())) >= 2
                and body_overlap(run["question"], items[0]["text"]) >= 0.85):
            # A short keyword lookup resolving to just one page needs only the writer.
            # Natural-language questions and competing documents still get reviewed.
            return None
        location = None
        memory_id = ""
        if remembered and remembered[0]["index_id"] in indexes:
            location, memory_id = remembered
        elif items and not items[0].get("read_document"):
            best = items[0]
            location = {"index_id": best["index_id"], "document_id": best.get("document_id")
                        or best["id"]}
        attempted = run.setdefault("_routing_reads", [])
        # Keep a read for the next targeted lookup when there is one read left and the
        # current generic hit barely overlaps the question. A saved route is already a lead.
        save_read = (bool(remaining) and len(attempted) + 1 >= budget.document_reads and not refined
                     and not memory_id and bool(items)
                     and body_overlap(run["question"], items[0]["text"]) < 0.5)
        if (location and not save_read and len(attempted) < budget.document_reads
                and run["metrics"].get("document_reads", 0) < budget.document_reads
                and engine._followup_calls(run) < budget.source_calls
                and (key := lookup_key("read_document", location)) not in attempted):
            attempted.append(key)
            if memory_id:
                engine._trace(run, "memory_route", location,
                              "По опыту похожего вопроса проверяем текущий текст документа.")
                try:
                    engine.memory.record_usage(context.principal.owner_id, [memory_id], run["id"])
                except Exception as exc:
                    engine._memory_problem(run, "Не удалось записать выбор источника.", exc)
            result = await engine._read(context.principal, run, context.cancel, location,
                                        deadline=deadline)
            engine._trace(run, "read_document", location, result)
            # A chunk and its freshly opened parent may have different evidence IDs.
            refs.add((location["index_id"], location["document_id"]))
            items = [
                item for item in engine._ranked_evidence(run) if is_answer_source(item) and (
                    (item["index_id"], item["id"]) in refs or
                    (item.get("read_document") and (item["index_id"], item.get("document_id"))
                     == (location["index_id"], location["document_id"]))
                )
            ]
        if context.cancel.is_set() or engine._now(run) >= deadline:
            return None
        return await engine._assess_search(context.principal, run, context.cancel, items,
                                           [*indexes, *remaining], deadline)

    async def inspect(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        """Open promising documents before accepting a snippet-only conclusion."""
        context = self._context(state, runtime)
        engine, run = self.engine, context.run
        run["phase"] = "reading"
        engine._checkpoint(context.principal, run, context.cancel)
        budget = engine._budget(run)
        candidates = engine._ranked_evidence(run)
        if run["mode"] == "autonomous" and service_inventory_question(run["question"]):
            candidates.sort(key=inventory_source_score, reverse=True)
        attempted = set(state.get("attempted_lookups", [])) | set(run.get("_routing_reads", []))
        available_tools = {t["function"]["name"] for t in state.get("external", [])}
        routes = await engine._initial_routes(
            context.principal, run,
            set(state.get("knowledge_ids", [])) if budget.document_reads else set(),
            mcp=bool({"mcp_list_tools", "mcp_tool_schema"} & available_tools),
        )
        recalled = routes.get("read_document")
        if recalled:
            route, _ = recalled
            candidates = [{"id": route["document_id"], "index_id": route["index_id"]}, *candidates]
        count = run["metrics"].get("document_reads", 0)
        for item in candidates:
            if (count >= budget.document_reads or context.cancel.is_set()
                    or engine._now(run) >= self._reserve_deadline(context)
                    or engine._followup_calls(run) >= budget.source_calls):
                break
            if item["index_id"] not in state.get("knowledge_ids", []) or item.get("read_document"):
                continue
            args = {"document_id": item.get("document_id") or item["id"],
                    "index_id": item["index_id"]}
            key = lookup_key("read_document", args)
            if key in attempted:
                continue
            if any(entry.get("read_document") and entry["id"] == args["document_id"]
                   and entry["index_id"] == args["index_id"] for entry in run["evidence"]):
                continue
            from_memory = bool(recalled and recalled[0] == args)
            if recalled and from_memory:
                engine._trace(run, "memory_route", args,
                              "Этот документ подтверждал прошлый ответ. Читаю текущую версию.")
                try:
                    engine.memory.record_usage(context.principal.owner_id, [recalled[1]], run["id"])
                except Exception as exc:
                    engine._memory_problem(run, "Не удалось записать использованный маршрут.", exc)
            result = await engine._read(context.principal, run, context.cancel, args,
                                        deadline=self._reserve_deadline(context),
                                        recovery=from_memory)
            attempted.add(key)
            engine._trace(run, "read_document", args, result)
            count += 1
        checks = []
        if ("check_sandbox_api" in available_tools
                and lookup_key("check_sandbox_api", {}) not in attempted
                and not context.cancel.is_set()
                and engine._now(run) < self._reserve_deadline(context)
                and engine._followup_calls(run) < budget.source_calls
                and run["metrics"]["tool_calls"] < budget.tool_calls):
            checks.append({"name": "check_sandbox_api", "args": {}})
        tool = "mcp_tool_schema" if "mcp_tool_schema" in routes else "mcp_list_tools"
        if (tool in routes and tool in available_tools and engine._decision_calls(run) == 0
                and not context.cancel.is_set()):
            args, memory_id = routes[tool]
            key = lookup_key(tool, args)
            # Discovery still needs a schema read and a substantive call. A
            # remembered contract only needs the latter.
            reserve_calls = (2 if tool == "mcp_list_tools" else 1) + len(checks)
            if (key not in attempted and engine._now(run) < self._reserve_deadline(context)
                    and run["metrics"]["tool_calls"] + reserve_calls < budget.tool_calls
                    and engine._followup_calls(run) + reserve_calls < budget.source_calls
                    and budget.model_calls >= (2 if read_chat(run) else 3) + reserve_calls):
                run["_initial_mcp_route"] = {"key": key, "memory_id": memory_id}
                run["_round_evidence"] = evidence_signature(run)
                return self._result(context, "tools", attempted_lookups=sorted(attempted),
                                    calls=[*checks, {"name": tool, "args": args}])
        if checks:
            return self._result(context, "tools", calls=checks, attempted_lookups=sorted(attempted))
        if (read_chat(run) and "mcp_list_tools" in available_tools
                and engine._decision_calls(run) == 0 and budget.model_calls > 1
                and not context.cancel.is_set()):
            discovery = next(t["function"] for t in state["external"]
                             if t["function"]["name"] == "mcp_list_tools")
            selected = discovery["parameters"]["properties"]["connection_id"].get("enum", [])
            remaining = min(budget.tool_calls - run["metrics"]["tool_calls"],
                            budget.source_calls - engine._followup_calls(run))
            # Preserve at least one schema read per manifest and two substantive reads.
            limit = min(3, max(0, (remaining - 2) // 2))
            calls = [{"name": "mcp_list_tools", "args": {"connection_id": name}}
                     for name in selected[:limit]]
            if calls:
                run["_round_evidence"] = evidence_signature(run)
                return self._result(context, "tools", attempted_lookups=sorted(attempted),
                                    calls=calls)
        return self._result(context, "decide", attempted_lookups=sorted(attempted))

    def _recover_draft(
        self, state: GraphState, context: ExecutionContext, issue: str,
    ) -> GraphState:
        run = context.run
        run["metrics"]["rejected_drafts"] = run["metrics"].get("rejected_drafts", 0) + 1
        self.engine._trace(run, "answer_quality_rejected", {}, issue)
        if needs_source_check(run) and not run.get("_goal_review_requested"):
            run["_goal_review_requested"] = True
            self.engine._trace(run, "goal_check", {}, "Для вывода нужна проверка источником.")
        return self._result(context, "replan", branch_reason=issue)

    async def replan(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        """Offer a concrete alternate check once, then execute a fresh read if ignored."""
        context = self._context(state, runtime)
        run, engine = context.run, self.engine
        if context.cancel.is_set():
            return self._result(context, "done", calls=[])
        budget = engine._budget(run)
        plan = run["research_plan"]
        waiting = state.get("branch_waiting", False)
        number = state.get("quality_rounds", 0) + int(not waiting)
        signature = evidence_signature(run)
        if signature != run.get("_branch_signature"):
            run["_branch_stalls"] = 0
            run["_branch_signature"] = signature
        if not waiting:
            run["_branch_stalls"] = run.get("_branch_stalls", 0) + 1
        if (run["_branch_stalls"] > engine._runtime_limits(run).max_stalled_rounds
                or engine._now(run) >= self._reserve_deadline(context)
                or run["metrics"]["tool_calls"] >= budget.tool_calls
                or engine._followup_calls(run) >= budget.source_calls):
            return self._result(context, "synthesize", calls=[])
        run["phase"] = "replanning"
        knowledge = state.get("knowledge_ids", [])
        attempted = set(state.get("attempted_lookups", []))
        attempted.update(lookup_key("search_knowledge", {
            "index_id": index, "query": initial_search_query(run),
        }) for index in knowledge if run["coverage"].get(index, {}).get("status") in
                         {"searched", "empty", "failed"})
        ranked = engine._ranked_evidence(run)
        inventory = service_inventory_question(run["question"])
        if inventory:
            ranked.sort(key=inventory_source_score, reverse=True)
        # Prefer a document directly relevant to an inventory over another generic
        # hit. When all snippets missed, the model can still offer its own query.
        candidates = ([item for item in ranked if inventory_source_score(item) >= 2]
                      if inventory else ranked)
        lookup = next_source_lookup(candidates, knowledge, attempted)
        if not lookup:
            lookup = next_external_lookup(run, state.get("external", []), attempted)
        if not lookup and not inventory:
            lookup = next_source_lookup(candidates, knowledge, attempted, run["question"])
        if not lookup and inventory:
            query = ("service catalog inventory" if run["question"].isascii()
                     else "реестр сервисов АС")
            for index in knowledge:
                args = {"index_id": index, "query": query}
                if lookup_key("search_knowledge", args) not in attempted:
                    lookup = {"name": "search_knowledge", "args": args}
                    break
        if not waiting:
            subquestion, target = branch_question(lookup, plan)
            branch = {"number": number, "reason": state["branch_reason"],
                      "subquestion": subquestion, "target": target,
                      "tool": lookup["name"] if lookup else "", "status": "open"}
            plan["branches"] = [*plan.get("branches", []), branch]
            plan["current_branch"] = branch
            engine._trace(run, "research_branch", lookup["args"] if lookup else {},
                          f"Проверка {number}. {branch['reason']} {subquestion}")
            engine._trace(run, "research_recovery", {},
                          "Уточните подзадачу и проверьте её новым разрешённым вызовом.")
        decisions_left = budget.model_calls - engine._decision_calls(run)
        if not waiting and not state.get("force_lookup") and decisions_left > 2:
            return self._result(context, "decide", quality_rounds=number, branch_waiting=True)
        if not lookup:
            plan["completion_reason"] = "Доступные зацепки для новой проверки исчерпаны."
            return self._result(context, "synthesize", calls=[], branch_waiting=False)
        run["_round_evidence"] = evidence_signature(run)
        run["_round_source_check"] = True
        return self._result(context, "tools", calls=[lookup], quality_rounds=number,
                            recovery_lookup=True, branch_waiting=False, force_lookup=False,
                            agent_batch=False)

    async def decide(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run, event, engine = context.run, context.cancel, self.engine
        budget = engine._budget(run)
        if event.is_set():
            return self._result(context, "done")
        if (
            engine._now(run) >= self._reserve_deadline(context)
            or engine._decision_calls(run) >= budget.model_calls - (
                2 if run["mode"] == "autonomous" else 1
            )
            or run["metrics"]["tool_calls"] >= budget.tool_calls
            or engine._followup_calls(run) >= budget.source_calls
        ):
            if run["mode"] == "quick":
                return self._result(context, "synthesize")
            plan = run.get("research_plan", {})
            covered = {index for index, value in run.get("coverage", {}).items()
                       if value["status"] in {"searched", "empty"}}
            covered.update(item["index_id"] for item in run["evidence"] if item.get("checked_with"))
            if (run["mode"] == "autonomous" and plan.get("model_updated")
                    and plan.get("ready_to_answer") and not plan.get("gaps")
                    and not needs_source_check(run) and set(run["index_ids"]) <= covered):
                # Using the last allowed operation to resolve a goal is success.
                # The normal synthesis gate still validates the actual answer.
                engine._trace(run, "goal_check", {},
                              "Проверки плана завершены. Готовим ответ по полученным материалам.")
                return self._result(context, "synthesize")
            previous_timeout = budget.timeout_seconds
            if await engine._review_budget(
                context.principal, run, event, started=context.started,
                deadline=(self._reserve_deadline(context)
                          if run.get("_control", {}).get("model_profile") == "deepseek_v4_flash"
                          else self._deadline(context)),
                reason="Достигнут рабочий бюджет исследования.",
            ):
                if (engine._budget(run).timeout_seconds > previous_timeout
                        and any(item["status"] == "pending"
                                for item in run.get("coverage", {}).values())):
                    run["_collect_pending"] = True
                    return self._result(context, "collect")
                return self._result(context, "decide")
            run["stop_reason"] = "budget_ceiling"
            engine._warn(run, "Исследование ограничено бюджетом; итог по собранным источникам.")
            return self._result(context, "synthesize")
        run["phase"] = "investigating"
        engine.store.save(context.principal.owner_id, run)
        catalog = self._tool_catalog(state)
        use_native = read_chat(run) or isinstance(engine.model, ModelRouter)
        native, targets = native_read_tools(run, catalog) if use_native else ([], {})
        if native and read_chat(run):
            discovery = next((t for t in catalog if t["function"]["name"] == "mcp_list_tools"),
                             None)
            inspected = discovery is not None and all_mcp_tools_inspected(run, discovery, targets)
            # A small complete manifest needs only the actual remote functions.
            # With a partial or remembered manifest keep discovery for other leads.
            hidden = {*METADATA, "mcp_call"} if inspected else {"mcp_call"}
            catalog = [t for t in catalog if t["function"]["name"] not in hidden]
        try:
            decision = await engine._decide_with_transport_retry(
                context.principal,
                run,
                event,
                [*native, *catalog],
                state.get("exchange", []),
                self._reserve_deadline(context),
            )
        except TimeoutError as exc:
            # Authentication exhaustion belongs to the outer run controller.
            if type(exc).__name__ == "_AuthenticationTimeout":
                raise
            engine._warn(run, "Время поиска исчерпано; зарезервирован итоговый ответ.")
            return self._result(context, "synthesize")
        except ConversationUnavailable:
            run["status"] = "partial"
            return self._result(context, "done")
        except ModelTransientError as exc:
            autonomous = run["mode"] == "autonomous"
            if (autonomous and not isinstance(engine.model, ModelRouter)
                    and not run["metrics"].get("planner_retries")
                    and engine._decision_calls(run) < budget.model_calls - 2
                    and engine._now(run) < self._reserve_deadline(context)):
                run["metrics"]["planner_retries"] = 1
                engine._trace(run, "model_retry", {},
                              f"{exc} Выбираем другую проверку.")
                return self._result(context, "replan",
                                    branch_reason="Планировщик временно недоступен.")
            engine._trace(run, "model_retry", {},
                          f"{exc} Готовим итог повторным запросом без повторения действий.")
            return self._result(context, "synthesize")
        except ModelDecisionError:
            run["metrics"]["decision_errors"] = run["metrics"].get("decision_errors", 0) + 1
            engine._trace(run, "model_response", {},
                          "Некорректный формат предыдущего решения. Верните content и tool_calls; "
                          "используйте только перечисленные инструменты и их схемы аргументов.")
            if run["mode"] == "autonomous":
                limit = engine._runtime_limits(run).max_decision_errors
                exhausted = run["metrics"]["decision_errors"] >= limit
                return self._result(context, "replan", force_lookup=exhausted,
                                    branch_reason="Планировщик вернул некорректное действие.")
            if run["metrics"]["decision_errors"] < engine._runtime_limits(run).max_decision_errors:
                return self._result(context, "decide")
            engine._warn(run, "Модель не смогла сформировать корректные действия; готовим итог.")
            return self._result(context, "synthesize")
        calls = decision.get("tool_calls", [])
        if targets:
            if (not calls and isinstance(decision.get("content"), str)
                    and decision.get("finish_reason") not in RETRYABLE_FINISH_REASONS):
                calls = text_read_call(decision["content"], targets)
            calls = [
                {"name": "mcp_call", "args": {**targets[call["name"]], "arguments": call["args"]}}
                if (isinstance(call, dict) and isinstance(call.get("name"), str)
                    and call["name"] in targets and isinstance(call.get("args"), dict)) else call
                for call in calls
            ]
        if calls and state.get("branch_waiting"):
            allowed_names = {item["function"]["name"] for item in catalog}
            useful_calls = []
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("name"), str):
                    continue
                name, args = call["name"], call.get("args", {})
                if name not in allowed_names or not isinstance(args, dict):
                    continue
                key = lookup_key(name, args)
                refresh = name in METADATA and args.get("refresh") is True
                if (refresh or key not in run.get("_issued_calls", [])) and key not in run.get(
                        "_uncertain_calls", []):
                    useful_calls.append(call)
            if not useful_calls:
                return self._result(context, "replan",
                                    branch_reason="Предложенная проверка уже выполнялась.")
            calls = useful_calls
        if not calls:
            if (read_chat(run) and state.get("external") and not targets
                    and not any(item.get("tool_origin", {}).get("tool") == "mcp_call"
                                for item in run["evidence"])
                    and (lookup := next_external_lookup(
                        run, state["external"], set(state.get("attempted_lookups", [])),
                    ))):
                # A text-only planning reply is common on small models. A catalog
                # or schema can be read safely before the next native tool decision.
                run["_round_evidence"] = evidence_signature(run)
                return self._result(context, "tools", calls=[lookup])
            if decision.get("finish_reason") in RETRYABLE_FINISH_REASONS:
                engine._trace(run, "answer_recovery", {},
                              "Предыдущий ответ был обрезан. Подготовьте краткий законченный итог.")
                return self._result(context, "synthesize")
            issue = engine._draft_quality_issue(run, decision.get("content", ""))
            if issue:
                run["_final_quality_issue"] = issue
                if run["mode"] == "autonomous":
                    return self._recover_draft(state, context, issue)
                engine._trace(run, "answer_quality_rejected", {}, issue)
                return self._result(context, "synthesize")
            if state.get("branch_waiting"):
                return self._result(context, "replan",
                                    branch_reason="Подзадача осталась без проверки.")
            plan = run.get("research_plan", {})
            missing_check = needs_source_check(run)
            no_actions = (run["mode"] != "autonomous" and not plan.get("model_updated")
                          and not run["metrics"]["tool_calls"])
            if (missing_check or no_actions) and not run.get("_goal_review_requested"):
                run["_goal_review_requested"] = True
                engine._trace(run, "goal_check", {},
                              "Выберите зацепку в найденных материалах и проверьте её: "
                              "уточните поиск, прочитайте документ, файл или запросите данные "
                              "доступным инструментом. Укажите результат или причину, "
                              "по которой проверка недоступна.")
                if run["mode"] == "autonomous":
                    return self._result(context, "replan",
                                        branch_reason="Вывод ещё не проверен в источнике.")
                return self._result(context, "decide")
            if missing_check:
                return self._result(context, "replan", force_lookup=True,
                                    branch_reason="Не получено подтверждение по вопросу.")
            if plan.get("model_updated") and not plan.get("ready_to_answer"):
                if not run.get("_gap_review_requested"):
                    run["_gap_review_requested"] = True
                    engine._trace(run, "goal_check", {},
                                  "План ещё не завершён. Выполните проверки инструментами "
                                  "или укажите конкретную причину недоступности проверки.")
                    if run["mode"] == "autonomous":
                        return self._result(context, "replan",
                                            branch_reason="В плане осталась незакрытая проверка.")
                    return self._result(context, "decide")
                # A planning-only response is never the final answer. A separate
                # synthesis call must report actual findings and remaining gaps.
                engine._trace(run, "goal_check", {},
                              "План не подтверждён как завершённый. Нужен итог по фактам; "
                              "перечислите непроверенные вопросы.")
                return self._result(context, "synthesize")
            return self._result(context, "done" if engine._answer(run, decision) else "synthesize")
        run["_round_evidence"] = evidence_signature(run)
        run["_round_source_check"] = False
        return self._result(
            context,
            "tools",
            calls=calls[: budget.tool_calls - run["metrics"]["tool_calls"]],
            exchange=state.get("exchange", []),
            recovery_lookup=False,
            branch_waiting=False,
            agent_batch=run["mode"] == "autonomous",
        )

    @staticmethod
    def _tool_catalog(state: GraphState) -> list[dict[str, Any]]:
        run = state["run"]
        if read_chat(run):
            return [*(TOOLS if state.get("knowledge_ids") else []), *state.get("external", [])]
        return [*([BUDGET_TOOL] if run["mode"] == "autonomous" else []),
                PLAN_TOOL, *(TOOLS if state.get("knowledge_ids") else []),
                *([ARCHIVE_TOOL] if archive_available(run) else []),
                *state.get("external", [])]

    async def tools(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        if not state.get("agent_batch") or state["run"]["mode"] != "autonomous":
            return await self._execute_tool(state, runtime)
        from agent_harness.library_agent import run_agent_round

        # The preceding durable node has already paid for and checkpointed this
        # decision. Feed it back to the framework without another model request.
        batch = list(state.get("calls", []))
        catalog = self._tool_catalog(state)
        names = {tool["function"]["name"] for tool in catalog}
        if not batch or not all(
            isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"] in names
            and isinstance(c.get("args", {}), dict) for c in batch
        ):
            return await self._execute_tool({**state, "agent_batch": False}, runtime)
        context = self._context(state, runtime)
        if context.cancel.is_set():
            return self._result(context, "done", calls=[], agent_batch=False)
        metrics = context.run["metrics"]
        metrics["agent_rounds"] = metrics.get("agent_rounds", 0) + 1
        self.engine._trace(context.run, "agent_round", {},
                           "Агент выполняет выбранные действия и проверяет результат.")
        working: GraphState = {**state, "agent_batch": False, "run": copy.deepcopy(context.run)}
        first = True
        dispatched = 0

        async def decide(observations: list[dict[str, Any]]) -> dict[str, Any]:
            nonlocal first, working
            if first:
                first = False
                return {"content": "", "tool_calls": batch}
            # Only a real ToolMessage can invite a new decision. _execute_tool
            # has already stored its receipt and evidence in the run and exchange.
            if not observations or working["next_step"] != "decide":
                raise RuntimeError("Agent loop resumed without a completed tool observation")
            exchange = working.get("exchange", [])
            receipts = exchange[-len(observations):]
            if len(receipts) != len(observations) or any(
                obs["name"] != receipt["name"]
                for obs, receipt in zip(observations, receipts, strict=True)
            ):
                raise ModelDecisionError("Agent observations differ from authorized tool receipts")
            working["exchange"] = [*exchange[:-len(receipts)], *(
                {**receipt, "model_observation": obs["result"]}
                for obs, receipt in zip(observations, receipts, strict=True)
            )]
            working = {**working, **await self.decide(working, runtime)}
            if working["next_step"] == "tools" and working.get("agent_batch"):
                return {"content": "", "tool_calls": working["calls"]}
            return {"content": "Дальнейший переход определён графом.", "tool_calls": []}

        async def dispatch(calls: list[dict[str, Any]]) -> list[str] | None:
            nonlocal dispatched, working
            if len(calls) != 1 or dispatched >= len(batch) or working.get("calls", [])[:1] != calls:
                # Never execute a substituted action or retry a partially consumed batch.
                return None
            dispatched += 1
            delta = await self._execute_tool(working, runtime)
            working = {**working, **delta}
            if ("exchange" not in delta or working.get("calls", []) != batch[dispatched:]
                    or working["next_step"] not in {"tools", "reflect"}
                    or runtime.context.cancel.is_set()):
                return None
            if working["next_step"] == "reflect":
                # Governance-only steps checkpoint independently before the next decision.
                governance = {"request_budget", "update_research_plan"}
                if all(call["name"] in governance for call in batch):
                    return None
                working = {**working, **await self.reflect(working, runtime)}
                if working["next_step"] != "decide":
                    return None
            exchange = working.get("exchange", [])
            return [exchange[-1]["result"]] if exchange else None

        try:
            await run_agent_round(tools=catalog, decide=decide, dispatch=dispatch)
        except ModelDecisionError:
            return self._result(context, "replan", calls=[], agent_batch=False,
                                branch_reason="Агент не смог продолжить проверку этим действием.")
        if not dispatched:
            return self._result(context, "replan", calls=[], agent_batch=False,
                                branch_reason="Агент не смог выполнить выбранное действие.")
        return working

    async def _execute_tool(
        self, state: GraphState, runtime: Runtime[ExecutionContext],
    ) -> GraphState:
        context = self._context(state, runtime)
        engine, run, principal, event = self.engine, context.run, context.principal, context.cancel
        budget = engine._budget(run)
        calls = list(state.get("calls", []))
        if event.is_set():
            return self._result(context, "done", calls=[])
        if (
            not calls
            or engine._now(run) >= self._reserve_deadline(context)
            or run["metrics"]["tool_calls"] >= budget.tool_calls
            or engine._followup_calls(run) >= budget.source_calls
        ):
            return self._result(context, "reflect", calls=[])
        call = calls.pop(0)
        run["metrics"]["tool_calls"] += 1
        if not isinstance(call, dict):
            engine._warn(run, "Некорректный формат вызова инструмента отклонён.")
            if run["mode"] == "autonomous" and not calls:
                return self._result(context, "replan", calls=[],
                                    branch_reason="Не удалось прочитать описание инструмента.")
            return self._result(context, "tools" if calls else "decide", calls=calls)
        name, args = str(call.get("name", "")), call.get("args", {})
        attempted = set(state.get("attempted_lookups", []))
        before = {(item["index_id"], item["id"]): item for item in run["evidence"]}
        previous_checks = {ref: item.get("checked_with") for ref, item in before.items()}
        cached_ref = None
        preflight = None
        ref = None
        archive_matches = []
        phases = {"search_knowledge": "refining", "read_document": "reading",
                  "read_repository": "reading_code", "search_repository": "reading_code",
                  "clone_repository": "reading_code", "run_in_sandbox": "running_code",
                  "update_research_plan": "planning", "request_budget": "budget_review",
                  "search_collected_materials": "reading"}
        run["phase"] = phases.get(name, "checking")
        run["current_action"] = {
            "name": name, "started_at": timestamp(),
            "args": {key: str(args[key])[:1000] for key in
                     ("index_id", "document_id", "connection_id", "repository_id")
                     if isinstance(args, dict) and key in args},
        }
        engine._checkpoint(principal, run, event)
        try:
            if not isinstance(args, dict):
                raise ValueError("Invalid tool arguments")
            key = lookup_key(name, args)
            refresh = name in METADATA and args.get("refresh") is True
            if name in METADATA:
                if "refresh" in args and type(args["refresh"]) is not bool:
                    raise ValueError("Invalid MCP refresh flag")
                ref = run.get("_mcp_metadata_refs", {}).get(key)
                if not refresh and ref and tuple(ref) in before:
                    cached_ref = ref
            signature = hashlib.sha256(json.dumps(
                [name, args, evidence_signature(run)], sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            seen = run.setdefault("_seen_tool_calls", [])
            if name not in {"update_research_plan", "request_budget"}:
                run["_round_source_check"] = True
            uncertain_mcp = (name == "mcp_call"
                             and schema_key(args) in run.get("_uncertain_mcp_tools", []))
            if key in run.get("_uncertain_calls", []) or uncertain_mcp:
                result = "Результат прежнего вызова неизвестен. Выберите другую проверку."
            elif key in run.get("_effects_before_resume", []):
                result = "Этот вызов завершён до прерывания. Используйте сохранённый результат."
            elif cached_ref:
                result = "Описание инструментов уже получено и доступно в материалах."
            elif (not refresh and name not in {"update_research_plan", "request_budget"}
                  and (signature in seen or ((name == "search_knowledge"
                       or (name == "mcp_call" and read_chat(run)))
                       and key in run.get("_issued_calls", [])))):
                if name == "search_knowledge":
                    run["metrics"]["tool_calls"] -= 1
                result = "Повтор без новых данных пропущен. Измените запрос или источник."
            elif name == "request_budget":
                # Governance is accounted separately from source/tool execution.
                run["metrics"]["tool_calls"] -= 1
                result = engine._change_budget(run, args, started=context.started)
            elif name == "update_research_plan":
                update_plan(run, args)
                plan = run["research_plan"]
                branch = plan.get("current_branch")
                if branch and branch.get("status") == "open" and plan.get("next_action"):
                    branch["subquestion"] = next(iter(plan["gaps"]), plan["next_action"])
                    plan["branches"][-1] = branch
                result = "План исследования обновлён. Проверки требуют подтверждений источниками."
            elif name == "search_collected_materials" and archive_available(run):
                seen.append(signature)
                allowed = {item["id"] for item in await engine._indexes(principal)}
                if not event.is_set():
                    archive_matches = await search_materials(run, args, allowed)
                run["_archive_lead_found"] = bool(archive_matches)
                result = f"Совпадений в ранее собранных материалах: {len(archive_matches)}."
            elif name in {item["function"]["name"] for item in state.get("external", [])}:
                seen.append(signature)
                attempted.add(key)
                remembered = run.get("_initial_mcp_route", {})
                if remembered.get("key") == key:
                    run.pop("_initial_mcp_route")
                    if remembered["memory_id"]:
                        engine._trace(run, "memory_route", args,
                                      f"Проверяю текущую схему {args['tool']} по прошлому опыту.")
                        try:
                            engine.memory.record_usage(
                                principal.owner_id, [remembered["memory_id"]], run["id"],
                            )
                        except Exception as exc:
                            engine._memory_problem(run, "Не удалось записать выбор маршрута.", exc)
                    else:
                        engine._trace(run, "source_discovery", args,
                                      f"Уточняю инструменты подключения {args['connection_id']}.")
                    engine._checkpoint(principal, run, event)
                run.pop("_uncertain_tool_call", None)
                call_args = args
                if name == "mcp_call":
                    version = run.get("_mcp_schema_revisions", {}).get(schema_key(args))
                    if version:
                        call_args = {**args, "schema_revision": version}
                if name in _EFFECTFUL:
                    # RunStore persists this before any external effect. A library
                    # batch can stop between outer LangGraph checkpoints.
                    run["_inflight_action"] = {"key": key, "mcp": schema_key(args)
                                               if name == "mcp_call" else ""}
                    engine._checkpoint(principal, run, event)
                result = await engine._external_tool(
                    principal, run, event, name, call_args, self._reserve_deadline(context)
                )
                unknown_result = run.pop("_uncertain_tool_call", False)
                if unknown_result and name in _EFFECTFUL:
                    run.setdefault("_uncertain_calls", []).append(key)
                    if name == "mcp_call":
                        run.setdefault("_uncertain_mcp_tools", []).append(schema_key(args))
                run.pop("_inflight_action", None)
                preflight = run.pop("_mcp_preflight_failure", None)
                conflict = preflight or run.pop("_mcp_dispatch_conflict", None)
                if name in _EFFECTFUL and not unknown_result and not conflict:
                    confirmed = run.setdefault("_completed_effects", [])
                    if key not in confirmed:
                        confirmed.append(key)
                if conflict:
                    repair = repair_call(run, args, conflict)
                    calls = [repair] if repair else []
                    if repair:
                        engine._trace(run, "mcp_contract_refresh", args,
                                      "Проверяем текущее описание инструмента на сервере MCP.")
            else:
                validated = validate_call(name, args, set(state.get("knowledge_ids", [])))
                attempted.add(lookup_key(name, validated))
                seen.append(signature)
                result = (
                    await engine._search(
                        principal,
                        run,
                        event,
                        validated["index_id"],
                        validated["query"],
                        validated["limit"],
                        deadline=self._reserve_deadline(context),
                        recovery=state.get("recovery_lookup", False),
                    )
                    if name == "search_knowledge"
                    else await engine._read(principal, run, event, validated,
                                            deadline=self._reserve_deadline(context),
                                            model_requested=not state.get("recovery_lookup"),
                                            recovery=state.get("recovery_lookup", False))
                )
            if not preflight and key not in run.setdefault("_issued_calls", []):
                run["_issued_calls"].append(key)
        except (ValueError, TypeError):
            result = "Вызов отклонён: недопустимый инструмент, аргументы или индекс."
            # A malformed query or plan is recoverable. The trace retains the
            # rejected action; changing scope or requesting an unknown operation
            # still leaves an explicit access warning on the final result.
            resources = BudgetChanges.model_fields.keys()
            budget_form_error = (
                name == "request_budget" and isinstance(args, dict)
                and args.keys() <= BudgetRequest.model_fields.keys() | resources
                and (not isinstance(args.get("changes"), dict)
                     or args["changes"].keys() <= resources)
            )
            schema_error = name == "update_research_plan" or budget_form_error or (
                name in {"search_knowledge", "read_document"} and isinstance(args, dict)
                and (not args.get("index_id") or args["index_id"] in state.get("knowledge_ids", []))
            )
            if run["mode"] != "autonomous" or not schema_error:
                engine._warn(run, result)
            engine._record_tool_failure(principal, run, operation="invalid",
                                        index_ids=run["index_ids"])
            run["_round_invalid_action"] = True
            run["_round_source_check"] = True
        engine._trace(run, name, args, result)
        run["current_action"] = None
        fresh = []
        for item in run["evidence"]:
            ref_key = item["index_id"], item["id"]
            if (previous_checks.get(ref_key) != item.get("checked_with")
                    or before.get(ref_key) is not item):
                fresh.append(item)
        refs = [[item["index_id"], item["id"]] for item in fresh[:8]]
        if archive_matches:
            refs = [[item["index_id"], item["id"]] for item in archive_matches]
        if (name in METADATA and refs
                and fresh[0].get("kind") == "tool_metadata"):
            old = before.get(tuple(ref), {}) if ref else {}
            changes = record_metadata(run, name, args, old, fresh[0])
            if changes:
                fresh[0]["tool_changes"] = changes
                labels = {"added": "Добавлены", "changed": "Изменены", "removed": "Удалены"}
                detail = ". ".join(f"{labels[k]}: {', '.join(v)}" for k, v in changes.items())
                engine._trace(run, "mcp_catalog_changed", args, detail)
            run.setdefault("_mcp_metadata_refs", {})[key] = refs[0]
        answer_ready = False
        answered = False
        bound = False
        if name == "mcp_call" and isinstance(args, dict):
            bound = (run.pop("_question_bound_mcp_call", None) == key
                     or question_record_lookup(run["question"], args.get("arguments")))
            if bound:
                verified = run.setdefault("_question_bound_mcp_refs", [])
                verified.extend(item["id"] for item in fresh if item.get("checked_with") == name
                                and item["id"] not in verified)
        if fresh and manifest_needs_refresh(run, name, args, fresh[0]):
            # The SDK already listed the live manifest. Present its change
            # before issuing any dependent actions from the old plan.
            calls = [{"name": "mcp_list_tools", "args": {
                "connection_id": args["connection_id"], "refresh": True,
            }}]
            engine._trace(run, "mcp_contract_refresh", args,
                          "Состав каталога MCP изменился. Получаем свежий список.")
        elif read_chat(run) and name == "mcp_list_tools" and fresh:
            # Leave room to use the inspected contracts and follow a reference.
            remaining = min(budget.tool_calls - run["metrics"]["tool_calls"],
                            budget.source_calls - engine._followup_calls(run))
            reserved = 2 + sum(2 if c.get("name") == "mcp_list_tools" else 1 for c in calls)
            reads = quick_schema_reads(fresh[0], args["connection_id"], remaining - reserved,
                                       attempted, run["question"])
            calls = [*calls, *reads]
        elif read_chat(run) and name == "mcp_call" and len(fresh) == 1 and not calls:
            field_value = requested_field(run, run["evidence"])
            if field_value and field_value[2] == fresh[0]["id"]:
                answer_ready = True
                engine._trace(run, "goal_check", args, "Инструмент вернул запрошенное поле.")
                if bound:
                    label, value, ref_id = field_value
                    label = label[:1].upper() + label[1:]
                    answered = engine._answer(run, {"content": f"{label} — {value} [{ref_id}]."})
            elif follow := linked_read(run, fresh[0], state.get("external", [])):
                engine._trace(run, "mcp_reference", follow["args"],
                              "В результате найден идентификатор для следующего чтения MCP.")
                calls = [follow]
                if bound:
                    run["_question_bound_mcp_call"] = lookup_key("mcp_call", follow["args"])
            elif run["mode"] != "quick":
                run.pop("_mcp_record_lookup", None)  # Ambiguous record; continue investigation.
        plan = run.get("research_plan", {})
        branch = plan.get("current_branch")
        if branch and branch.get("status") == "open" and name not in {
            "update_research_plan", "request_budget",
        }:
            # Report the action actually taken even if the model selected a different
            # route from our suggested next read. Never store commands or arguments here.
            if refs:
                branch["target"] = refs[0][0]
            branch["tool"] = name if name in phases or name in {
                item["function"]["name"] for item in state.get("external", [])
            } else ""
            if any(item.get("checked_with") and not item.get("tool_outcome") for item in fresh):
                branch["status"] = "checked"
            elif archive_matches or any(not item.get("tool_outcome") for item in fresh):
                branch["status"] = "inspected"
            else:
                branch["status"] = "no_result"
            plan["branches"][-1] = branch
        next_step = "tools" if calls else "reflect"
        if name == "check_sandbox_api" and state.get("finish_after_sandbox_check"):
            next_step = "synthesize"
        if answer_ready:
            next_step = "done" if answered else "synthesize"
        return self._result(
            context,
            next_step,
            finish_after_sandbox_check=False,
            calls=calls,
            attempted_lookups=sorted(attempted),
            recovery_lookup=False,
            exchange=[*state.get("exchange", []), {
                "name": name, "result": result,
                "evidence_refs": [cached_ref] if cached_ref else refs,
                **({"archive_matches": archive_matches} if name == "search_collected_materials"
                   else {}),
            }][-50:],
        )

    async def reflect(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run = context.run
        initialize_plan(run)
        run["phase"] = "checking"
        plan = run["research_plan"]
        plan["round"] += 1
        changed = evidence_signature(run) != run.get("_round_evidence")
        invalid_action = run.pop("_round_invalid_action", False)
        archive_lead = run.pop("_archive_lead_found", False)
        if archive_lead and not changed:
            self.engine._trace(run, "research_round", {},
                               "Найдена зацепка в собранных материалах. Проверяем первоисточник.")
            return self._result(context, "decide", calls=[])
        run["_stalled_rounds"] = 0 if changed else run.get("_stalled_rounds", 0) + 1
        self.engine._trace(
            run, "research_round", {},
            "Получены новые материалы; проверяем оставшиеся вопросы."
            if changed else "Новых материалов нет; нужен другой запрос или источник.",
        )
        if run["_stalled_rounds"] >= self.engine._runtime_limits(run).max_stalled_rounds:
            if run["mode"] == "autonomous":
                return self._result(context, "replan", force_lookup=True, calls=[],
                                    branch_reason="Несколько шагов не дали новых материалов.")
            run["stop_reason"] = "no_new_evidence"
            plan["completion_reason"] = "Поиск не даёт новых данных."
            self.engine._warn(run, "Повторные действия не дали новых данных; поиск ограничен.")
            return self._result(context, "synthesize", calls=[])
        if run["mode"] == "autonomous" and run.get("_round_source_check") and not changed:
            # A stalled knowledge lookup already has a scoped fallback. A live
            # external tool may need the model to interpret its current schema.
            last = next(iter(state.get("exchange", [])[-1:]), {})
            force = invalid_action or last.get("name") in {"read_document", "search_knowledge"}
            return self._result(context, "replan", calls=[], force_lookup=force,
                                branch_reason="Проверка не добавила данных. Нужен другой путь.")
        return self._result(context, "decide", calls=[])

    async def synthesize(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run = context.run
        initialize_plan(run)
        if not run["research_plan"].get("completion_reason"):
            run["research_plan"]["completion_reason"] = "Готовим итог по доступным подтверждениям."
        budget = self.engine._budget(run)
        if (not context.cancel.is_set()
                and self.engine._decision_calls(run) < budget.model_calls):
            await self.engine._synthesize(context.principal, run, context.cancel)
        elif not context.cancel.is_set():
            run["status"] = "partial"
            self.engine._warn(
                run, "Бюджет модели исчерпан. Для нового исследования создайте запрос."
            )
        return self._result(context, "done")
