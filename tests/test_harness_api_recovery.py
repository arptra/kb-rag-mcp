"""Exercise provider failures through the public run and cancellation endpoints."""

import asyncio
import json
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
from agent_harness.material_archive import deep_agents_installed
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


async def test_transport_recovery_leaves_formatting_repair_for_an_interrupted_answer(
    tmp_path: Path,
) -> None:
    responses = [
        httpx.Response(502),
        httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {
            "content": "The value starts with",
        }}]}),
        httpx.Response(502),
        httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": "Requests arrive through payment-service [entry].",
        }}]}),
    ]

    def gateway(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with client_for(tmp_path, gateway) as (client, source):
        submitted = await client.post("/api/runs", json={"question": "How do requests arrive?"})
        result = await finish(client, "/api/runs/" + submitted.json()["id"])
        assert result["status"] == "completed" and not responses, result["warnings"]
        assert result["answer"] == "Requests arrive through payment-service [entry]."
        assert result["metrics"]["model_calls"] == 4
        assert result["metrics"]["transport_retries"] == 2
        assert result["metrics"]["answer_retries"] == 1
        assert len(source.queries) == 1


async def test_retry_stops_on_explicit_provider_restriction(tmp_path: Path) -> None:
    responses = [
        httpx.Response(502),
        httpx.Response(200, json={"choices": [{"finish_reason": "content_filter",
                                               "message": {}}]}),
    ]

    def gateway(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with client_for(tmp_path, gateway) as (client, _):
        submitted = await client.post("/api/runs", json={"question": "How do requests arrive?"})
        result = await finish(client, "/api/runs/" + submitted.json()["id"])
        assert result["stop_reason"] == "model_policy_stop" and not responses
        assert result["metrics"]["model_calls"] == 2
        assert result["metrics"]["transport_retries"] == 1


@pytest.mark.parametrize(("allowed_calls", "allowed_attempts", "expected"),
                         [(1, 3, 1), (8, 2, 2)])
async def test_transport_retries_respect_the_run_and_decision_limits(
    tmp_path: Path, allowed_calls: int, allowed_attempts: int, expected: int,
) -> None:
    from agent_harness.control.settings import ControlStore

    control = ControlStore(HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path))
    policy = control.load()
    policy.runtime.max_decision_errors = allowed_attempts
    policy.runtime.max_answer_retries = 0
    policy.profiles["quick"]["medium"].model_calls = allowed_calls
    control.save(policy, expected_revision=policy.revision)
    calls = 0

    def gateway(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502)

    async with client_for(tmp_path, gateway) as (client, source):
        response = await client.post("/api/runs", json={
            "question": "How do requests arrive?", "mode": "quick", "effort": "medium",
        })
        run = await finish(client, "/api/runs/" + response.json()["id"])
        assert run["status"] == "partial" and run["stop_reason"] == "model_transport"
        assert calls == run["metrics"]["model_calls"] == expected
        assert len(source.queries) == 1


async def test_independent_gateway_outages_do_not_replay_an_agent_tool(tmp_path: Path) -> None:
    calls = 0

    def gateway(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls in {1, 3, 4}:
            return httpx.Response(502)
        payload = json.loads(request.content)
        if calls in {2, 5}:
            assert payload.get("tools")
            if calls == 2:
                action = {"name": "search_knowledge", "arguments":
                          '{"index_id":"a","query":"ledger"}'}
            else:
                decision_context = json.loads(payload["messages"][-1]["content"])
                receipt, = decision_context["last_tool_observations"]
                assert receipt["tool"] == "search_knowledge"
                index, doc = receipt["sources"][0]
                action = {"name": "read_document", "arguments": json.dumps(
                    {"index_id": index, "document_id": doc})}
            message = {"tool_calls": [{"type": "function", "function": action}]}
        else:
            assert calls == 6
            message = {"content": "Requests arrive through payment-service [entry]."}
        return httpx.Response(200, json={"choices": [{"message": message}]})

    async with client_for(tmp_path, gateway) as (client, source):
        response = await client.post("/api/runs", json={
            "question": "How do requests arrive?", "mode": "autonomous",
            "effort": "high", "index_ids": ["a"],
        })
        run = await finish(client, "/api/runs/" + response.json()["id"])
        assert run["status"] == "completed" and "[entry]" in run["answer"], run["warnings"]
        assert calls == run["metrics"]["model_calls"] == 6
        assert run["metrics"]["transport_retries"] == 3
        assert run["metrics"]["tool_calls"] == 2
        assert source.queries == ["How do requests arrive?", "ledger"]


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
        framework = "Deep Agents" if deep_agents_installed() else "LangChain"
        rounds = [entry for entry in run["research_trace"] if entry["name"] == "agent_round"]
        assert len(rounds) == 2 and all(entry["result"].startswith(framework) for entry in rounds)
        assert len(requests) == run["metrics"]["model_calls"] == 3


@pytest.mark.parametrize("cancel", [False, True])
async def test_autopilot_releases_worker_and_continues_after_gateway_pause(
    tmp_path: Path, cancel: bool,
) -> None:
    import json

    primary_requests = []

    def gateway(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "orders" in payload["messages"][-1]["content"]:
            primary_requests.append(time.monotonic())
            if len(primary_requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Проверен документ [entry].",
        }}]})

    async with client_for(tmp_path, gateway, investigate_timeout_seconds=2,
                          investigate_workers=1) as (client, _):
        created = await client.post("/api/runs", json={"question": "orders", "mode": "autonomous",
                                                       "index_ids": ["a"]})
        path = "/api/runs/" + created.json()["id"]
        async with asyncio.timeout(4):
            while (parked := (await client.get(path)).json())["phase"] != "waiting_for_gateway":
                assert parked["status"] in {"queued", "running"}, parked
                await asyncio.sleep(.02)
        assert parked["current_action"]["retry_at"] > time.time()
        second = await client.post("/api/runs", json={"question": "other", "mode": "investigate",
                                                      "index_ids": ["a"]})
        other = await finish(client, "/api/runs/" + second.json()["id"])
        assert other["answer"] and len(primary_requests) == 1
        if cancel:
            assert (await client.post(path + "/cancel")).status_code == 200
            await asyncio.sleep(max(0, parked["current_action"]["retry_at"] - time.time()) + .05)
        async with asyncio.timeout(5):
            while (result := (await client.get(path)).json())["status"] in {"queued", "running"}:
                await asyncio.sleep(.02)
        if cancel:
            assert result["status"] == "cancelled" and len(primary_requests) == 1
        else:
            assert primary_requests[1] - primary_requests[0] >= 2.95
            assert result["resume_count"] == 1 and "[entry]" in result["answer"], result


