"""Operator revisions, profile validation, telemetry privacy and endpoint authorization."""

import asyncio
import json
import time
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness.config import HarnessSettings
from agent_harness.contracts import Evidence, Principal
from agent_harness.control.metrics import collect_metrics
from agent_harness.control.settings import ControlSettings, ControlStore, RevisionConflict
from agent_harness.http import create_app
from agent_harness.runtime import HarnessRuntime
from agent_harness.store import RunStore


def settings(tmp_path: Path, **values: Any) -> HarnessSettings:
    return HarnessSettings(_env_file=None, enabled=True, state_dir=tmp_path, **values)


async def authenticate(request: Request) -> Principal | None:
    name = request.headers.get("x-owner")
    return Principal(name) if name else None


async def manager(request: Request) -> bool:
    return request.headers.get("x-owner") == "operator"


def test_default_load_is_lazy_and_revision_is_atomic(tmp_path: Path) -> None:
    store = ControlStore(settings(tmp_path))
    first = store.load()
    assert first.revision == 0
    assert not store.path.exists()
    first.profiles["autonomous"]["medium"].model_calls = 12
    saved = store.save(first, expected_revision=0)
    assert saved.revision == 1
    assert (
        ControlStore(settings(tmp_path)).load().profiles["autonomous"]["medium"].model_calls == 12
    )
    with pytest.raises(RevisionConflict):
        store.save(first, expected_revision=0)
    assert store.load().revision == 1
    assert store.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "change",
    [
        "negative_calls",
        "missing_mode",
        "initial_over_cap",
        "window_reserve",
        "window_too_small_after_reserve",
        "keep_trigger",
        "quick_tools",
        "unknown_field",
        "negative_memory",
        "bool_as_number",
    ],
)
def test_operator_policy_rejects_invalid_limits(tmp_path: Path, change: str) -> None:
    payload = ControlStore(settings(tmp_path)).load().model_dump()
    if change == "negative_calls":
        payload["profiles"]["autonomous"]["high"]["model_calls"] = -1
    elif change == "missing_mode":
        del payload["profiles"]["quick"]
    elif change == "initial_over_cap":
        payload["autonomy"]["caps"]["tool_calls"] = 1
    elif change == "window_reserve":
        payload["context"]["window_tokens"] = payload["context"]["response_reserve_tokens"]
    elif change == "window_too_small_after_reserve":
        payload["context"]["window_tokens"] = 2048
        payload["context"]["response_reserve_tokens"] = 2047
    elif change == "keep_trigger":
        payload["context"]["keep_fraction"] = payload["context"]["trigger_fraction"]
    elif change == "quick_tools":
        payload["profiles"]["quick"]["low"]["tool_calls"] = 1
    elif change == "unknown_field":
        payload["allow_all_sources"] = True
    elif change == "negative_memory":
        payload["runtime"]["memory_max_records"] = 0
    else:
        payload["profiles"]["autonomous"]["high"]["model_calls"] = True
    with pytest.raises(ValidationError):
        ControlSettings.model_validate(payload)


def test_defaults_respect_environment_settings(tmp_path: Path) -> None:
    selected = settings(
        tmp_path,
        quick_workers=7,
        source_timeout_seconds=9.0,
        investigate_timeout_seconds=3000.0,
        investigate_context_chars=240_000,
    )
    policy = ControlStore(selected).load()
    assert policy.runtime.quick_workers == 7
    assert policy.runtime.source_timeout_seconds == 9
    assert policy.profiles["investigate"]["high"].timeout_seconds == 6000
    assert policy.profiles["investigate"]["high"].context_tokens == 80_000
    assert policy.autonomy.caps.context_tokens >= 80_000


