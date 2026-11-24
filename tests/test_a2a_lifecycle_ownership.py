"""Shared controller ownership and large selected scopes stay independent of consumers."""

import asyncio
import json
from threading import Event

import pytest

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal, RunRequest
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import ACTIVE
from corporate_kb.a2a.research import ResearchBridge
from corporate_kb.a2a.service import A2AService
from corporate_kb.a2a.store import A2AStore


class Controller:
    def __init__(self, *, block=False):
        self.enabled = True
        self.block = block
        self.disabled = 0
        self.stopped = 0
        self.entered = Event()
        self.cleaned = Event()
        self.requests = []

    def status(self):
        return {"enabled": self.enabled, "available": self.enabled, "state": "ready", "reason": ""}

    def disable(self):
        self.disabled += 1
        self.enabled = False

    def stop(self):
        self.stopped += 1
        self.enabled = False

    async def decide(self, **kwargs):
        self.requests.append(kwargs)
        self.entered.set()
        while self.block and not kwargs["cancel"].is_set():
            await asyncio.sleep(0.005)
        if kwargs["cancel"].is_set():
            self.cleaned.set()
            raise asyncio.CancelledError
        return {"content": "Ответ по найденным документам [doc-0].", "tool_calls": []}


class Sources:
    def __init__(self, identifiers=None):
        self.identifiers = identifiers or ["default"]
        self.calls = []

    async def indexes(self, principal):
        return [{"id": item, "name": item} for item in self.identifiers]

    async def search(self, **kwargs):
        self.calls.append(kwargs["index_id"])
        return [
            Evidence(
                id="doc-0", title="Document", text="Current evidence.", index_id=kwargs["index_id"]
            )
        ]

    async def read(self, **kwargs):
        return Evidence(
            id=kwargs["document_id"], title="Document", text="Detail", index_id=kwargs["index_id"]
        )


def test_default_bridge_still_owns_controller(settings_factory):
    settings = settings_factory()
    controller = Controller()
    bridge = ResearchBridge(settings, A2AStore(settings.a2a_state_dir), None, controller)
    assert bridge.owns_controller
    bridge.stop()
    assert controller.stopped == 1 and not controller.enabled


def test_a2a_shutdown_cleans_its_run_without_disabling_shared_controller(settings_factory):
    settings = settings_factory()
    store = A2AStore(settings.a2a_state_dir)
    controller = Controller(block=True)
    bridge = ResearchBridge(settings, store, None, controller, owns_controller=False)
    bridge.source = Sources()
    service = A2AService(store, None, research=bridge)
    signal = service._signals["active-task"] = Event()
    pool_job = service._pool.submit(lambda: signal.wait(timeout=3))
    loop = bridge._ensure_started()
    runtime = bridge._runtime
    principal = Principal("test-owner")
    run = asyncio.run_coroutine_threadsafe(
        runtime.submit(principal, RunRequest(question="Explain")), loop
    ).result(timeout=3)
    try:
        assert controller.entered.wait(timeout=3)
    finally:
        service.stop()
    assert pool_job.result() and signal.is_set()
    assert controller.cleaned.is_set()
    assert runtime.store.get(principal.owner_id, run["id"])["status"] == "cancelled"
    assert controller.enabled and controller.disabled == 0 and controller.stopped == 0
    assert bridge._thread is not None and not bridge._thread.is_alive()


def test_a2a_shutdown_preserves_default_controller_ownership(settings_factory):
    settings = settings_factory()
    store = A2AStore(settings.a2a_state_dir)
    controller = Controller()
    bridge = ResearchBridge(settings, store, None, controller)
    A2AService(store, None, research=bridge).stop()
    assert controller.disabled == 1 and controller.stopped == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "length"), [(40, 10), (1000, 180)])
async def test_large_selection_searches_every_index_with_bounded_model_context(
    tmp_path, count, length
):
    selected = [f"index-{number:04d}-" + "x" * length for number in range(count)]
    request = RunRequest(question="Explain selected system", index_ids=selected, effort="low")
    settings = HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path)
    assert settings.max_indexes == 1000
    source, model = Sources(selected), Controller()
    runtime = HarnessRuntime(settings, source, model)
    await runtime.start()
    try:
        submitted = await runtime.submit(Principal("owner"), request)
        async with asyncio.timeout(3):
            while True:
                run = await runtime.get(Principal("owner"), submitted["id"])
                if run["status"] not in ACTIVE:
                    break
                await asyncio.sleep(0.01)
        assert run["index_ids"] == selected
        assert sorted(source.calls) == selected
        assert run["coverage"] == {index: {"status": "searched", "count": 1} for index in selected}
        assert run["metrics"]["bootstrap_calls"] == count
        assert len(model.requests) == 1
        assert run["status"] == "completed" and "[doc-0]" in run["answer"]
        invocation = model.requests[0]
        assert len(json.dumps(invocation["messages"], ensure_ascii=False)) <= (
            invocation["max_input_chars"] - 4000
        )
        context = json.loads(invocation["messages"][-1]["content"])
        catalog = set(context["allowed_indexes"])
        evidence = {(item["index_id"], item["id"]) for item in context["evidence"]}
        assert evidence
        assert evidence <= {(item["index_id"], item["id"]) for item in run["evidence"]}
        assert {index for index, _ in evidence} <= catalog <= set(selected)
        assert context["selected_index_count"] == count
        assert context["coverage"] == {"searched": count, "empty": 0, "failed": 0, "pending": 0}
        if count == 1000:
            assert context["index_catalog_truncated"] and len(catalog) < count
        else:
            assert not context["index_catalog_truncated"] and catalog == set(selected)
    finally:
        await runtime.stop()