async def test_admitted_gateway_retry_survives_restart_with_one_retry(tmp_path: Path) -> None:
    from agent_harness.control.settings import ControlStore

    control = ControlStore(HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path,
                                           investigate_timeout_seconds=2))
    settings = control.load()
    settings.runtime.max_resume_attempts = 1
    control.save(settings, expected_revision=0)
    attempts = []

    def gateway(_: httpx.Request) -> httpx.Response:
        attempts.append(time.monotonic())
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "Проверен документ [entry].",
        }}]})

    async with client_for(tmp_path, gateway) as (client, _):
        submission = await client.post("/api/runs", json={
            "question": "orders", "mode": "autonomous", "index_ids": ["a"],
        })
        path = "/api/runs/" + submission.json()["id"]
        async with asyncio.timeout(3):
            while (await client.get(path)).json()["phase"] != "waiting_for_gateway":
                await asyncio.sleep(.01)
    async with client_for(tmp_path, gateway) as (client, _):
        # First authenticated poll reconnects the admitted dispatch. It must still
        # honor Retry-After and get its result with a single charged retry.
        async with asyncio.timeout(5):
            while (result := (await client.get(path)).json())["status"] in {"queued", "running"}:
                await asyncio.sleep(.02)
        assert "[entry]" in result["answer"], result
        assert attempts[1] - attempts[0] >= 2.95