def test_control_api_authorization_revision_validation_and_csrf(tmp_path: Path) -> None:
    runtime = HarnessRuntime(settings(tmp_path))
    app = create_app(runtime, authenticate, authorize_model_manager=manager)
    with TestClient(app) as client:
        assert client.get("/api/control/settings").status_code == 401
        assert client.get("/api/control/metrics", headers={"x-owner": "alice"}).status_code == 403
        headers = {"x-owner": "operator"}
        initial = client.get("/api/control/settings", headers=headers).json()
        assert initial["can_manage"] is True and initial["revision"] == 0
        assert "profiles" in initial["schema"]["properties"]
        candidate = deepcopy(initial["settings"])
        candidate["runtime"]["quick_workers"] = 3
        body = {"revision": initial["revision"], "settings": candidate}
        assert (
            client.put(
                "/api/control/settings",
                json=body,
                headers={**headers, "Origin": "https://untrusted.test"},
            ).status_code
            == 403
        )
        response = client.put("/api/control/settings", json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["revision"] == 1
        assert "quick_workers" in response.json()["activation"]["restart_required"]
        assert runtime.settings.quick_workers == 2
        assert client.put("/api/control/settings", json=body, headers=headers).status_code == 409
        body["revision"] = 1
        body["settings"]["runtime"]["queue_max"] = -2
        assert client.put("/api/control/settings", json=body, headers=headers).status_code == 400
        assert client.get("/api/control/settings", headers=headers).json()["revision"] == 1
        assert client.get("/api/control/metrics?days=0", headers=headers).status_code == 400
        assert client.get("/api/control/metrics?owner=alice", headers=headers).status_code == 400
        metrics = client.get("/api/control/metrics?days=7", headers=headers).json()
        assert metrics["summary"]["runs"] == 0 and len(metrics["series"]) == 7
        assert metrics["summary"]["input_tokens_reported"] is None


def test_metrics_aggregate_real_saved_runs_without_private_contents(tmp_path: Path) -> None:
    store = RunStore(settings(tmp_path))
    now = datetime(2026, 11, 24, 12, tzinfo=UTC)
    common: dict[str, Any] = {
        "question": "PRIVATE_QUESTION",
        "answer": "PRIVATE_ANSWER",
        "index_ids": ["PRIVATE_INDEX"],
        "evidence": [{"text": "PRIVATE_SOURCE"}],
        "effort": "medium",
        "warnings": ["PRIVATE_ERROR"],
    }
    store.save(
        "PRIVATE_OWNER",
        {
            **common,
            "id": "one",
            "mode": "autonomous",
            "status": "completed",
            "created_at": "2026-11-23T12:00:00+00:00",
            "budget": {"extensions": 2, "events": [{"reason": "PRIVATE_REASON", "accepted": True}]},
            "metrics": {
                "duration_ms": 2000,
                "model_calls": 4,
                "source_calls": 5,
                "input_tokens_estimated": 500,
                "input_tokens_reported": 480,
                "output_tokens_reported": 50,
                "compactions": 1,
            },
        },
    )
    store.save(
        "OTHER_PRIVATE_OWNER",
        {
            **common,
            "id": "two",
            "mode": "investigate",
            "status": "timed_out",
            "created_at": "2026-11-24T11:00:00+00:00",
            "metrics": {"duration_ms": 4000, "model_calls": 2},
        },
    )
    store.save(
        "old",
        {
            **common,
            "id": "old",
            "mode": "quick",
            "status": "completed",
            "created_at": "2026-11-01T11:00:00+00:00",
            "metrics": {"duration_ms": 99999},
        },
    )
    result = collect_metrics(store, days=2, now=now)
    summary = result["summary"]
    assert summary["runs"] == 2 and summary["completed"] == 1 and summary["failed"] == 1
    assert summary["duration_ms"] == 6000 and summary["duration_mean_ms"] == 3000
    assert summary["duration_p95_ms"] == 4000
    assert summary["model_calls"] == 6 and summary["extensions"] == 2
    assert summary["input_tokens_estimated"] == 500 and summary["input_tokens_reported"] == 480
    assert summary["reported_usage_runs"] == 1 and summary["compactions"] == 1
    assert [row["runs"] for row in result["series"]] == [1, 1]
    assert result["recent_runs"][0]["stop_reason"] == "time_limit"
    assert "PRIVATE" not in str(result)


def test_control_feature_flag_removes_routes_without_disabling_runtime(tmp_path: Path) -> None:
    config = settings(tmp_path).model_copy(update={"control_enabled": False})
    app = create_app(HarnessRuntime(config), authenticate, authorize_model_manager=manager)
    with TestClient(app) as client:
        assert client.get("/control/").status_code == 404
        assert (
            client.get("/api/control/settings", headers={"x-owner": "operator"}).status_code == 404
        )
        assert client.get("/api/status", headers={"x-owner": "operator"}).status_code == 200


def test_metrics_project_only_telemetry_and_keep_all_active_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_harness.control import metrics as metrics_module

    store = RunStore(settings(tmp_path))
    common = {"effort": "medium", "mode": "autonomous", "metrics": {"model_calls": 2}}
    for number in range(55):
        store.save(
            "PRIVATE_OWNER",
            {
                **common,
                "id": f"recent-{number:02}",
                "status": "completed",
                "created_at": "2026-11-24T09:00:00+00:00",
            },
        )
    store.save(
        "PRIVATE_OWNER",
        {
            **common,
            "id": "old-active",
            "status": "running",
            "created_at": "2026-11-01T09:00:00+00:00",
            "answer": "PRIVATE_ANSWER" * 100_000,
            "evidence": [{"text": "PRIVATE_SOURCE" * 100_000}],
            "metrics": {"model_calls": 3, "PRIVATE_EXTRA": "PRIVATE_PAYLOAD"},
        },
    )
    store.save(
        "PRIVATE_OWNER",
        {
            **common,
            "id": "inside-active",
            "status": "queued",
            "created_at": "2026-11-23T09:00:00+00:00",
        },
    )
    decoded = []

    def small_public_payload(raw: str) -> Any:
        assert len(raw) < 5000
        assert "PRIVATE" not in raw
        decoded.append(raw)
        return json.loads(raw)

    monkeypatch.setattr(metrics_module, "json", SimpleNamespace(loads=small_public_payload))
    result = metrics_module.collect_metrics(
        store, days=2, now=datetime(2026, 11, 24, 12, tzinfo=UTC)
    )
    assert decoded
    assert result["summary"]["runs"] == 56
    assert result["summary"]["active_now"] == 2
    assert result["summary"]["input_tokens_reported"] is None
    assert len(result["recent_runs"]) == 50
    assert {run["id"] for run in result["active_runs"]} == {"old-active", "inside-active"}
    assert all(run["id"] not in {"old-active", "inside-active"} for run in result["recent_runs"])
    assert "PRIVATE" not in str(result)


def test_admin_can_stop_active_run_without_receiving_its_private_contents(tmp_path: Path) -> None:
    entered = Event()

    class Source:
        async def indexes(self, principal: Principal) -> list[dict[str, str]]:
            return [{"id": "private", "name": "Private source"}]

        async def search(self, **kwargs: Any) -> list[Evidence]:
            return [
                Evidence(
                    id="secret", title="Private title", text="PRIVATE_SOURCE", index_id="private"
                )
            ]

        async def read(self, **kwargs: Any) -> Evidence:
            return (await self.search(**kwargs))[0]

    class Model:
        def status(self) -> dict[str, Any]:
            return {"available": True}

        async def decide(self, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            while not kwargs["cancel"].is_set():
                await asyncio.sleep(0.01)
            return {"content": "PRIVATE_ANSWER", "tool_calls": []}

    runtime = HarnessRuntime(settings(tmp_path), Source(), Model())
    app = create_app(runtime, authenticate, authorize_model_manager=manager)
    with TestClient(app) as client:
        operator = {"x-owner": "operator"}
        owner = {"x-owner": "PRIVATE_OWNER"}
        submitted = client.post(
            "/api/runs",
            headers=owner,
            json={
                "question": "PRIVATE_QUESTION",
                "index_ids": ["private"],
            },
        )
        assert submitted.status_code == 202
        run_id = submitted.json()["id"]
        assert entered.wait(timeout=3)
        url = f"/api/control/runs/{run_id}/cancel"
        assert client.post(url, json={}).status_code == 401
        assert client.post(url, json={}, headers=owner).status_code == 403
        assert (
            client.post(
                url,
                json={},
                headers={
                    **operator,
                    "Origin": "https://untrusted.test",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(url, json={"owner": "PRIVATE_OWNER"}, headers=operator).status_code == 400
        )
        response = client.post(url, json={}, headers=operator)
        assert response.status_code == 200
        assert set(response.json()) == {"id", "status"}
        assert response.json()["status"] in {"cancelling", "cancelled"}
        assert "PRIVATE" not in response.text
        until = time.monotonic() + 3
        while True:
            result = client.get(f"/api/runs/{run_id}", headers=owner).json()
            if result["status"] == "cancelled":
                break
            assert time.monotonic() < until
            time.sleep(0.01)
        repeated = client.post(url, json={}, headers=operator)
        assert repeated.json() == {"id": run_id, "status": "cancelled"}
        assert (
            client.post("/api/control/runs/missing/cancel", json={}, headers=operator).status_code
            == 404
        )
