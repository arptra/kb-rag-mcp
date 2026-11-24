"""Ancillary failures and cancellation-resistant providers cannot strand a worker lane."""

import asyncio
import copy
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import RunStore, timestamp

OWNER = Principal("alice")


class Source:
    async def indexes(self, principal: Principal) -> list[dict[str, str]]:
        return [{"id": "a", "name": "A"}]

    async def search(self, **kwargs: Any) -> list[Evidence]:
        return [Evidence(id="doc", title="A", text="Current source", index_id="a")]

    async def read(self, **kwargs: Any) -> Evidence:
        return (await self.search(**kwargs))[0]


class Model:
    def status(self) -> dict[str, Any]:
        return {"available": True}

    async def decide(self, **kwargs: Any) -> dict[str, Any]:
        return {"content": "Current explanation [doc].", "tool_calls": []}


def runtime(tmp_path: Path, **settings: Any) -> HarnessRuntime:
    return HarnessRuntime(
        HarnessSettings(
            enabled=True,
            state_dir=tmp_path,
            _env_file=None,
            quick_workers=1,
            cleanup_timeout_seconds=0.02,
            **settings,
        ),
        Source(),
        Model(),
    )


async def finished(app: HarnessRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            run = app.store.get(OWNER.owner_id, run_id)
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_part", ["memory", "retention", "checkpoints"])
async def test_postprocessing_failure_does_not_kill_worker_or_strand_next_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_part: str,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:

        def broken(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("maintenance-failure")

        async def broken_async(*args: Any, **kwargs: Any) -> None:
            broken()

        target = (
            app.memory
            if failing_part == "memory"
            else app.store
            if failing_part == "retention"
            else app._graph
        )
        method = "record_outcome" if failing_part == "memory" else "prune"
        original = getattr(target, method)
        monkeypatch.setattr(
            target, method, broken_async if failing_part == "checkpoints" else broken
        )
        first = await app.submit(OWNER, RunRequest(question="First", index_ids=["a"]))
        result = await finished(app, first["id"])
        assert result["status"] == "completed" and result["answer"]
        if failing_part == "memory":
            assert any("память временно недоступна" in warning for warning in result["warnings"])
        await asyncio.wait_for(app._queues["quick"].join(), 1)
        monkeypatch.setattr(target, method, original)
        second = await app.submit(OWNER, RunRequest(question="Next chat", index_ids=["a"]))
        result = await finished(app, second["id"])
        assert result["status"] == "completed"
        assert result["memory_contribution"]["updated"] is True
        assert not app._workers[0].done()
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_worker_boundary_recovers_transient_final_save_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        original = app.store.save
        failed = False

        def fail_once(owner: str, run: dict[str, Any]) -> None:
            nonlocal failed
            if run.get("completed_at") and not failed:
                failed = True
                raise RuntimeError("transient-write")
            original(owner, run)

        monkeypatch.setattr(app.store, "save", fail_once)
        first = await app.submit(OWNER, RunRequest(question="First", index_ids=["a"]))
        assert (await finished(app, first["id"]))["status"] == "completed"
        assert failed
        second = await app.submit(OWNER, RunRequest(question="Second", index_ids=["a"]))
        assert (await finished(app, second["id"]))["status"] == "completed"
        assert not app._workers[0].done()
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_unexpected_execute_exception_has_terminal_state_and_worker_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        original = app._execute

        async def broken(principal: Principal, run: dict[str, Any]) -> None:
            raise RuntimeError("before-execution")

        monkeypatch.setattr(app, "_execute", broken)
        first = await app.submit(OWNER, RunRequest(question="First", index_ids=["a"]))
        result = await finished(app, first["id"])
        assert result["status"] == "failed" and result["phase"] == "finished"
        monkeypatch.setattr(app, "_execute", original)
        second = await app.submit(OWNER, RunRequest(question="Second", index_ids=["a"]))
        assert (await finished(app, second["id"]))["status"] == "completed"
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_cleanup_deadline_is_bounded_when_task_suppresses_cancellation(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    release = asyncio.Event()

    async def stubborn() -> None:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    operation = asyncio.create_task(stubborn())
    run: dict[str, Any] = {"warnings": []}
    try:
        await asyncio.wait_for(app._cleanup(operation, run), 0.3)
        assert operation in app._detached_tasks and not operation.done()
        assert run["warnings"]
    finally:
        release.set()
        await asyncio.wait_for(operation, 1)
    await asyncio.sleep(0)
    assert not app._detached_tasks


@pytest.mark.asyncio
async def test_timed_out_resistant_model_releases_lane_and_cannot_reopen_terminal_run(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()

    class Resistant(Model):
        calls = 0

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            return await super().decide(**kwargs)

    app = runtime(tmp_path, quick_timeout_seconds=0.15)
    app.model = Resistant()
    await app.start()
    try:
        first = await app.submit(OWNER, RunRequest(question="First", index_ids=["a"]))
        assert (await finished(app, first["id"]))["status"] == "timed_out"
        second = await app.submit(OWNER, RunRequest(question="Second", index_ids=["a"]))
        assert (await finished(app, second["id"]))["status"] == "completed"
        release.set()
        await asyncio.sleep(0.05)
        assert app.store.get("alice", first["id"])["status"] == "timed_out"
    finally:
        release.set()
        await app.stop()


def test_late_checkpoints_cannot_reopen_terminal_or_overwrite_resumed_generation(
    tmp_path: Path,
) -> None:
    store = RunStore(HarnessSettings(state_dir=tmp_path, _env_file=None))
    finished_run = {
        "id": "run",
        "status": "timed_out",
        "created_at": timestamp(),
        "resume_count": 0,
    }
    store.save("alice", finished_run)
    store.save("alice", {**finished_run, "status": "running"})
    assert store.get("alice", "run")["status"] == "timed_out"
    store.save("alice", {**finished_run, "status": "queued", "resume_count": 1})
    store.save("alice", {**finished_run, "status": "completed"})
    current = store.get("alice", "run")
    assert current["status"] == "queued" and current["resume_count"] == 1


@pytest.mark.asyncio
async def test_late_terminal_callback_cannot_overwrite_feedback_or_recreate_deleted_run(
    tmp_path: Path,
) -> None:
    app = runtime(tmp_path)
    await app.start()
    try:
        submitted = await app.submit(OWNER, RunRequest(question="Question", index_ids=["a"]))
        terminal = await finished(app, submitted["id"])
        late = copy.deepcopy(terminal)
        terminal["feedback"] = {"rating": "negative", "comment": "Correction kept"}
        app.store.save(OWNER.owner_id, terminal)
        late["evidence"] = []
        cancelled = Event()
        cancelled.set()
        app._checkpoint(OWNER, late, cancelled)
        saved = app.store.get(OWNER.owner_id, terminal["id"])
        assert saved["feedback"] == terminal["feedback"]
        assert saved["evidence"] == terminal["evidence"]
        # A newer resume must not accept a callback from the previous execution.
        app.store.save(OWNER.owner_id, {**terminal, "status": "running", "resume_count": 1})
        app._checkpoint(OWNER, late, cancelled)
        assert app.store.get(OWNER.owner_id, terminal["id"])["status"] == "running"
        assert app.store.get(OWNER.owner_id, terminal["id"])["resume_count"] == 1
        app.store.save(OWNER.owner_id, {**terminal, "resume_count": 1})
        app.store.delete(OWNER.owner_id, terminal["id"])
        app._checkpoint(OWNER, late, cancelled)
        with pytest.raises(KeyError):
            app.store.get(OWNER.owner_id, terminal["id"])
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_shutdown_cancellation_during_cleanup_cannot_return_worker_to_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = runtime(tmp_path)
    entered = asyncio.Event()
    child: asyncio.Task[None] | None = None

    async def operation() -> None:
        entered.set()
        await asyncio.Event().wait()

    async def execute(principal: Principal, run: dict[str, Any]) -> None:
        nonlocal child
        child = asyncio.create_task(operation())
        await app._cleanup(child, run)

    monkeypatch.setattr(app, "_execute", execute)
    await app.start()
    try:
        await app.submit(OWNER, RunRequest(question="Shutting down", index_ids=["a"]))
        await asyncio.wait_for(entered.wait(), 1)
        workers = list(app._workers)
        stopping = asyncio.create_task(app.stop())
        done, _ = await asyncio.wait({stopping}, timeout=1)
        if not done:
            # Release a regressed test without leaving a hung asyncio test loop.
            for worker in workers:
                worker.cancel()
            await stopping
        assert done, "stop() must not swallow cancellation inside provider cleanup"
        assert child is not None and child.cancelled()
        assert all(worker.done() for worker in workers)
    finally:
        await app.stop()
