"""Exercise provider failures through the public run and cancellation endpoints."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from test_harness_goal_research import OWNER, Source

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Principal
from agent_harness.http import create_app
from agent_harness.model_connection import ModelConnection, ModelRouter
from agent_harness.runtime import HarnessRuntime


@asynccontextmanager
async def client_for(
    path: Path, handler: Callable[[httpx.Request], httpx.Response], **settings: Any,
) -> AsyncIterator[tuple[httpx.AsyncClient, Source]]:
    source = Source()
    runtime = HarnessRuntime(
        HarnessSettings(_env_file=None, enabled=True, state_dir=path, **settings), source,
    )

    async def authenticate(_: Request) -> Principal:
        return OWNER

    app = create_app(runtime, authenticate)
    assert isinstance(runtime.model, ModelRouter)
    runtime.model.store.save(ModelConnection(provider="openai", base_url="https://gateway.test"))
    runtime.model.transport = httpx.MockTransport(handler)
    await runtime.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://portal.test") as client:
            yield client, source
    finally:
        await runtime.stop()


async def finish(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            run = (await client.get(path)).json()
            if run["status"] not in {"queued", "running", "cancelling"}:
                return run
            await asyncio.sleep(.01)


@pytest.mark.parametrize(("code", "body", "recovered"), [
    (408, b"", True), (425, b"", True), (429, b"", True),
    (500, b"", True), (502, b"", True), (503, b"", True), (504, b"", True),
    (200, b'{"choices":[{"message":{"content":"lost', True),
    (200, b'{"choices":[]}', True), (200, b'{"choices":[{}]}', True),
    (200, b"<html>Login landing page</html>", False),
    (200, b'{"unrelated_api":"OK"}', False), (403, b"", False), (404, b"", False),
    (501, b"", False), (429, b'{"error":{"code":"blacklist"}}', False),
])
async def test_gateway_error_then_valid_response_over_runs_api(
    tmp_path: Path, code: int, body: bytes, recovered: bool,
) -> None:
    calls = 0

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(code, content=body)
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Подтверждены настройки [entry].",
        }, "finish_reason": "stop"}]})

    async with client_for(tmp_path, gateway) as (client, source):
        created = await client.post("/api/runs", json={"question": "Проверить настройки"})
        assert created.status_code == 202
        run = await finish(client, "/api/runs/" + created.json()["id"])
        assert (run["status"] == "completed") == recovered, run["warnings"]
        assert calls == run["metrics"]["model_calls"] == (2 if recovered else 1)
        assert source.queries == ["Проверить настройки"]
        if recovered:
            assert run["answer"] == "Подтверждены настройки [entry]."
            assert any(step["name"] == "model_retry" for step in run["research_trace"])
        if b"blacklist" in body:
            assert run["stop_reason"] == "model_policy_stop"


@pytest.mark.parametrize("cancel", [False, True])
async def test_retry_after_is_observable_and_cancellable(tmp_path: Path, cancel: bool) -> None:
    times: list[float] = []

    def gateway(_: httpx.Request) -> httpx.Response:
        times.append(time.monotonic())
        if len(times) == 1:
            return httpx.Response(503, headers={"Retry-After": "45" if cancel else "1"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Готово [entry]."}}]})

    async with client_for(tmp_path, gateway) as (client, _):
        response = await client.post("/api/runs", json={"question": "Проверить настройки"})
        submitted = response.json()
        path = "/api/runs/" + submitted["id"]
        async with asyncio.timeout(3):
            while True:
                run = (await client.get(path)).json()
                if (run.get("current_action") or {}).get("name") == "model_retry":
                    break
                await asyncio.sleep(.01)
        if cancel:
            assert (await client.post(path + "/cancel")).status_code == 200
        result = await finish(client, path)
        assert result["status"] == ("cancelled" if cancel else "completed")
        assert len(times) == (1 if cancel else 2)
        if not cancel:
            assert times[1] - times[0] >= .98


async def test_temporarily_busy_planner_keeps_its_tool_catalog(tmp_path: Path) -> None:
    import json

    calls = 0

    def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if calls == 1:
            assert payload["tools"]
            return httpx.Response(503)
        if calls == 2:
            assert any(item["function"]["name"] == "read_document" for item in payload["tools"])
            message = {"tool_calls": [{"function": {"name": "read_document",
                       "arguments": '{"index_id":"a","document_id":"entry"}'}}]}
        else:
            message = {"content": "Проверен документ [entry]."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with client_for(tmp_path, gateway) as (client, source):
        created = await client.post("/api/runs", json={
            "question": "Проверить настройки", "mode": "investigate", "index_ids": ["a"],
        })
        run = await finish(client, "/api/runs/" + created.json()["id"])
        assert run["status"] == "completed", run["warnings"]
        assert calls == run["metrics"]["model_calls"] == 3
        assert run["metrics"]["tool_calls"] == 1 and len(source.queries) == 1


async def test_rate_limit_beyond_configured_run_deadline_finishes_without_replay(
    tmp_path: Path,
) -> None:
    calls = 0

    def gateway(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "45"})

    async with client_for(tmp_path, gateway, quick_timeout_seconds=2) as (client, _):
        submitted = await client.post("/api/runs", json={"question": "Проверить настройки"})
        result = await finish(client, "/api/runs/" + submitted.json()["id"])
        assert calls == 1 and result["status"] == "partial"
        assert any("оставшегося времени" in warning for warning in result["warnings"])


async def test_autopilot_uses_library_tool_observations_for_its_next_decision(
    tmp_path: Path,
) -> None:
    import json

    requests: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        ctx = json.loads(payload["messages"][-1]["content"])
        requests.append(ctx)
        if len(requests) == 1:
            action = {"name": "search_knowledge", "arguments": '{"index_id":"a","query":"ledger"}'}
        elif len(requests) == 2:
            assert any(s["name"] == "agent_round" for s in ctx["prior_tool_attempts"])
            receipt, = ctx["last_tool_observations"]
            assert receipt["tool"] == "search_knowledge" and receipt["result"]
            index_id, doc_id = receipt["sources"][0]
            action = {"name": "read_document", "arguments": json.dumps({
                "index_id": index_id, "document_id": doc_id,
            })}
        else:
            assert len(requests) == 3
            assert any(item.get("read_document") for item in ctx["evidence"])
            return httpx.Response(200, json={"choices": [{"message": {
                "content": "Проверено чтением первоисточника [entry].",
            }}]})
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"type": "function", "function": action},
        ]}, "finish_reason": "tool_calls"}]})

    async with client_for(tmp_path, gateway) as (client, source):
        response = await client.post("/api/runs", json={
            "question": "Где проходит обработка?", "mode": "autonomous", "index_ids": ["a"],
        })
        run = await finish(client, "/api/runs/" + response.json()["id"])
        assert run["status"] == "completed", run["warnings"]
        assert source.queries == ["Где проходит обработка?", "ledger"]
        assert run["metrics"]["agent_rounds"] == 2 and run["metrics"]["tool_calls"] == 2
        assert len(requests) == run["metrics"]["model_calls"] == 3
