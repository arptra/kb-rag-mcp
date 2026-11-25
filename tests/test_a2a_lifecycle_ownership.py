"""Shared controller ownership and large selected scopes stay independent of consumers."""

import asyncio
from threading import Event

from corporate_kb.a2a.research import ResearchBridge
from corporate_kb.a2a.service import A2AService
from corporate_kb.a2a.store import A2AStore
from corporate_kb.agents.activation import GigaCodeAgentController


class Controller(GigaCodeAgentController):
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
        try:
            while self.block and not kwargs["cancel"].is_set():
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            self.cleaned.set()
            raise
        if kwargs["cancel"].is_set():
            self.cleaned.set()
            raise asyncio.CancelledError
        return {"content": "Ответ по найденным документам [doc-0].", "tool_calls": []}


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
    service = A2AService(store, None, research=bridge)
    agent = store.issue("Active", ["default"])["agent"]
    signal = service._signals["active-task"] = Event()
    job = service._pool.submit(bridge.run, agent, {"query": "Question"}, "active-task",
                               signal, lambda *unused: None)
    try:
        assert controller.entered.wait(timeout=5)
    finally:
        service.stop()
    assert signal.is_set() and job.done()
    assert controller.cleaned.is_set()
    assert controller.enabled and controller.disabled == 0 and controller.stopped == 0


def test_a2a_shutdown_preserves_default_controller_ownership(settings_factory):
    settings = settings_factory()
    store = A2AStore(settings.a2a_state_dir)
    controller = Controller()
    bridge = ResearchBridge(settings, store, None, controller)
    A2AService(store, None, research=bridge).stop()
    assert controller.disabled == 1 and controller.stopped == 1
