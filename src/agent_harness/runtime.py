"""Bounded asynchronous investigation runtime, independent of any host or model SDK."""

from __future__ import annotations

import asyncio
import json
import time
from threading import Event
from typing import Any
from uuid import uuid4

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, ModelPort, Principal, RunRequest, SourcePort, ToolPort
from agent_harness.policy import BUDGETS, QUICK_BUDGETS, SYSTEM_PROMPT, TOOLS, validate_call
from agent_harness.store import ACTIVE, RunStore, timestamp


class QueueFullError(RuntimeError):
    """The selected execution lane has no backlog capacity."""


class HarnessUnavailableError(RuntimeError):
    """The optional service is disabled or has not started."""


class _AuthenticationTimeout(TimeoutError):
    """The run's cumulative allowance for human authorization was exhausted."""


class _RunClock:
    """Shared virtual clock: all work deadlines exclude bounded browser login time."""

    def __init__(self, authentication_limit: float, owner_id: str) -> None:
        self.limit = authentication_limit
        self.owner_id = owner_id
        self.waiting_since: float | None = None
        self.waited = 0.0
        self.checked = 0.0
        self.lock = asyncio.Lock()
        self.previous_phase = ""
        self.probe: asyncio.Task[dict[str, Any]] | None = None

    def authentication_elapsed(self, now: float) -> float:
        return self.waited + (now - self.waiting_since if self.waiting_since is not None else 0)

    def now(self) -> float:
        now = time.monotonic()
        return now - min(self.authentication_elapsed(now), self.limit)

    def observe(self, waiting: bool, now: float) -> None:
        if waiting and self.waiting_since is None:
            self.waiting_since = now
        elif not waiting and self.waiting_since is not None:
            self.waited += now - self.waiting_since
            self.waiting_since = None


