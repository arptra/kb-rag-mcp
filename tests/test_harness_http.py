"""HTTP boundary checks independent of model calls and corporate integrations."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx
import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from agent_harness import __main__ as standalone
from agent_harness import http
from agent_harness.contracts import Principal, RunRequest
from agent_harness.runtime import HarnessRuntime, HarnessUnavailableError, QueueFullError


class FakeRuntime:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.calls: list[tuple[str, str]] = []
        self.records: dict[str, dict[str, Any]] = {}
        self.failure: Exception | None = None

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def status(self, principal: Principal) -> dict[str, Any]:
        self.calls.append(("status", principal.owner_id))
        return {"started": bool(self.started), "model": {"available": False}}

    async def list_runs(self, principal: Principal) -> list[dict[str, Any]]:
        self.calls.append(("list", principal.owner_id))
        return [run for run in self.records.values() if run["owner"] == principal.owner_id]

    async def submit(self, principal: Principal, request: RunRequest) -> dict[str, Any]:
        self.calls.append(("submit", principal.owner_id))
        if self.failure:
            raise self.failure
        run = {"id": "run-1", "owner": principal.owner_id, **request.model_dump()}
        self.records["run-1"] = run
        return run

    async def get(self, principal: Principal, run_id: str) -> dict[str, Any]:
        self.calls.append(("get", principal.owner_id))
        run = self.records[run_id]
        if run["owner"] != principal.owner_id:
            raise KeyError(run_id)
        return run

    async def cancel(self, principal: Principal, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", principal.owner_id))
        run = await self.get(principal, run_id)
        run["status"] = "cancelled"
        return run


class FakeModelControl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = "disabled"

    def _snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.state != "disabled",
            "available": False,
            "state": self.state,
            "authentication_url": (
                "https://login.example.test/?code=private-browser-code"
                if self.state == "authentication_required" else None
            ),
            "events": [{"message": "private-manager-event"}],
        }

    def status(self) -> dict[str, Any]:
        self.calls.append("status")
        return self._snapshot()

    def enable(self) -> dict[str, Any]:
        self.calls.append("enable")
        self.state = "authentication_required"
        return self._snapshot()

    def disable(self) -> dict[str, Any]:
        self.calls.append("disable")
        self.state = "disabled"
        return self._snapshot()


async def authenticate(request: Request) -> Principal | None:
    owner = request.headers.get("x-test-owner")
    return Principal(owner_id=owner) if owner else None


async def model_manager(request: Request) -> bool:
    return request.headers.get("x-test-role") == "manager"


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime()


@pytest.fixture
async def client(runtime: FakeRuntime) -> AsyncIterator[httpx.AsyncClient]:
    app = http.create_app(cast(HarnessRuntime, runtime), authenticate)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as connection:
        yield connection


@pytest.fixture
def model_control() -> FakeModelControl:
    return FakeModelControl()


@pytest.fixture
async def control_client(
    runtime: FakeRuntime, model_control: FakeModelControl,
) -> AsyncIterator[httpx.AsyncClient]:
    app = http.create_app(
        cast(HarnessRuntime, runtime), authenticate,
        model_control=model_control, authorize_model_manager=model_manager,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as connection:
        yield connection


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/status"),
        ("GET", "/api/runs"),
        ("POST", "/api/runs"),
        ("GET", "/api/runs/run-1"),
        ("POST", "/api/runs/run-1/cancel"),
        ("GET", "/api/model"),
        ("POST", "/api/model/enable"),
        ("POST", "/api/model/disable"),
    ],
)
async def test_all_data_endpoints_authenticate_before_runtime(
    client: httpx.AsyncClient, runtime: FakeRuntime, method: str, path: str,
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": "Authentication required"}
    assert not runtime.calls


async def test_model_manager_can_enable_observe_login_and_disable(
    control_client: httpx.AsyncClient, model_control: FakeModelControl,
) -> None:
    headers = {"x-test-owner": "alice", "x-test-role": "manager"}
    initial = await control_client.get("/api/model", headers=headers)
    assert initial.json()["can_manage"] is True
    assert initial.json()["state"] == "disabled"
    enabled = await control_client.post("/api/model/enable", headers=headers, json={})
    assert enabled.status_code == 200
    assert enabled.json()["can_manage"] is True
    assert enabled.json()["state"] == "authentication_required"
    assert "private-browser-code" in enabled.json()["authentication_url"]
    state = await control_client.get("/api/model", headers=headers)
    assert state.json()["authentication_url"] == enabled.json()["authentication_url"]
    disabled = await control_client.post("/api/model/disable", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["state"] == "disabled"
    assert disabled.json()["authentication_url"] is None
    assert model_control.calls == ["status", "enable", "status", "disable"]
    for response in (initial, enabled, state, disabled):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"


async def test_model_controls_recheck_privileges_without_leaking_private_state(
    control_client: httpx.AsyncClient, model_control: FakeModelControl,
) -> None:
    manager = {"x-test-owner": "alice", "x-test-role": "manager"}
    await control_client.post("/api/model/enable", headers=manager)
    reader = {"x-test-owner": "alice"}
    response = await control_client.get("/api/model", headers=reader)
    assert response.status_code == 200
    assert response.json() == {"can_manage": False}
    for operation in ("enable", "disable"):
        rejected = await control_client.post(f"/api/model/{operation}", headers=reader)
        assert rejected.status_code == 403
        assert "private-browser-code" not in rejected.text
        assert "private-manager-event" not in rejected.text
        assert rejected.headers["cache-control"] == "no-store"
    public_status = await control_client.get("/api/status", headers=reader)
    assert "private-browser-code" not in public_status.text
    assert "private-manager-event" not in public_status.text
    assert model_control.calls == ["enable"]


async def test_model_manager_check_does_not_replace_regular_authentication(
    control_client: httpx.AsyncClient, model_control: FakeModelControl,
) -> None:
    for method, path in (
        ("GET", "/api/model"),
        ("POST", "/api/model/enable"),
        ("POST", "/api/model/disable"),
    ):
        response = await control_client.request(method, path, headers={"x-test-role": "manager"})
        assert response.status_code == 401
    assert not model_control.calls


async def test_absent_model_control_remains_compatible(client: httpx.AsyncClient) -> None:
    headers = {"x-test-owner": "alice", "x-test-role": "manager"}
    response = await client.get("/api/model", headers=headers)
    assert response.json() == {"can_manage": False}
    for operation in ("enable", "disable"):
        response = await client.post(f"/api/model/{operation}", headers=headers)
        assert response.status_code == 503


async def test_control_without_manager_authorizer_denies_access(
    runtime: FakeRuntime, model_control: FakeModelControl,
) -> None:
    app = http.create_app(cast(HarnessRuntime, runtime), authenticate, model_control=model_control)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = {"x-test-owner": "alice", "x-test-role": "manager"}
        assert (await client.get("/api/model", headers=headers)).json() == {"can_manage": False}
        assert (await client.post("/api/model/enable", headers=headers)).status_code == 403
    assert not model_control.calls


@pytest.mark.parametrize("operation", ["enable", "disable"])
async def test_model_controls_reject_cross_origin_requests(
    control_client: httpx.AsyncClient, model_control: FakeModelControl, operation: str,
) -> None:
    response = await control_client.post(
        f"/api/model/{operation}",
        headers={
            "x-test-owner": "alice", "x-test-role": "manager", "origin": "https://foreign.test",
        },
    )
    assert response.status_code == 403
    assert not model_control.calls


@pytest.mark.parametrize("operation", ["enable", "disable"])
async def test_model_control_body_is_bounded_and_cannot_change_configuration(
    control_client: httpx.AsyncClient, model_control: FakeModelControl, operation: str,
) -> None:
    headers = {"x-test-owner": "alice", "x-test-role": "manager"}
    path = f"/api/model/{operation}"
    for body in ('{"command":"untrusted-launcher"}', "[]", "not-json"):
        response = await control_client.post(path, headers=headers, content=body)
        assert response.status_code == 400
    response = await control_client.post(path, headers=headers, content=b"x" * 65_537)
    assert response.status_code == 413

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 40_000
        yield b"x" * 40_000

    response = await control_client.post(path, headers=headers, content=chunks())
    assert response.status_code == 413
    assert not model_control.calls


async def test_run_contract_and_identity_are_forwarded(
    client: httpx.AsyncClient, runtime: FakeRuntime,
) -> None:
    headers = {"x-test-owner": "alice"}
    response = await client.post(
        "/api/runs", headers=headers,
        json={"question": "  How does payment work?  ", "mode": "investigate", "effort": "high"},
    )
    assert response.status_code == 202
    run = response.json()
    assert run["question"] == "How does payment work?"
    assert run["mode"] == "investigate"
    assert run["effort"] == "high"
    listed = await client.get("/api/runs", headers=headers)
    assert listed.json() == {"runs": [run]}
    assert (await client.get("/api/runs/run-1", headers=headers)).json() == run
    assert (await client.get("/api/status", headers=headers)).json()["model"] == {
        "available": False
    }
    cancelled = await client.post("/api/runs/run-1/cancel", headers=headers)
    assert cancelled.json()["status"] == "cancelled"
    assert {owner for _, owner in runtime.calls} == {"alice"}


async def test_foreign_and_missing_run_look_the_same(client: httpx.AsyncClient) -> None:
    await client.post("/api/runs", headers={"x-test-owner": "alice"}, json={"question": "Hello"})
    headers = {"x-test-owner": "bob"}
    for path in ("/api/runs/run-1", "/api/runs/unknown"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 404
        assert response.json() == {"error": "Run not found"}
    response = await client.post("/api/runs/run-1/cancel", headers=headers)
    assert response.status_code == 404
    assert (await client.get("/api/runs", headers=headers)).json() == {"runs": []}


@pytest.mark.parametrize(
    "payload",
    [
        {}, {"question": " "}, {"question": "x", "mode": "magic"},
        {"question": "x", "effort": "unlimited"},
        {"question": "x", "owner_id": "someone-else"},
        {"question": "x" * 8001}, {"question": "x", "index_ids": ["a"] * 1001},
        ["not", "an object"],
    ],
)
async def test_invalid_request_does_not_reach_runtime(
    client: httpx.AsyncClient, runtime: FakeRuntime, payload: Any,
) -> None:
    response = await client.post("/api/runs", headers={"x-test-owner": "alice"}, json=payload)
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert "someone-else" not in response.text
    assert not runtime.calls


async def test_bounded_json_and_content_type(
    client: httpx.AsyncClient, runtime: FakeRuntime,
) -> None:
    headers = {"x-test-owner": "alice", "content-type": "application/json"}
    malformed = await client.post("/api/runs", headers=headers, content="{not json")
    assert malformed.status_code == 400
    wrong_type = await client.post(
        "/api/runs", headers={"x-test-owner": "alice"}, content='{"question":"x"}'
    )
    assert wrong_type.status_code == 415
    oversized = await client.post("/api/runs", headers=headers, content=b"x" * 65_537)
    assert oversized.status_code == 413

    async def chunks() -> AsyncIterator[bytes]:
        yield b"x" * 40_000
        yield b"x" * 40_000

    streamed = await client.post("/api/runs", headers=headers, content=chunks())
    assert streamed.status_code == 413
    assert not runtime.calls


@pytest.mark.parametrize("origin", ["https://foreign.example", "null", "http://testserver/path"])
async def test_cross_origin_mutations_are_rejected(
    client: httpx.AsyncClient, runtime: FakeRuntime, origin: str,
) -> None:
    response = await client.post(
        "/api/runs", headers={"x-test-owner": "alice", "origin": origin},
        json={"question": "x"},
    )
    assert response.status_code == 403
    assert not runtime.calls


async def test_same_origin_default_port_is_accepted(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/runs", headers={"x-test-owner": "alice", "origin": "http://testserver:80"},
        json={"question": "x"},
    )
    assert response.status_code == 202


@pytest.mark.parametrize(
    ("failure", "status"),
    [(QueueFullError("Queue is full"), 429), (HarnessUnavailableError("Disabled"), 503)],
)
async def test_runtime_capacity_errors_are_actionable(
    client: httpx.AsyncClient, runtime: FakeRuntime, failure: Exception, status: int,
) -> None:
    runtime.failure = failure
    response = await client.post(
        "/api/runs", headers={"x-test-owner": "alice"}, json={"question": "x"},
    )
    assert response.status_code == status
    assert response.json() == {"error": str(failure)}


@pytest.mark.parametrize("manage", [True, False])
def test_lifespan_ownership(runtime: FakeRuntime, manage: bool) -> None:
    app = http.create_app(cast(HarnessRuntime, runtime), authenticate, manage_lifespan=manage)
    with TestClient(app) as client:
        assert client.get("/api/status", headers={"x-test-owner": "a"}).status_code == 200
        assert runtime.started == int(manage)
    assert runtime.stopped == int(manage)


async def test_static_files_are_public_and_allowlisted(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http, "_STATIC", tmp_path)
    (tmp_path / "index.html").write_text("<!doctype html><title>Harness</title>")
    (tmp_path / "app.js").write_text("'use strict';")
    (tmp_path / "styles.css").write_text("body { color: black; }")
    (tmp_path / "secret.txt").write_text("not served")
    for path in ("/", "/assets/app.js", "/assets/styles.css"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "script-src 'self'" in response.headers["content-security-policy"]
    for path in ("/assets/secret.txt", "/assets/../secret.txt"):
        response = await client.get(path)
        assert response.status_code == 404
        assert "not served" not in response.text


def test_standalone_requires_long_token() -> None:
    for token in ("", "too-short", " " + "a" * 32):
        with pytest.raises(ValueError, match="HARNESS_API_TOKEN"):
            standalone.build_app({"HARNESS_API_TOKEN": token})


def test_standalone_starts_real_runtime_without_inventing_providers(tmp_path: Path) -> None:
    token = "standalone-smoke-token-12345678901234567890"
    app = standalone.build_app({
        "HARNESS_API_TOKEN": token,
        "HARNESS_STATE_DIR": str(tmp_path),
    })
    with TestClient(app) as client:
        response = client.get("/api/status", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        status = response.json()
        assert status["enabled"] and status["started"]
        assert status["source_available"] is False
        assert status["model"]["available"] is False
        assert status["indexes"] == []


async def test_standalone_only_bearer_auth_and_unconfigured_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "a-secure-token-for-local-testing-123456789"
    runtime = FakeRuntime()
    constructor_arguments: dict[str, Any] = {}

    def make_runtime(settings: Any, **kwargs: Any) -> FakeRuntime:
        constructor_arguments.update(settings=settings, **kwargs)
        return runtime

    monkeypatch.setattr(standalone, "HarnessRuntime", make_runtime)
    app = standalone.build_app({"HARNESS_API_TOKEN": token, "HARNESS_STATE_DIR": str(tmp_path)})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/status?token=" + token)).status_code == 401
        assert (await client.get(
            "/api/status", headers={"x-kb-admin-password": token}
        )).status_code == 401
        response = await client.get("/api/status", headers={"authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["model"]["available"] is False
        assert token not in response.text
    assert runtime.calls[0][1].startswith("standalone:")
    assert token not in runtime.calls[0][1]
    assert constructor_arguments["settings"].enabled
    assert constructor_arguments["settings"].state_dir == tmp_path
    assert constructor_arguments["source"] is None
    assert constructor_arguments["model"] is None


def test_provider_factory_is_explicit_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("test_harness_provider")
    source = type("Source", (), {name: lambda _: None for name in ("indexes", "search", "read")})()
    model = type("Model", (), {name: lambda _: None for name in ("status", "decide")})()
    module.factory = lambda: (source, model)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert standalone._providers("test_harness_provider:factory") == (source, model)
    for specification in ("missing_separator", "module:", ":factory"):
        with pytest.raises(ValueError, match="module:function"):
            standalone._providers(specification)
    module.factory = lambda: (object(), model)  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="Source provider"):
        standalone._providers("test_harness_provider:factory")
