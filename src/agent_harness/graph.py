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

from agent_harness.budgets import BUDGET_TOOL, BudgetChanges, BudgetRequest
from agent_harness.contracts import ConversationUnavailable, ModelDecisionError, Principal
from agent_harness.material_archive import ARCHIVE_TOOL, search_materials
from agent_harness.material_archive import available as archive_available
from agent_harness.mcp_catalog import (
    METADATA,
    manifest_needs_refresh,
    record_metadata,
    repair_call,
    schema_key,
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
            context.run.pop("_goal_review_requested", None)
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
        available = {
            item["id"]
            for item in await self.engine._indexes(principal, self.engine._source_timeout(run))
        }
        requested = list(dict.fromkeys(run["index_ids"]))
        if set(requested) - available:
            run["status"] = "failed"
            self.engine._warn(run, "Один или несколько выбранных индексов недоступны.")
            return self._result(context, "done")
        selected = requested or sorted(available)
        if len(selected) > self.engine._runtime_limits(run).max_indexes:
            self.engine._warn(run, "Область поиска ограничена бюджетом числа индексов.")
        run["index_ids"] = selected[: self.engine._runtime_limits(run).max_indexes]
        if not run["index_ids"]:
            run["status"] = "partial"
            self.engine._warn(run, "Нет доступных источников; состояние системы не проверено.")
            return self._result(context, "done")
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
        available = {item["id"] for item in await engine._indexes(principal)}
        if set(run["index_ids"]) - available:
            raise ValueError("Source access changed during the investigation")
        parent = engine._parent_context(principal, run, available)
        run["parent_question"] = parent["question"] if parent else None
        query = initial_search_query(run)
        external = (
            await engine.tools.describe(principal, run["index_ids"])
            if engine.tools and run["mode"] != "quick"
            else []
        )
        external_ids = (
            {item["id"] for item in await engine.tools.indexes(principal)}
            if engine.tools
            else set()
        )
        knowledge = [item for item in run["index_ids"] if item not in external_ids]
        if run["mode"] == "quick" and set(run["index_ids"]) & external_ids:
            engine._warn(
                run, "Быстрый ответ использует RAG. Код, MCP и A2A доступны в исследовании."
            )
        pending_only = bool(run.pop("_collect_pending", False))
        if not pending_only:
            run["coverage"] = {index: {"status": "pending", "count": 0} for index in knowledge}
        bootstrap = [index for index in knowledge if not pending_only
                     or run["coverage"].get(index, {}).get("status") == "pending"] \
            if engine.source else []
        sweep_deadline = (self._deadline(context) - engine._run_timeout(run) * 2 / 3
                          if run["mode"] == "quick" else self._reserve_deadline(context))
        if ("graph:system" in bootstrap and not run.get("parent_run_id")
                and not run.get("resume_count") and simple_service_count_question(query)):
            bootstrap.remove("graph:system")
            await engine._search(principal, run, event, "graph:system", query,
                                 initial=True, deadline=sweep_deadline)
            if event.is_set():
                return self._result(context, "done")
            if engine._answer_snapshot_count(run):
                run["current_action"] = None
                return self._result(context, "done")
        await asyncio.gather(
            *(
                engine._search(
                    principal,
                    run,
                    event,
                    index,
                    query,
                    deadline=sweep_deadline,
                    initial=True,
                )
                for index in bootstrap
            )
        )
        run["current_action"] = None
        engine._trace(run, "search_knowledge", {},
                      f"Начальный поиск завершён: источников {len(bootstrap)}.")
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
        if not model["available"] or (
            not run["evidence"] and not external and run["mode"] == "quick"
        ):
            run["status"] = "partial"
            engine._warn(
                run,
                model["reason"]
                if not model["available"]
                else "Подтверждающие источники не найдены.",
            )
            return self._result(context, "done")
        return self._result(
            context,
            "synthesize" if run["mode"] == "quick" else "inspect",
            external=external,
            knowledge_ids=knowledge,
            exchange=[],
            calls=[],
        )

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
        attempted = set(state.get("attempted_lookups", []))
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
        count = 0
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
        tool = "mcp_tool_schema" if "mcp_tool_schema" in routes else "mcp_list_tools"
        if (tool in routes and tool in available_tools and engine._decision_calls(run) == 0
                and not context.cancel.is_set()):
            args, memory_id = routes[tool]
            key = lookup_key(tool, args)
            # Discovery still needs a schema read and a substantive call. A
            # remembered contract only needs the latter.
            reserve_calls = 2 if tool == "mcp_list_tools" else 1
            if (key not in attempted and engine._now(run) < self._reserve_deadline(context)
                    and run["metrics"]["tool_calls"] + reserve_calls < budget.tool_calls
                    and engine._followup_calls(run) + reserve_calls < budget.source_calls
                    and budget.model_calls >= 3 + reserve_calls):
                run["_initial_mcp_route"] = {"key": key, "memory_id": memory_id}
                run["_round_evidence"] = evidence_signature(run)
                return self._result(context, "tools", attempted_lookups=sorted(attempted),
                                    calls=[{"name": tool, "args": args}])
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
        }) for index in knowledge)
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
        try:
            decision = await engine._decide(
                context.principal,
                run,
                event,
                catalog,
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
            if ((autonomous or isinstance(engine.model, ModelRouter))
                    and not run["metrics"].get("planner_retries")
                    and engine._decision_calls(run) < budget.model_calls - (2 if autonomous else 1)
                    and engine._now(run) < self._reserve_deadline(context)):
                run["metrics"]["planner_retries"] = 1
                if not autonomous:
                    engine._trace(run, "model_retry", {},
                                  f"{exc} Повторяем выбор действия по собранным данным.")
                    return self._result(context, "decide")
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
                  and (signature in seen or (name == "search_knowledge"
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
        if fresh and manifest_needs_refresh(run, name, args, fresh[0]):
            # The SDK already listed the live manifest. Present its change
            # before issuing any dependent actions from the old plan.
            calls = [{"name": "mcp_list_tools", "args": {
                "connection_id": args["connection_id"], "refresh": True,
            }}]
            engine._trace(run, "mcp_contract_refresh", args,
                          "Состав каталога MCP изменился. Получаем свежий список.")
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
        return self._result(
            context,
            "tools" if calls else "reflect",
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
