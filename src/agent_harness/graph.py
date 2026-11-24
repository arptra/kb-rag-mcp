"""Durable LangGraph orchestration with private threads and conservative tool recovery."""

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

from agent_harness.budgets import BUDGET_TOOL
from agent_harness.contracts import ConversationUnavailable, ModelDecisionError, Principal
from agent_harness.model_connection import RETRYABLE_FINISH_REASONS, ModelTransientError
from agent_harness.policy import TOOLS, validate_call
from agent_harness.research import (
    PLAN_TOOL,
    evidence_signature,
    initial_search_query,
    initialize_plan,
    lookup_key,
    needs_source_check,
    next_source_lookup,
    update_plan,
)
from agent_harness.store import timestamp

if TYPE_CHECKING:
    from agent_harness.runtime import HarnessRuntime


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
        builder.add_node("synthesize", self.synthesize)
        builder.add_edge(START, "prepare")
        for name in ("prepare", "plan", "collect", "inspect", "decide", "tools", "reflect"):
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
            pending = "tools" in snapshot.next or bool(checkpoint.get("calls"))
            if pending:
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
                    "attempted_lookups": [],
                    "quality_rounds": 0,
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
        await asyncio.gather(
            *(
                engine._search(
                    principal,
                    run,
                    event,
                    index,
                    query,
                    deadline=self._deadline(context) - engine._run_timeout(run) * 2 / 3
                    if run["mode"] == "quick"
                    else self._reserve_deadline(context),
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
        attempted = set(state.get("attempted_lookups", []))
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
            if any(entry.get("read_document") and entry["id"] == args["document_id"]
                   and entry["index_id"] == args["index_id"] for entry in run["evidence"]):
                continue
            result = await engine._read(context.principal, run, context.cancel, args,
                                        deadline=self._reserve_deadline(context))
            attempted.add(lookup_key("read_document", args))
            engine._trace(run, "read_document", args, result)
            count += 1
        return self._result(context, "decide", attempted_lookups=sorted(attempted))

    def _recover_draft(
        self, state: GraphState, context: ExecutionContext, issue: str,
    ) -> GraphState:
        run, engine = context.run, self.engine
        metrics = run["metrics"]
        metrics["rejected_drafts"] = metrics.get("rejected_drafts", 0) + 1
        round_number = state.get("quality_rounds", 0) + 1
        run["phase"] = "checking"
        engine._trace(run, "answer_quality_rejected", {}, issue)
        # First let the agent choose a domain-specific tool, including repository
        # reads or a configured MCP. Keep the last model call for reporting.
        decisions_left = engine._budget(run).model_calls - engine._decision_calls(run)
        if round_number == 1 and decisions_left > 2:
            if needs_source_check(run):
                run["_goal_review_requested"] = True
                engine._trace(run, "goal_check", {}, "Для вывода нужна проверка источником.")
            engine._trace(run, "research_recovery", {},
                          "Выберите зацепку из материалов и проверьте её подходящим инструментом. "
                          "Используйте новые данные и укажите подтверждающие источники в ответе.")
            return self._result(context, "decide", quality_rounds=round_number)
        recovery = self._try_source_lookup(state, context)
        if recovery:
            return recovery
        engine._trace(run, "research_recovery", {},
                      "Завершаем проверку черновика по уже собранным материалам.")
        return self._result(context, "synthesize")

    def _try_source_lookup(
        self, state: GraphState, context: ExecutionContext,
    ) -> GraphState | None:
        run, engine = context.run, self.engine
        round_number = state.get("quality_rounds", 0) + 1
        budget = engine._budget(run)
        if (round_number > engine._runtime_limits(run).max_stalled_rounds
                or context.cancel.is_set() or engine._now(run) >= self._reserve_deadline(context)
                or run["metrics"]["tool_calls"] >= budget.tool_calls
                or engine._followup_calls(run) >= budget.source_calls):
            return None
        knowledge = state.get("knowledge_ids", [])
        attempted = set(state.get("attempted_lookups", []))
        attempted.update(lookup_key("search_knowledge", {
            "index_id": index, "query": initial_search_query(run),
        }) for index in knowledge)
        lookup = next_source_lookup(engine._ranked_evidence(run), knowledge, attempted)
        if lookup is None:
            return None
        name, args = lookup["name"], lookup["args"]
        target = args.get("document_id") or args["query"]
        action = "Читаем документ" if name == "read_document" else "Уточняем поиск по"
        engine._trace(run, "research_recovery", args, f"{action} «{target}».")
        run["_round_evidence"] = evidence_signature(run)
        return self._result(context, "tools", calls=[lookup], recovery_lookup=True,
                            quality_rounds=round_number)

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
        catalog = [*([BUDGET_TOOL] if run["mode"] == "autonomous" else []),
                   PLAN_TOOL, *(TOOLS if state.get("knowledge_ids") else []),
                   *state.get("external", [])]
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
            if (run["mode"] == "autonomous" and not run["metrics"].get("planner_retries")
                    and engine._decision_calls(run) < budget.model_calls - 2
                    and engine._now(run) < self._reserve_deadline(context)):
                run["metrics"]["planner_retries"] = 1
                engine._trace(run, "model_retry", {},
                              f"{exc} Повторяем выбор инструмента для следующего шага.")
                return self._result(context, "decide")
            engine._trace(run, "model_retry", {},
                          f"{exc} Готовим итог повторным запросом без повторения действий.")
            return self._result(context, "synthesize")
        except ModelDecisionError:
            run["metrics"]["decision_errors"] = run["metrics"].get("decision_errors", 0) + 1
            engine._trace(run, "model_response", {},
                          "Некорректный формат предыдущего решения. Верните content и tool_calls; "
                          "используйте только перечисленные инструменты и их схемы аргументов.")
            if run["metrics"]["decision_errors"] < engine._runtime_limits(run).max_decision_errors:
                return self._result(context, "decide")
            engine._warn(run, "Модель не смогла сформировать корректные действия; готовим итог.")
            return self._result(context, "synthesize")
        calls = decision.get("tool_calls", [])
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
            plan = run.get("research_plan", {})
            missing_check = needs_source_check(run)
            no_actions = not plan.get("model_updated") and not run["metrics"]["tool_calls"]
            if (missing_check or no_actions) and not run.get("_goal_review_requested"):
                run["_goal_review_requested"] = True
                engine._trace(run, "goal_check", {},
                              "Выберите зацепку в найденных материалах и проверьте её: "
                              "уточните поиск, прочитайте документ, файл или запросите данные "
                              "доступным инструментом. Укажите результат или причину, "
                              "по которой проверка недоступна.")
                return self._result(context, "decide")
            if missing_check:
                recovery = self._try_source_lookup(state, context)
                if recovery:
                    return recovery
                return self._result(context, "synthesize")
            if plan.get("model_updated") and not plan.get("ready_to_answer"):
                if not run.get("_gap_review_requested"):
                    run["_gap_review_requested"] = True
                    engine._trace(run, "goal_check", {},
                                  "План ещё не завершён. Выполните проверки инструментами "
                                  "или укажите конкретную причину недоступности проверки.")
                    return self._result(context, "decide")
                # A planning-only response is never the final answer. A separate
                # synthesis call must report actual findings and remaining gaps.
                engine._trace(run, "goal_check", {},
                              "План не подтверждён как завершённый. Нужен итог по фактам; "
                              "перечислите непроверенные вопросы.")
                return self._result(context, "synthesize")
            return self._result(context, "done" if engine._answer(run, decision) else "synthesize")
        run["_round_evidence"] = evidence_signature(run)
        return self._result(
            context,
            "tools",
            calls=calls[: budget.tool_calls - run["metrics"]["tool_calls"]],
            exchange=state.get("exchange", []),
            recovery_lookup=False,
        )

    async def tools(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
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
            return self._result(context, "tools" if calls else "decide", calls=calls)
        name, args = str(call.get("name", "")), call.get("args", {})
        attempted = set(state.get("attempted_lookups", []))
        phases = {"search_knowledge": "refining", "read_document": "reading",
                  "read_repository": "reading_code", "search_repository": "reading_code",
                  "clone_repository": "reading_code", "run_in_sandbox": "running_code",
                  "update_research_plan": "planning", "request_budget": "budget_review"}
        run["phase"] = phases.get(name, "checking")
        run["current_action"] = {
            "name": name, "started_at": timestamp(),
            "args": {key: str(args[key])[:1000] for key in
                     ("index_id", "document_id", "connection_id", "repository_id")
                     if isinstance(args, dict) and key in args},
        }
        engine._checkpoint(principal, run, event)
        try:
            signature = hashlib.sha256(json.dumps(
                [name, args, evidence_signature(run)], sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            seen = run.setdefault("_seen_tool_calls", [])
            if name not in {"update_research_plan", "request_budget"} and signature in seen:
                result = "Повтор без новых данных пропущен. Измените запрос или источник."
            elif name == "request_budget":
                # Governance is accounted separately from source/tool execution.
                run["metrics"]["tool_calls"] -= 1
                result = engine._change_budget(run, args, started=context.started)
            elif name == "update_research_plan":
                update_plan(run, args)
                result = "План исследования обновлён. Проверки требуют подтверждений источниками."
            elif name in {item["function"]["name"] for item in state.get("external", [])}:
                seen.append(signature)
                result = await engine._external_tool(
                    principal, run, event, name, args, self._reserve_deadline(context)
                )
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
        except (ValueError, TypeError):
            result = "Вызов отклонён: недопустимый инструмент, аргументы или индекс."
            engine._warn(run, result)
            engine.memory.record_failure(
                principal.owner_id, run, operation="invalid", index_ids=run["index_ids"]
            )
        engine._trace(run, name, args, result)
        run["current_action"] = None
        return self._result(
            context,
            "tools" if calls else "reflect",
            calls=calls,
            attempted_lookups=sorted(attempted),
            recovery_lookup=False,
            exchange=[*state.get("exchange", []), {"name": name, "result": result}][-50:],
        )

    async def reflect(self, state: GraphState, runtime: Runtime[ExecutionContext]) -> GraphState:
        context = self._context(state, runtime)
        run = context.run
        initialize_plan(run)
        run["phase"] = "checking"
        plan = run["research_plan"]
        plan["round"] += 1
        changed = evidence_signature(run) != run.get("_round_evidence")
        run["_stalled_rounds"] = 0 if changed else run.get("_stalled_rounds", 0) + 1
        self.engine._trace(
            run, "research_round", {},
            "Получены новые материалы; проверяем оставшиеся вопросы."
            if changed else "Новых материалов нет; нужен другой запрос или источник.",
        )
        if run["_stalled_rounds"] >= self.engine._runtime_limits(run).max_stalled_rounds:
            if run["mode"] == "autonomous":
                recovery = self._try_source_lookup(state, context)
                if recovery:
                    return recovery
            run["stop_reason"] = "no_new_evidence"
            plan["completion_reason"] = "Поиск не даёт новых данных."
            self.engine._warn(run, "Повторные действия не дали новых данных; поиск ограничен.")
            return self._result(context, "synthesize", calls=[])
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
