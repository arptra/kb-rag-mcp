"""Verify attribution at the adapter boundary, before tool outcomes reach memory."""

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from test_harness_context_regressions import OWNER, Source, record
from test_harness_remote_tools import connection, peer  # noqa: F401 — pytest fixture
from test_harness_sandbox import CONFIG, REPOSITORY

from agent_harness import sandbox
from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.integrations import IntegrationHub
from agent_harness.integrations_config import IntegrationSettings, IntegrationStore
from agent_harness.runtime import HarnessRuntime


async def test_native_rag_search_and_document_retain_their_origins(tmp_path: Path) -> None:
    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None), Source(["docs"]),
    )
    await runtime.start()
    try:
        run = record(runtime, ["docs"])
        run["mode"] = "quick"
        runtime._semaphores[run["id"]] = asyncio.Semaphore(1)
        await runtime._search(OWNER, run, Event(), "docs", "payments", initial=True)
        assert run["evidence"][0]["tool_origin"] == {"tool": "search_knowledge", "index_id": "docs"}
        await runtime._read(OWNER, run, Event(), {"index_id": "docs", "document_id": "docs-doc"})
        assert run["evidence"][0]["tool_origin"] == {"tool": "read_document", "index_id": "docs"}
        # Re-reading the same result as a search hit doesn't replace the retained document.
        await runtime._search(OWNER, run, Event(), "docs", "other query")
        assert run["evidence"][0]["tool_origin"]["tool"] == "read_document"
    finally:
        await runtime.stop()


async def test_mcp_reports_validated_leaf_failure_and_empty_results(
    tmp_path: Path, peer: Any,  # noqa: F811 — pytest resolves the imported fixture
) -> None:
    server, _ = peer
    store = IntegrationStore(tmp_path)
    store.save(IntegrationSettings(connections=[connection()]))

    async def allow(_: Principal) -> bool:
        return True

    runtime = HarnessRuntime(
        HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None),
        tools=IntegrationHub(store, allow),
    )
    await runtime.start()
    try:
        run = record(runtime, ["connection:source", "docs"])
        args = {"connection_id": "source", "tool": "search", "arguments": {"query": "PRIVATE"}}

        async def call() -> str:
            return await runtime._external_tool(
                OWNER, run, Event(), "mcp_call", args, asyncio.get_running_loop().time() + 5,
            )

        server.text = "incident evidence"
        assert await call() == "Получено подтверждений: 1."
        assert run["evidence"][0]["tool_origin"] == {
            "tool": "mcp_call", "remote_tool": "search", "index_id": "connection:source",
        }
        server.text = "[]"
        assert await call() == "Получено подтверждений: 0."
        assert not run["evidence"][0].get("tool_origin")
        server.text = "DO_NOT_PERSIST"
        server.mcp_error = True
        assert "удалённый инструмент вернул ошибку" in await call()
        lesson = runtime.memory.list(OWNER.owner_id, set(run["index_ids"]))[0]
        assert lesson["failures"] == [{
            "operation": "external", "index_ids": ["connection:source"],
            "tool": "mcp_call", "remote_tool": "search", "reason": "remote_error",
        }]
        assert not any(word in json.dumps(lesson) for word in ("PRIVATE", "DO_NOT_PERSIST"))
        args["tool"] = "PRIVATE_UNAUTHORIZED"
        assert "нет доступа" in await call()
        denied = runtime.memory.list(OWNER.owner_id, set(run["index_ids"]))[0]["failures"][-1]
        assert denied["reason"] == "permission" and "remote_tool" not in denied
    finally:
        await runtime.stop()


@pytest.mark.parametrize(("tool", "output", "expected"), [
    ("search_repository", sandbox._Result(0, '{"matches":[]}', ""), "empty"),
    ("run_in_sandbox", sandbox._Result(1, "Assertion failed", ""), "failed"),
    ("run_in_sandbox", sandbox._Result(0, "", ""), "empty"),
    ("run_in_sandbox", sandbox._Result(0, "1 test passed", ""), None),
])
async def test_sandbox_outcome_comes_from_exit_status_and_matches(
    tool: str, output: sandbox._Result, expected: str | None,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = sandbox.SandboxBackend()

    async def result(*args: Any, **kwargs: Any) -> sandbox._Result:
        return output

    monkeypatch.setattr(backend, "_container", result)
    result_items = await backend._tool(
        tool, {"command": ["test"], "query": "missing"}, REPOSITORY, CONFIG,
        sandbox._Checkout("volume", ("test",)), Event(), sandbox._Run("alice"),
        asyncio.get_running_loop().time() + 1,
    )
    assert result_items[0].text and result_items[0].tool_outcome == expected
    runtime = HarnessRuntime(HarnessSettings(enabled=True, state_dir=tmp_path, _env_file=None))
    await runtime.start()
    try:
        run = record(runtime, ["repo:service"])
        run["mode"] = "autonomous"
        assert runtime._add_evidence(run, result_items[0], "repo:service",
                                     origin_tool=tool, checked_with=tool)
        evidence = run["evidence"][0]
        assert ("checked_with" in evidence) == (expected is None)
        assert ("tool_origin" in evidence) == (expected is None)
    finally:
        await runtime.stop()