class HarnessRuntime:
    def __init__(
        self,
        settings: HarnessSettings,
        source: SourcePort | None = None,
        model: ModelPort | None = None,
        tools: ToolPort | None = None,
    ) -> None:
        self.settings, self.source, self.model = settings, source, model
        self.tools = tools
        self._store: RunStore | None = None
        self._queues: dict[str, asyncio.Queue[tuple[Principal, str]]] = {
            lane: asyncio.Queue(maxsize=settings.queue_max) for lane in ("quick", "investigate")
        }
        self._workers: list[asyncio.Task[None]] = []
        self._signals: dict[str, Event] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._clocks: dict[str, _RunClock] = {}
        self._started = False

    @property
    def store(self) -> RunStore:
        if self._store is None:
            raise HarnessUnavailableError("Harness has not started")
        return self._store

    async def start(self) -> None:
        if self._started or not self.settings.enabled:
            return
        self._store = RunStore(self.settings)
        self.store.recover()
        self._started = True
        for lane, count in (
            ("quick", self.settings.quick_workers),
            ("investigate", self.settings.investigate_workers),
        ):
            self._workers.extend(asyncio.create_task(self._worker(lane)) for _ in range(count))

    async def stop(self) -> None:
        self._started = False
        for event in self._signals.values():
            event.set()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        for queue in self._queues.values():
            while not queue.empty():
                principal, run_id = queue.get_nowait()
                run = self.store.get(principal.owner_id, run_id)
                if run["status"] in ACTIVE:
                    run.update(status="cancelled", phase="finished", completed_at=timestamp())
                    self.store.save(principal.owner_id, run)
                queue.task_done()
        self._signals.clear()
        self._clocks.clear()

    async def submit(self, principal: Principal, request: RunRequest) -> dict[str, Any]:
        if not self._started or not self.settings.enabled:
            raise HarnessUnavailableError("Harness is disabled or stopped")
        if not principal.owner_id.strip():
            raise ValueError("Principal is required")
        parent = (
            self.store.get(principal.owner_id, request.parent_run_id)
            if request.parent_run_id
            else None
        )
        queue = self._queues[request.mode]
        if queue.full():
            raise QueueFullError("Очередь заполнена; повторите запрос позже.")
        run_id, now = uuid4().hex, timestamp()
        run = {
            "id": run_id,
            "run_id": run_id,
            **request.model_dump(),
            "status": "queued",
            "phase": "queued",
            "answer": "",
            "evidence": [],
            "warnings": [],
            "metrics": {"model_calls": 0, "source_calls": 0, "tool_calls": 0, "duration_ms": 0},
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "parent_question": parent["question"][:2000] if parent else None,
        }
        self.store.save(principal.owner_id, run)
        self.store.prune()
        self._signals[run_id] = Event()
        queue.put_nowait((principal, run_id))
        return run

    async def get(self, principal: Principal, run_id: str) -> dict[str, Any]:
        run = self.store.get(principal.owner_id, run_id)
        return self._visible(run, await self._indexes(principal))

    async def list_runs(self, principal: Principal) -> list[dict[str, Any]]:
        fields = {
            "id",
            "run_id",
            "question",
            "mode",
            "effort",
            "status",
            "phase",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "parent_run_id",
        }
        return [
            {key: value for key, value in run.items() if key in fields}
            for run in self.store.list(principal.owner_id)
        ]

    async def cancel(self, principal: Principal, run_id: str) -> dict[str, Any]:
        run = self.store.get(principal.owner_id, run_id)
        if run["status"] in ACTIVE:
            event = self._signals.get(run_id)
            if event is not None:
                event.set()
            if run["status"] == "queued":
                run.update(status="cancelled", phase="finished", completed_at=timestamp())
                self.store.save(principal.owner_id, run)
            else:
                run["status"] = "cancelling"
                self._warn(run, "Запрошена отмена; ожидается завершение текущего вызова.")
                self.store.save(principal.owner_id, run)
        return self._visible(run, await self._indexes(principal))

    async def status(self, principal: Principal) -> dict[str, Any]:
        indexes = await self._indexes(principal)
        model = await self._model_status()
        return {
            "enabled": self.settings.enabled,
            "started": self._started,
            "model": model,
            "source_available": self.source is not None or bool(indexes),
            "sources_available": self.source is not None or bool(indexes),
            "indexes": indexes,
            "queues": {
                lane: {
                    "queued": queue.qsize(),
                    "workers": getattr(self.settings, f"{lane}_workers"),
                }
                for lane, queue in self._queues.items()
            },
            "limits": {
                "queue_max": self.settings.queue_max,
                "max_indexes": self.settings.max_indexes,
                "quick_timeout_seconds": self.settings.quick_timeout_seconds,
                "investigate_timeout_seconds": self.settings.investigate_timeout_seconds,
                "authentication_timeout_seconds": self.settings.authentication_timeout_seconds,
                "effort_policy": {
                    mode: {
                        effort: {
                            "source_calls": profile.source_calls,
                            "model_calls": profile.model_calls,
                            "context_chars": max(
                                12_000,
                                int(
                                    getattr(self.settings, f"{mode}_context_chars")
                                    * profile.context_ratio
                                ),
                            ),
                            "evidence_per_index": min(
                                self.settings.results_per_index, profile.evidence_per_index
                            ),
                            "evidence_chars": min(
                                self.settings.evidence_chars, profile.evidence_chars
                            ),
                        }
                        for effort, profile in profiles.items()
                    }
                    for mode, profiles in (("quick", QUICK_BUDGETS), ("investigate", BUDGETS))
                },
            },
        }

    async def _indexes(
        self, principal: Principal, timeout: float | None = None
    ) -> list[dict[str, str]]:
        async def catalog(provider: SourcePort | ToolPort) -> list[dict[str, str]]:
            try:
                return await asyncio.wait_for(
                    provider.indexes(principal),
                    timeout or self.settings.source_timeout_seconds,
                )
            except Exception:
                return []

        catalogs = await asyncio.gather(
            *(catalog(provider) for provider in (self.source, self.tools) if provider is not None)
        )
        return list(
            {
                str(item["id"]): {"id": str(item["id"]), "name": str(item.get("name", item["id"]))}
                for items in catalogs
                for item in items
                if item.get("id")
            }.values()
        )[:1000]

    @staticmethod
    def _visible(run: dict[str, Any], indexes: list[dict[str, str]]) -> dict[str, Any]:
        allowed = {item["id"] for item in indexes}
        if any(item["index_id"] not in allowed for item in run["evidence"]):
            run["evidence"], run["answer"] = [], ""
            run["warnings"].append("Результат скрыт: доступ к части источников отсутствует.")
        return run

    async def _model_status(self) -> dict[str, Any]:
        if self.model is None:
            return {
                "available": False,
                "reason": "Модель не подключена; итоговая генерация отключена.",
            }
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self.model.status), 5)
            return {
                **result,
                "available": bool(result.get("available", False)),
                "reason": str(result.get("reason", "")),
            }
        except Exception:
            return {
                "available": False,
                "reason": "Проверка доступности модели завершилась ошибкой.",
            }

    async def _worker(self, lane: str) -> None:
        queue = self._queues[lane]
        while True:
            principal, run_id = await queue.get()
            try:
                run = self.store.get(principal.owner_id, run_id)
                if run["status"] == "queued":
                    await self._execute(principal, run)
            finally:
                self._signals.pop(run_id, None)
                self._semaphores.pop(run_id, None)
                clock = self._clocks.pop(run_id, None)
                if clock is not None and clock.probe is not None:
                    clock.probe.cancel()
                    await asyncio.gather(clock.probe, return_exceptions=True)
                queue.task_done()

    async def _execute(self, principal: Principal, run: dict[str, Any]) -> None:
        event = self._signals[run["id"]]
        started = time.monotonic()
        timeout = getattr(self.settings, f"{run['mode']}_timeout_seconds")
        deadline = started + timeout
        self._clocks[run["id"]] = _RunClock(
            self.settings.authentication_timeout_seconds,
            principal.owner_id,
        )
        run.update(status="running", phase="searching", started_at=timestamp())
        self.store.save(principal.owner_id, run)
        operation = asyncio.create_task(self._research(principal, run, event, deadline))
        terminal: str | None = None
        try:
            while not operation.done():
                await asyncio.wait({operation}, timeout=0.05)
                if not event.is_set():
                    await self._refresh_authentication(run)
                if event.is_set() or self._now(run) >= deadline:
                    terminal = "cancelled" if event.is_set() else "timed_out"
                    event.set()
                    await self._cleanup(operation, run)
                    break
            if terminal is None:
                await operation
        except _AuthenticationTimeout:
            terminal = "cancelled" if event.is_set() else "timed_out"
            event.set()
            if terminal == "timed_out":
                self._warn(
                    run,
                    "Время ожидания авторизации истекло. Администратору нужно завершить вход "
                    "в GigaCode, затем повторить исследование.",
                )
            await self._cleanup(operation, run)
        except asyncio.CancelledError:
            event.set()
            terminal = "cancelled"
            await self._cleanup(operation, run)
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        except Exception as exc:
            terminal = "partial" if run["evidence"] else "failed"
            run["warnings"].append(f"Исследование не завершено ({type(exc).__name__}).")
        finally:
            if self.tools is not None:
                try:
                    await asyncio.wait_for(
                        self.tools.close_run(run["id"]),
                        self.settings.cleanup_timeout_seconds,
                    )
                except Exception:
                    self._warn(run, "Очистка песочницы не подтверждена; сообщите администратору.")
            if terminal:
                run["status"] = terminal
            if not run["answer"]:
                self._evidence_answer(run)
            run.update(phase="finished", completed_at=timestamp())
            run["metrics"]["duration_ms"] = round((time.monotonic() - started) * 1000)
            self.store.save(principal.owner_id, run)
            self.store.prune()

    def _now(self, run: dict[str, Any]) -> float:
        clock = self._clocks.get(run["id"])
        return clock.now() if clock is not None else time.monotonic()

    async def _refresh_authentication(self, run: dict[str, Any]) -> None:
        clock = self._clocks.get(run["id"])
        if clock is None or self.model is None:
            return
        async with clock.lock:
            now = time.monotonic()
            if clock.authentication_elapsed(now) >= clock.limit:
                raise _AuthenticationTimeout
            if now - clock.checked < 0.02:
                return
            clock.checked = now
            try:
                if clock.probe is None:
                    clock.probe = asyncio.create_task(asyncio.to_thread(self.model.status))
                status = await asyncio.wait_for(asyncio.shield(clock.probe), timeout=0.2)
                clock.probe = None
            except TimeoutError:
                # Keep awaiting the same probe; do not accumulate blocked status threads.
                return
            except Exception:
                clock.probe = None
                # A slow or broken status provider cannot create unbounded extra time.
                return
            waiting = status.get("state") == "authentication_required"
            was_waiting = clock.waiting_since is not None
            clock.observe(waiting, time.monotonic())
            if waiting and not was_waiting:
                clock.previous_phase = run["phase"]
                run["phase"] = "authentication_required"
                self.store.save(clock.owner_id, run)
            elif was_waiting and not waiting:
                if run["phase"] == "authentication_required":
                    run["phase"] = clock.previous_phase
                    self.store.save(clock.owner_id, run)
            if clock.authentication_elapsed(time.monotonic()) >= clock.limit:
                raise _AuthenticationTimeout

    async def _cleanup(self, operation: asyncio.Task[Any], run: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(operation), self.settings.cleanup_timeout_seconds)
        except TimeoutError:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            run["warnings"].append("Провайдер не подтвердил очистку за отведённое время.")
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _research(
        self, principal: Principal, run: dict[str, Any], event: Event, deadline: float
    ) -> None:
        available = {
            item["id"] for item in await self._indexes(principal, self._source_timeout(run))
        }
        budget = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        self._semaphores[run["id"]] = asyncio.Semaphore(budget.parallelism)
        requested = list(dict.fromkeys(run["index_ids"]))
        if set(requested) - available:
            run["status"] = "failed"
            run["warnings"].append("Один или несколько выбранных индексов недоступны.")
            return
        selected = requested or sorted(available)
        if len(selected) > self.settings.max_indexes:
            run["warnings"].append("Область поиска ограничена бюджетом числа индексов.")
        run["index_ids"] = selected[: self.settings.max_indexes]
        if not run["index_ids"]:
            run["status"] = "partial"
            run["warnings"].append("Нет доступных источников; состояние системы не проверено.")
            return
        query = run["question"]
        if run["parent_question"]:
            query = run["parent_question"] + "\n" + "Дополнительный вопрос: " + query
        external = (
            await self.tools.describe(principal, run["index_ids"])
            if self.tools is not None and run["mode"] == "investigate"
            else []
        )
        external_ids = (
            {item["id"] for item in await self.tools.indexes(principal)}
            if self.tools is not None
            else set()
        )
        knowledge_ids = [item for item in run["index_ids"] if item not in external_ids]
        if run["mode"] == "quick" and set(run["index_ids"]) & external_ids:
            self._warn(run, "Быстрый ответ использует RAG. Код, MCP и A2A доступны в исследовании.")
        search_limit = max(1, budget.source_calls // 2) if external else budget.source_calls
        bootstrap = knowledge_ids[:search_limit] if self.source is not None else []
        if len(bootstrap) < len(knowledge_ids):
            self._warn(run, "Часть индексов не опрошена: исчерпан бюджет источников.")
        await asyncio.gather(
            *(
                self._search(
                    principal,
                    run,
                    event,
                    index,
                    query[:8000],
                    deadline=deadline - self.settings.quick_timeout_seconds / 3
                    if run["mode"] == "quick"
                    else None,
                )
                for index in bootstrap
            )
        )
        self.store.save(principal.owner_id, run)
        if event.is_set():
            return
        model_status = await self._model_status()
        while model_status.get("state") == "authentication_required" and not event.is_set():
            await self._refresh_authentication(run)
            self.store.save(principal.owner_id, run)
            await asyncio.sleep(0.025)
            model_status = await self._model_status()
        if event.is_set():
            return
        await self._refresh_authentication(run)
        if not model_status["available"] or (not run["evidence"] and not external):
            run["status"] = "partial"
            run["warnings"].append(
                model_status["reason"]
                if not model_status["available"]
                else "Подтверждающие источники не найдены."
            )
            return
        if run["mode"] == "quick":
            await self._synthesize(principal, run, event)
            return
        reserve = min(
            self.settings.final_reserve_seconds, self.settings.investigate_timeout_seconds / 4
        )
        exchange: list[dict[str, Any]] = []
        for _ in range(budget.model_calls - 1):
            if event.is_set() or self._now(run) >= deadline - reserve:
                break
            if run["metrics"]["tool_calls"] >= budget.tool_calls:
                break
            if run["metrics"]["source_calls"] >= budget.source_calls:
                break
            run["phase"] = "investigating"
            self.store.save(principal.owner_id, run)
            try:
                tool_catalog = [*(TOOLS if knowledge_ids else []), *external]
                decision = await self._decide(
                    run, event, tool_catalog, exchange, deadline - reserve
                )
            except _AuthenticationTimeout:
                raise
            except TimeoutError:
                self._warn(run, "Время поиска исчерпано; зарезервирован итоговый ответ.")
                break
            calls = decision.get("tool_calls", [])
            if not calls:
                if self._answer(run, decision):
                    return
                break
            exchange = []
            for call in calls[: budget.tool_calls - run["metrics"]["tool_calls"]]:
                if event.is_set() or self._now(run) >= deadline - reserve:
                    break
                if run["metrics"]["source_calls"] >= budget.source_calls:
                    break
                run["metrics"]["tool_calls"] += 1
                if not isinstance(call, dict):
                    self._warn(run, "Некорректный формат вызова инструмента отклонён.")
                    continue
                name, args = str(call.get("name", "")), call.get("args", {})
                try:
                    if name in {item["function"]["name"] for item in external}:
                        result = await self._external_tool(
                            principal,
                            run,
                            event,
                            name,
                            args,
                            deadline - reserve,
                        )
                        exchange.append({"name": name, "result": result})
                        continue
                    validated = validate_call(name, args, set(knowledge_ids))
                    if name == "search_knowledge":
                        result = await self._search(
                            principal,
                            run,
                            event,
                            validated["index_id"],
                            validated["query"],
                            validated["limit"],
                        )
                    else:
                        result = await self._read(principal, run, event, validated)
                except (ValueError, TypeError):
                    result = "Вызов отклонён: недопустимый инструмент, аргументы или индекс."
                    self._warn(run, result)
                exchange.append({"name": name, "result": result})
            self.store.save(principal.owner_id, run)
        if not event.is_set():
            self._warn(run, "Исследование ограничено бюджетом; итог по собранным источникам.")
            await self._synthesize(principal, run, event)

    async def _external_tool(
        self,
        principal: Principal,
        run: dict[str, Any],
        event: Event,
        name: str,
        args: Any,
        deadline: float,
    ) -> str:
        assert self.tools is not None
        run["metrics"]["source_calls"] += 1
        try:
            timeout = min(600, max(0.01, deadline - self._now(run)))
            items = await asyncio.wait_for(
                self.tools.call(
                    name,
                    args,
                    principal=principal,
                    run_id=run["id"],
                    index_ids=run["index_ids"],
                    cancel=event,
                ),
                timeout,
            )
            accepted = 0
            for item in items:
                if item.index_id not in run["index_ids"]:
                    raise ValueError("Tool evidence is outside run scope")
                accepted += self._add_evidence(run, item, item.index_id)
            return f"Получено подтверждений: {accepted}."
        except Exception as exc:
            message = f"Инструмент {name} недоступен ({type(exc).__name__})."
            self._warn(run, message)
            return message
        finally:
            self._checkpoint(principal, run, event)

    async def _search(
        self,
        principal: Principal,
        run: dict[str, Any],
        event: Event,
        index_id: str,
        query: str,
        limit: int | None = None,
        deadline: float | None = None,
    ) -> str:
        if event.is_set() or self.source is None:
            return "Вызов отменён либо источник отключён."
        profile = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        count = min(
            limit or self.settings.results_per_index,
            self.settings.results_per_index,
            profile.evidence_per_index,
        )
        try:
            async with self._semaphores[run["id"]]:
                if event.is_set():
                    return "Вызов отменён."
                timeout = self._source_timeout(run)
                if deadline is not None:
                    timeout = min(timeout, deadline - self._now(run))
                if timeout <= 0:
                    self._warn(run, "Время поиска исчерпано; зарезервирован итоговый ответ.")
                    return "Время поиска исчерпано."
                run["metrics"]["source_calls"] += 1
                items = await asyncio.wait_for(
                    self.source.search(
                        query=query,
                        index_id=index_id,
                        limit=count,
                        principal=principal,
                        cancel=event,
                    ),
                    timeout,
                )
            accepted = sum(self._add_evidence(run, item, index_id) for item in items[:count])
            return f"Получено подтверждений: {accepted}."
        except Exception as exc:
            outcome = f"Поиск в {index_id} недоступен ({type(exc).__name__})."
            self._warn(run, outcome)
            return outcome
        finally:
            self._checkpoint(principal, run, event)

    async def _read(
        self, principal: Principal, run: dict[str, Any], event: Event, args: dict[str, Any]
    ) -> str:
        if event.is_set() or self.source is None:
            return "Вызов отменён либо источник отключён."
        run["metrics"]["source_calls"] += 1
        try:
            item = await asyncio.wait_for(
                self.source.read(**args, principal=principal, cancel=event),
                self._source_timeout(run),
            )
            return (
                "Документ прочитан; подтверждение включено в контекст."
                if self._add_evidence(run, item, args["index_id"])
                else "Документ не включён: ограничение области доступа или бюджета."
            )
        except Exception as exc:
            outcome = f"Чтение документа недоступно ({type(exc).__name__})."
            self._warn(run, outcome)
            return outcome
        finally:
            self._checkpoint(principal, run, event)

    def _checkpoint(self, principal: Principal, run: dict[str, Any], event: Event) -> None:
        if event.is_set() and run["status"] in ACTIVE:
            run["status"] = "cancelling"
        self.store.save(principal.owner_id, run)

    def _source_timeout(self, run: dict[str, Any]) -> float:
        return (
            min(self.settings.source_timeout_seconds, self.settings.quick_timeout_seconds / 3)
            if run["mode"] == "quick"
            else self.settings.source_timeout_seconds
        )

    def _add_evidence(self, run: dict[str, Any], item: Evidence, index_id: str) -> bool:
        item = Evidence.model_validate(item)
        if item.index_id != index_id:
            self._warn(run, "Источник вернул evidence за пределами запрошенного индекса.")
            return False
        payload = item.model_dump()
        profile = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        text_limit = (
            min(8000, self.settings.investigate_context_chars // 3)
            if item.kind == "tool_metadata"
            else min(self.settings.evidence_chars, profile.evidence_chars)
        )
        payload["text"] = payload["text"][:text_limit]
        payload["title"] = payload["title"][:300]
        payload["id"] = payload["id"][:1000]
        payload["url"] = payload["url"][:2000] if payload["url"] else None
        payload["revision"] = payload["revision"][:200] if payload["revision"] else None
        for i, existing in enumerate(run["evidence"]):
            if (existing["id"], existing["index_id"]) == (payload["id"], index_id):
                run["evidence"][i] = payload
                return True
        if len(run["evidence"]) < self.settings.max_evidence:
            run["evidence"].append(payload)
            return True
        return False

    async def _decide(
        self,
        run: dict[str, Any],
        event: Event,
        tools: list[dict[str, Any]],
        exchange: list[dict[str, Any]] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        assert self.model is not None
        profile = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        maximum = max(
            12_000,
            int(getattr(self.settings, f"{run['mode']}_context_chars") * profile.context_ratio),
        )
        context: dict[str, Any] = {
            "allowed_indexes": [],
            "selected_index_count": len(run["index_ids"]),
            "index_catalog_truncated": False,
            "evidence": [],
            "prior_tool_attempts": exchange or [],
            "history_question_not_evidence": run["parent_question"],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": run["question"]},
        ]
        tool_reserve = max(4500, len(json.dumps(tools, ensure_ascii=False)) + 1000)
        remaining = maximum - len(json.dumps(messages, ensure_ascii=False)) - tool_reserve
        for item in run["evidence"]:
            catalog = list(dict.fromkeys([*context["allowed_indexes"], item["index_id"]]))
            candidate = {
                **context,
                "allowed_indexes": catalog,
                "evidence": [*context["evidence"], item],
            }
            if len(json.dumps(candidate, ensure_ascii=False)) > remaining:
                self._warn(run, "Часть evidence не помещается в контекст модели.")
                continue
            context = candidate
        catalog_budget = min(4000, max(0, remaining // 3))
        for index_id in run["index_ids"]:
            if index_id in context["allowed_indexes"]:
                continue
            catalog = [*context["allowed_indexes"], index_id]
            candidate = {**context, "allowed_indexes": catalog}
            if len(json.dumps(catalog, ensure_ascii=False)) > catalog_budget:
                continue
            if len(json.dumps(candidate, ensure_ascii=False)) <= remaining:
                context = candidate
        if len(context["allowed_indexes"]) < len(run["index_ids"]):
            context["index_catalog_truncated"] = True
            self._warn(
                run,
                "Каталог индексов для модели ограничен контекстом; "
                "полная выбранная область сохранена в запуске.",
            )
        messages.append({"role": "user", "content": json.dumps(context, ensure_ascii=False)})
        if len(json.dumps(messages, ensure_ascii=False)) > maximum - tool_reserve + 500:
            raise ValueError("Input context budget exceeded")
        run["metrics"]["model_calls"] += 1
        # Model cancellation is separate from user cancellation so an exhausted
        # authentication/decision budget retains its timed_out outcome.
        signal = Event()
        operation = asyncio.create_task(
            self.model.decide(
                messages=messages, tools=tools, cancel=signal, max_input_chars=maximum
            )
        )
        try:
            while not operation.done():
                await asyncio.wait({operation}, timeout=0.025)
                if not event.is_set():
                    await self._refresh_authentication(run)
                if event.is_set() or (deadline is not None and self._now(run) >= deadline):
                    signal.set()
                    await self._cleanup(operation, run)
                    if event.is_set():
                        raise asyncio.CancelledError
                    raise TimeoutError("Investigation decision budget exhausted")
            result = await operation
        except _AuthenticationTimeout:
            signal.set()
            await self._cleanup(operation, run)
            if event.is_set():
                raise asyncio.CancelledError from None
            raise
        except asyncio.CancelledError:
            signal.set()
            await self._cleanup(operation, run)
            raise
        if not isinstance(result, dict) or not isinstance(result.get("tool_calls", []), list):
            raise ValueError("Invalid model decision")
        return result

    async def _synthesize(self, principal: Principal, run: dict[str, Any], event: Event) -> None:
        run["phase"] = "synthesizing"
        self._checkpoint(principal, run, event)
        decision = await self._decide(run, event, [])
        if decision.get("tool_calls"):
            self._warn(
                run, "Модель запросила инструменты на этапе итогового ответа; вызов отклонён."
            )
            run["status"] = "partial"
            return
        if not self._answer(run, decision):
            run["status"] = "partial"
            self._warn(run, "Модель не вернула итоговый ответ.")

    def _answer(self, run: dict[str, Any], decision: dict[str, Any]) -> bool:
        content = decision.get("content")
        if not isinstance(content, str) or not content.strip():
            return False
        run["answer"] = content.strip()[: self.settings.answer_chars]
        if not any(item.get("kind", "source") == "source" for item in run["evidence"]):
            self._warn(run, "Ответ не подтверждён источниками; состояние системы не проверено.")
        run["status"] = "partial" if run["warnings"] else "completed"
        return True

    @staticmethod
    def _warn(run: dict[str, Any], message: str) -> None:
        if message not in run["warnings"] and len(run["warnings"]) < 30:
            run["warnings"].append(message)

    @staticmethod
    def _evidence_answer(run: dict[str, Any]) -> None:
        run["answer"] = (
            "Найдены фрагменты источников. Объяснение системы не сформировано; "
            "проверьте найденные подтверждения и ограничения ниже."
            if run["evidence"]
            else "Подтверждённых данных для ответа пока нет."
        )
