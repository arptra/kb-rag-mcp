"""Bounded asynchronous investigation runtime, independent of any host or model SDK."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import AsyncExitStack
from threading import Event
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from agent_harness.config import HarnessSettings
from agent_harness.contracts import (
    Evidence,
    ModelDecisionError,
    ModelPort,
    Principal,
    RunRequest,
    SourcePort,
    ToolPort,
)
from agent_harness.graph import ExecutionContext, HarnessGraph
from agent_harness.memory import MAX_PER_OWNER, MemoryFeedback, MemoryInput, MemoryStore
from agent_harness.model_connection import ModelConnectionError
from agent_harness.policy import BUDGETS, QUICK_BUDGETS, SYSTEM_PROMPT
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
        self._memory: MemoryStore | None = None
        self._graph: HarnessGraph | None = None
        self._graph_lifespan = AsyncExitStack()
        self._queues: dict[str, asyncio.Queue[tuple[Principal, str]]] = {
            lane: asyncio.Queue(maxsize=settings.queue_max) for lane in ("quick", "investigate")
        }
        self._workers: list[asyncio.Task[None]] = []
        self._signals: dict[str, Event] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._clocks: dict[str, _RunClock] = {}
        self._last_checkpoints: dict[str, float] = {}
        self._checkpoint_evidence_counts: dict[str, int] = {}
        self._started = False

    @property
    def store(self) -> RunStore:
        if self._store is None:
            raise HarnessUnavailableError("Harness has not started")
        return self._store

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            raise HarnessUnavailableError("Harness has not started")
        return self._memory

    async def start(self) -> None:
        if self._started or not self.settings.enabled:
            return
        self._store = RunStore(self.settings)
        self._memory = MemoryStore(self.store)
        self.store.recover()
        checkpoint_path = self.settings.state_dir / "checkpoints.sqlite3"
        checkpoint_path.touch(mode=0o600, exist_ok=True)
        checkpoint_path.chmod(0o600)
        checkpointer = await self._graph_lifespan.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(checkpoint_path))
        )
        await checkpointer.setup()
        await checkpointer.conn.execute("PRAGMA busy_timeout=5000")
        self._graph = HarnessGraph(self, checkpointer)
        await self._graph.prune()
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
                try:
                    run = self.store.get(principal.owner_id, run_id)
                except KeyError:
                    queue.task_done()
                    continue
                if run["status"] in ACTIVE:
                    run.update(status="cancelled", phase="finished", completed_at=timestamp())
                    self.store.save(principal.owner_id, run)
                queue.task_done()
        self._signals.clear()
        self._clocks.clear()
        await self._graph_lifespan.aclose()
        self._graph = None

    async def submit(self, principal: Principal, request: RunRequest) -> dict[str, Any]:
        if not self._started or not self.settings.enabled:
            raise HarnessUnavailableError("Harness is disabled or stopped")
        if not principal.owner_id.strip():
            raise ValueError("Principal is required")
        if request.parent_run_id:
            self.store.get(principal.owner_id, request.parent_run_id)
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
            "metrics": {"model_calls": 0, "source_calls": 0, "bootstrap_calls": 0,
                        "document_reads": 0, "tool_calls": 0, "duration_ms": 0},
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "parent_question": None,
            "memory_used": [],
            "context_index_ids": [],
            "resume_count": 0,
            "resumable": False,
            "coverage": {},
            "research_trace": [],
        }
        self.store.save(principal.owner_id, run)
        self.store.prune()
        self._signals[run_id] = Event()
        queue.put_nowait((principal, run_id))
        return run

    async def get(self, principal: Principal, run_id: str) -> dict[str, Any]:
        run = self.store.get(principal.owner_id, run_id)
        run["resumable"] = await self._can_resume(principal, run)
        return self._visible(run, await self._indexes(principal))

    async def delete_run(self, principal: Principal, run_id: str) -> dict[str, Any]:
        deleted = self.store.delete(principal.owner_id, run_id)
        if self._graph is not None:
            for item in deleted:
                await self._graph.delete(principal, item)
        return {"deleted": True, "id": run_id, "deleted_ids": deleted}

    async def _can_resume(self, principal: Principal, run: dict[str, Any]) -> bool:
        if (
            self._graph is None
            or run["status"] not in {"interrupted", "timed_out", "failed", "partial", "cancelled"}
            or run.get("resume_count", 0) >= 3
            or not {"mode", "effort", "metrics"} <= run.keys()
        ):
            return False
        budget = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        if run["metrics"]["model_calls"] >= budget.model_calls:
            return False
        if run["mode"] == "investigate" and self._followup_calls(run) >= budget.source_calls:
            return False
        return await self._graph.has_checkpoint(principal, run["id"])

    async def resume(
        self, principal: Principal, run_id: str, index_ids: list[str] | None = None
    ) -> dict[str, Any]:
        if not self._started:
            raise HarnessUnavailableError("Harness has not started")
        run = self.store.get(principal.owner_id, run_id)
        if not await self._can_resume(principal, run):
            raise ValueError("Продолжение недоступно или бюджет исчерпан. Создайте новый запрос.")
        if index_ids is not None and (not index_ids or len(index_ids) > self.settings.max_indexes):
            raise ValueError("Выберите допустимую область источников.")
        allowed = {item["id"] for item in await self._indexes(principal)}
        selected = set(index_ids if index_ids is not None else run["index_ids"])
        if not selected <= allowed:
            raise ValueError("Доступ к одному из выбранных источников отсутствует.")
        queue = self._queues[run["mode"]]
        if queue.full():
            raise QueueFullError("Очередь заполнена; повторите запрос позже.")
        current = self.store.get(principal.owner_id, run_id)
        if current["status"] in ACTIVE or current.get("resume_count", 0) != run.get(
            "resume_count", 0
        ):
            raise ValueError("Продолжение этого исследования уже запущено.")
        # Fail closed on hidden dependencies, including a previously used note.
        if self._run_scope(run) - (allowed & selected):
            run["evidence"], run["answer"], run["memory_used"] = [], "", []
            run["context_index_ids"], run["parent_question"] = [], None
            run["research_trace"], run["coverage"], run["context_summary"] = [], {}, {}
            self._warn(run, "Область изменилась; прежние данные исключены из продолжения.")
        run.update(
            status="queued",
            phase="queued",
            completed_at=None,
            answer="",
            index_ids=sorted(selected),
            resume_count=run.get("resume_count", 0) + 1,
            resumable=False,
            _resume_checkpoint=True,
        )
        self.store.save(principal.owner_id, run)
        self._signals[run_id] = Event()
        queue.put_nowait((principal, run_id))
        return {key: value for key, value in run.items() if not key.startswith("_")}

    async def list_memory(self, principal: Principal) -> dict[str, Any]:
        allowed = {item["id"] for item in await self._indexes(principal)}
        return {
            "enabled": self.memory.enabled(principal.owner_id),
            "memories": [
                self.memory.public(item) for item in self.memory.list(principal.owner_id, allowed)
            ],
            "limits": {"max_per_owner": MAX_PER_OWNER},
        }

    async def save_memory(
        self, principal: Principal, request: MemoryInput, memory_id: str | None = None
    ) -> dict[str, Any]:
        allowed = {item["id"] for item in await self._indexes(principal)}
        question = ""
        existing = self.memory.get(principal.owner_id, memory_id) if memory_id else None
        # Editing never lets an old note shed a restricted source dependency.
        scope = set(request.index_ids) | set((existing or {}).get("index_ids", []))
        source_run_id = (existing or {}).get("source_run_id") or request.source_run_id
        if source_run_id:
            source = self.store.get(principal.owner_id, source_run_id)
            if source["status"] in ACTIVE:
                raise ValueError("Сначала дождитесь завершения исследования.")
            scope |= self._run_scope(source)
            question = source["question"]
        if not scope <= allowed:
            raise ValueError("Доступ к источникам этой записи отсутствует.")
        request = request.model_copy(
            update={"index_ids": sorted(scope), "source_run_id": source_run_id}
        )
        return self.memory.save(
            principal.owner_id, request, memory_id=memory_id, match_question=question
        )

    async def delete_memory(self, principal: Principal, memory_id: str) -> dict[str, Any]:
        self.memory.delete(principal.owner_id, memory_id)
        return {"deleted": True, "id": memory_id}

    async def set_memory_enabled(self, principal: Principal, enabled: bool) -> dict[str, Any]:
        self.memory.set_enabled(principal.owner_id, enabled)
        return {"enabled": enabled}

    async def set_run_feedback(
        self, principal: Principal, run_id: str, request: MemoryFeedback
    ) -> dict[str, Any]:
        run = self.store.get(principal.owner_id, run_id)
        if run["status"] in ACTIVE:
            raise ValueError("Дождитесь завершения ответа, чтобы оценить результат.")
        allowed = {item["id"] for item in await self._indexes(principal)}
        scope = self._run_scope(run)
        if not scope <= allowed:
            raise ValueError("Доступ к источникам этого ответа отсутствует.")
        run["feedback"] = request.model_dump()
        self.store.save(principal.owner_id, run)
        memory = self.memory.record_feedback(
            principal.owner_id, run, rating=request.rating, comment=request.comment,
            index_ids=sorted(scope),
        )
        return {"feedback": run["feedback"], "memory": memory}

    @staticmethod
    def _run_scope(run: dict[str, Any]) -> set[str]:
        return (
            set(run.get("index_ids", []))
            | set(run.get("context_index_ids", []))
            | {item["index_id"] for item in run["evidence"]}
        )

    def _parent_context(
        self, principal: Principal, run: dict[str, Any], allowed: set[str]
    ) -> dict[str, str] | None:
        if not run.get("parent_run_id"):
            return None
        try:
            parent = self.store.get(principal.owner_id, run["parent_run_id"])
        except KeyError:
            return None
        scope = self._run_scope(parent)
        if parent["status"] in ACTIVE or not scope <= allowed & set(run["index_ids"]):
            return None
        run["context_index_ids"] = sorted(set(run.get("context_index_ids", [])) | scope)
        return {"question": parent["question"][:1000], "answer": parent["answer"][:1600]}

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
                            "initial_search": "all_selected_indexes",
                            "model_calls": profile.model_calls,
                            "document_reads": profile.document_reads,
                            "timeout_seconds": getattr(self.settings, f"{mode}_timeout_seconds")
                            * profile.time_ratio,
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
        if HarnessRuntime._run_scope(run) - allowed:
            run["evidence"], run["answer"] = [], ""
            run["memory_used"], run["parent_question"] = [], None
            run["research_trace"], run["coverage"], run["context_summary"] = [], {}, {}
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
                try:
                    run = self.store.get(principal.owner_id, run_id)
                except KeyError:
                    continue
                if run["status"] == "queued":
                    await self._execute(principal, run)
            finally:
                self._signals.pop(run_id, None)
                self._semaphores.pop(run_id, None)
                self._last_checkpoints.pop(run_id, None)
                self._checkpoint_evidence_counts.pop(run_id, None)
                clock = self._clocks.pop(run_id, None)
                if clock is not None and clock.probe is not None:
                    clock.probe.cancel()
                    await asyncio.gather(clock.probe, return_exceptions=True)
                queue.task_done()

    async def _execute(self, principal: Principal, run: dict[str, Any]) -> None:
        event = self._signals[run["id"]]
        started = time.monotonic()
        previous_duration = run["metrics"]["duration_ms"]
        timeout = self._run_timeout(run)
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
        except ModelConnectionError as exc:
            terminal = "partial" if run["evidence"] else "failed"
            self._warn(run, str(exc))
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
            run["metrics"]["duration_ms"] = previous_duration + round(
                (time.monotonic() - started) * 1000
            )
            self.store.save(principal.owner_id, run)
            self.memory.record_outcome(
                principal.owner_id, run, index_ids=sorted(self._run_scope(run))
            )
            self.store.prune()
            if self._graph is not None:
                await self._graph.prune()

    def _run_timeout(self, run: dict[str, Any]) -> float:
        profile = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        return float(getattr(self.settings, f"{run['mode']}_timeout_seconds") * profile.time_ratio)

    @staticmethod
    def _followup_calls(run: dict[str, Any]) -> int:
        return max(0, int(run["metrics"]["source_calls"])
                   - int(run["metrics"].get("bootstrap_calls", 0)))

    @staticmethod
    def _trace(run: dict[str, Any], name: str, args: Any, result: str) -> None:
        # Store only identification fields. Shell commands, tokens and arbitrary
        # remote tool arguments must not be copied into persistent diagnostics.
        identifiers = {key: str(args[key])[:1000] for key in
                       ("index_id", "document_id", "connection_id", "repository_id")
                       if isinstance(args, dict) and key in args}
        run["research_trace"] = [*run.get("research_trace", []), {
            "name": name, "args": identifiers, "result": result[:1000],
        }][-50:]

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
        assert self._graph is not None
        await self._graph.execute(
            ExecutionContext(principal, run, event, deadline),
            resume=bool(run.pop("_resume_checkpoint", False)),
        )

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
            environment = (
                {"sandbox_profile_id": run["sandbox_profile_id"]}
                if run.get("sandbox_profile_id")
                else {}
            )
            items = await asyncio.wait_for(
                self.tools.call(
                    name,
                    args,
                    principal=principal,
                    run_id=run["id"],
                    index_ids=run["index_ids"],
                    cancel=event,
                    **environment,
                ),
                timeout,
            )
            accepted = 0
            for item in items:
                if item.index_id not in run["index_ids"]:
                    raise ValueError("Tool evidence is outside run scope")
                accepted += self._add_evidence(run, item, item.index_id, fresh=True)
            return f"Получено подтверждений: {accepted}."
        except Exception as exc:
            message = f"Инструмент {name} недоступен ({type(exc).__name__})."
            self._warn(run, message)
            self.memory.record_failure(
                principal.owner_id, run, operation="external", index_ids=run["index_ids"]
            )
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
        initial: bool = False,
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
                if initial:
                    run["metrics"]["bootstrap_calls"] = run["metrics"].get("bootstrap_calls", 0) + 1
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
            accepted = sum(self._add_evidence(run, item, index_id, fresh=not initial)
                           for item in items[:count])
            if initial:
                run.setdefault("coverage", {})[index_id] = {
                    "status": "searched" if items else "empty", "count": len(items[:count]),
                }
            return f"Получено подтверждений: {accepted}."
        except Exception as exc:
            if initial:
                run.setdefault("coverage", {})[index_id] = {"status": "failed", "count": 0}
            outcome = f"Поиск в {index_id} недоступен ({type(exc).__name__})."
            self._warn(run, outcome)
            self.memory.record_failure(
                principal.owner_id, run, operation="search", index_ids=[index_id]
            )
            return outcome
        finally:
            self._checkpoint(principal, run, event, minimum_interval=0.15 if initial else 0)

    async def _read(
        self, principal: Principal, run: dict[str, Any], event: Event, args: dict[str, Any],
        deadline: float | None = None,
    ) -> str:
        if event.is_set() or self.source is None:
            return "Вызов отменён либо источник отключён."
        run["metrics"]["source_calls"] += 1
        try:
            timeout = self._source_timeout(run)
            if deadline is not None:
                timeout = min(timeout, max(0, deadline - self._now(run)))
            item = await asyncio.wait_for(
                self.source.read(**args, principal=principal, cancel=event),
                timeout,
            )
            run["metrics"]["document_reads"] = run["metrics"].get("document_reads", 0) + 1
            return (
                "Документ прочитан; подтверждение включено в контекст."
                if self._add_evidence(run, item, args["index_id"], document=True)
                else "Документ не включён: ограничение области доступа или бюджета."
            )
        except Exception as exc:
            outcome = f"Чтение документа недоступно ({type(exc).__name__})."
            self._warn(run, outcome)
            self.memory.record_failure(
                principal.owner_id, run, operation="read", index_ids=[args["index_id"]]
            )
            return outcome
        finally:
            self._checkpoint(principal, run, event)

    def _checkpoint(
        self, principal: Principal, run: dict[str, Any], event: Event,
        *, minimum_interval: float = 0,
    ) -> None:
        if event.is_set() and run["status"] in ACTIVE:
            run["status"] = "cancelling"
        now = time.monotonic()
        first_evidence = bool(run["evidence"]) and not self._checkpoint_evidence_counts.get(
            run["id"]
        )
        if not first_evidence and now - self._last_checkpoints.get(run["id"], 0) < minimum_interval:
            return
        self.store.save(principal.owner_id, run)
        self._last_checkpoints[run["id"]] = now
        self._checkpoint_evidence_counts[run["id"]] = len(run["evidence"])

    def _source_timeout(self, run: dict[str, Any]) -> float:
        return (
            min(self.settings.source_timeout_seconds, self.settings.quick_timeout_seconds / 3)
            if run["mode"] == "quick"
            else self.settings.source_timeout_seconds
        )

    def _add_evidence(
        self, run: dict[str, Any], item: Evidence, index_id: str, *, document: bool = False,
        fresh: bool = False,
    ) -> bool:
        item = Evidence.model_validate(item)
        if item.index_id != index_id:
            self._warn(run, "Источник вернул evidence за пределами запрошенного индекса.")
            return False
        payload = item.model_dump()
        profile = (QUICK_BUDGETS if run["mode"] == "quick" else BUDGETS)[run["effort"]]
        text_limit = (
            min(32_000, self.settings.investigate_context_chars // 3)
            if item.kind == "tool_metadata"
            else profile.document_chars if document
            else min(self.settings.evidence_chars, profile.evidence_chars)
        )
        payload["text"] = payload["text"][:text_limit]
        payload["title"] = payload["title"][:300]
        payload["id"] = payload["id"][:1000]
        payload["url"] = payload["url"][:2000] if payload["url"] else None
        payload["revision"] = payload["revision"][:200] if payload["revision"] else None
        if document:
            payload["read_document"] = True
            payload["text_truncated"] = len(item.text) > text_limit
        for i, existing in enumerate(run["evidence"]):
            if (existing["id"], existing["index_id"]) == (payload["id"], index_id):
                if existing.get("read_document") and not document:
                    return True
                run["evidence"][i] = payload
                return True
        if len(run["evidence"]) < self.settings.max_evidence:
            run["evidence"].append(payload)
            return True
        counts: dict[str, int] = {}
        for entry in run["evidence"]:
            counts[entry["index_id"]] = counts.get(entry["index_id"], 0) + 1
        # Do not let fast sources occupy every slot. Preserve at least one
        # fragment per index when possible, and prioritize an explicitly read doc.
        for i in range(len(run["evidence"]) - 1, -1, -1):
            entry = run["evidence"][i]
            if (index_id not in counts and counts[entry["index_id"]] > 1) or (
                document and not entry.get("read_document") and entry["index_id"] == index_id
            ) or (
                fresh and entry["index_id"] == index_id and not entry.get("read_document")
            ):
                run["evidence"][i] = payload
                return True
        return False

    @staticmethod
    def _ranked_evidence(run: dict[str, Any]) -> list[dict[str, Any]]:
        words = set(re.findall(r"[\w-]{3,}", run["question"].casefold()))

        def relevance(item: dict[str, Any]) -> int:
            title = set(re.findall(r"[\w-]{3,}", item["title"].casefold()))
            body = set(re.findall(r"[\w-]{3,}", item["text"].casefold()))
            return len(words & title) * 4 + len(words & body)

        ranked = sorted(run["evidence"],
                        key=lambda item: (item.get("kind") == "tool_metadata",
                                          bool(item.get("read_document")), relevance(item)),
                        reverse=True)
        first, remaining, indexes = [], [], set()
        for item in ranked:
            if item["index_id"] in indexes:
                remaining.append(item)
            else:
                first.append(item)
                indexes.add(item["index_id"])
        return first + remaining

    async def _decide(
        self,
        principal: Principal,
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
        allowed = {item["id"] for item in await self._indexes(principal)}
        memories = self.memory.retrieve(
            principal.owner_id, run["question"], allowed, set(run["index_ids"]),
            exclude_run_id=run["id"],
        )
        parent = self._parent_context(principal, run, allowed)
        if set(run.get("context_index_ids", [])) - allowed or any(
            item["index_id"] not in allowed for item in run["evidence"]
        ):
            raise ValueError("Source access changed during the investigation")
        coverage = run.get("coverage", {})
        context: dict[str, Any] = {
            "mode": run["mode"], "effort": run["effort"],
            "stage": run["phase"],
            "sandbox_profile_id": run.get("sandbox_profile_id", ""),
            "allowed_indexes": [],
            "selected_index_count": len(run["index_ids"]),
            "index_catalog_truncated": False,
            "coverage": {status: sum(item["status"] == status for item in coverage.values())
                         for status in ("searched", "empty", "failed", "pending")},
            "evidence": [],
            "evidence_total": len(run["evidence"]),
            "prior_tool_attempts": [],
            "history_question_not_evidence": None,
            "history_answer_not_evidence": None,
            "private_notes_not_evidence": [],
            "remaining_followup_calls": max(0, profile.source_calls - self._followup_calls(run)),
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": run["question"]},
        ]
        # Measure the actual serialized envelope, including a JSON context string
        # nested in messages. Source code contains many quotes and newlines.
        # Leave room for GigaCode/API adapter instructions and framing.
        adapter_reserve = 1500
        maximum = max(maximum, len(json.dumps(messages, ensure_ascii=False)) + 6500)
        selected_tools: list[dict[str, Any]] = []

        def fits(candidate: dict[str, Any], catalog: list[dict[str, Any]] | None = None) -> bool:
            envelope = {"messages": [*messages, {
                "role": "user", "content": json.dumps(candidate, ensure_ascii=False),
            }], "tools": selected_tools if catalog is None else catalog}
            return len(json.dumps(envelope, ensure_ascii=False)) + adapter_reserve <= maximum

        for tool in tools:
            proposed = [*selected_tools, tool]
            if (len(json.dumps(proposed, ensure_ascii=False)) <= maximum // 3
                    and fits(context, proposed)):
                selected_tools = proposed
        if len(selected_tools) < len(tools):
            self._warn(run, "Часть описаний инструментов не помещается в контекст модели.")
        tools = selected_tools
        # Keep source routing before evidence; the model must be able to target
        # sources with no initial hits, not only sources already present in context.
        for index in run["index_ids"]:
            candidate = {**context, "allowed_indexes": [*context["allowed_indexes"], index]}
            if (len(json.dumps(candidate["allowed_indexes"], ensure_ascii=False)) <= maximum // 5
                    and fits(candidate)):
                context = candidate
        context["index_catalog_truncated"] = len(context["allowed_indexes"]) < len(run["index_ids"])
        if parent:
            candidate = {**context, "history_question_not_evidence": parent["question"],
                         "history_answer_not_evidence": parent["answer"]}
            if fits(candidate):
                context = candidate
        included_memories = []
        for item in memories:
            note = {key: item.get(key) for key in
                    ("id", "title", "text", "kind", "status", "origin", "feedback", "outcome")}
            candidate = {**context, "private_notes_not_evidence": [
                *context["private_notes_not_evidence"], note,
            ]}
            if fits(candidate):
                context = candidate
                included_memories.append(item)
        # Final synthesis receives previous failures too. Preserve the latest
        # diagnostics first when a large tool history must be compacted.
        attempts = run.get("research_trace", []) or exchange or []
        for item in reversed(attempts[-30:]):
            candidate = {**context, "prior_tool_attempts": [item, *context["prior_tool_attempts"]]}
            if (len(json.dumps(candidate["prior_tool_attempts"], ensure_ascii=False))
                    <= maximum // 6
                    and fits(candidate)):
                context = candidate
        # First represent the most relevant documents across different indexes,
        # then expand their text. Arrival order must not decide what the model sees.
        ranked = self._ranked_evidence(run)
        originals: list[dict[str, Any]] = []
        for item in ranked:
            metadata = item.get("kind") == "tool_metadata"
            compact = {**item, "text": item["text"] if metadata else item["text"][:500]}
            compact["context_truncated"] = len(compact["text"]) < len(item["text"])
            candidate = {**context, "evidence": [*context["evidence"], compact]}
            if ((metadata or len(json.dumps(candidate["evidence"], ensure_ascii=False))
                 < maximum // 2)
                    and fits(candidate)):
                context = candidate
                originals.append(item)
        for position, item in enumerate(originals):
            small = context["evidence"][position]
            low, high = len(small["text"]), len(item["text"])
            # Binary-search the exact fit rather than discarding a long document.
            while low < high:
                length = (low + high + 1) // 2
                expanded = {**item, "text": item["text"][:length],
                            "context_truncated": length < len(item["text"])}
                evidence = list(context["evidence"])
                evidence[position] = expanded
                if fits({**context, "evidence": evidence}):
                    low = length
                else:
                    high = length - 1
            context["evidence"][position] = {**item, "text": item["text"][:low]}
            if low < len(item["text"]):
                context["evidence"][position]["context_truncated"] = True
        run["context_summary"] = {
            "evidence_available": len(run["evidence"]),
            "evidence_included": len(context["evidence"]),
            "documents_shortened": sum(
                bool(item.get("context_truncated")) for item in context["evidence"]
            ),
            "tools_available": len(tools),
        }
        messages.append({"role": "user", "content": json.dumps(context, ensure_ascii=False)})
        used = {item["id"]: item for item in run.get("memory_used", [])}
        for item in included_memories:
            used[item["id"]] = {
                key: item.get(key) for key in ("id", "title", "status", "origin", "feedback")
            }
            run["context_index_ids"] = sorted(
                set(run.get("context_index_ids", [])) | set(item["index_ids"])
            )
        run["memory_used"] = list(used.values())[:12]
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
        except ValueError:
            raise ModelDecisionError("Модель вернула некорректный формат решения.") from None
        if not isinstance(result, dict) or not isinstance(result.get("tool_calls", []), list):
            raise ModelDecisionError("Модель вернула некорректный формат решения.")
        self.memory.record_usage(
            principal.owner_id, [item["id"] for item in included_memories], run["id"]
        )
        return result

    async def _synthesize(self, principal: Principal, run: dict[str, Any], event: Event) -> None:
        run["phase"] = "synthesizing"
        self._checkpoint(principal, run, event)
        decision = await self._decide(principal, run, event, [])
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
